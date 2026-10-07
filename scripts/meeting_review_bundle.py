"""Transfer one verified local Agent draft set to the authenticated demo tenant.

This is an operator-only import, not an HTTP endpoint or an Agent run. It keeps
original task/draft IDs and traces, maps the known submitter, and never writes
to Feishu. The bundle contains meeting text and must stay outside Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

from fde_control_plane import Actor, ActorType, AgentVersion, AssistantBinding, ControlPlane, SQLiteStore
from fde_control_plane.models import MeetingRecord, MeetingTodoDraft, TaskRun, TaskRunStatus
from scripts.meeting_draft_review_export import load_review


def export_bundle(db: Path, review_path: Path, config_path: Path) -> dict:
    from scripts.feishu_meeting_agent_ingest_probe import document_id_from_config

    review = json.loads(review_path.read_text(encoding="utf-8"))
    document_id = document_id_from_config(json.loads(config_path.read_text(encoding="utf-8")))
    connection = sqlite3.connect(db)
    try:
        record = json.loads(connection.execute(
            "SELECT data_json FROM meeting_records ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0])
        current = load_review(db, record["record_id"])
        for key in ("record_id_hash", "document_id_hash", "revision_id", "todos"):
            if key == "todos":
                without_evidence = [{k: v for k, v in t.items() if k != "evidence"} for t in review[key]]
                if current[key] != without_evidence:
                    raise ValueError("review snapshot no longer matches stored drafts")
            elif current[key] != review[key]:
                raise ValueError("review snapshot no longer matches stored record")
        task = json.loads(connection.execute("SELECT data_json FROM task_runs WHERE task_run_id = ?", (record["task_run_id"],)).fetchone()[0])
        member = connection.execute("SELECT external_ref_hash FROM actors WHERE actor_id = ?", (record["submitted_by"],)).fetchone()[0]
        drafts = [json.loads(row[0]) for row in connection.execute("SELECT data_json FROM meeting_todo_drafts WHERE record_id = ? ORDER BY rowid", (record["record_id"],))]
        related = {}
        for table in ("task_steps", "delegations", "runtime_events"):
            related[table] = [json.loads(row[0]) for row in connection.execute(f"SELECT data_json FROM {table} WHERE task_run_id = ? ORDER BY rowid", (task["task_run_id"],))]
        blocks = {}
        for todo in review["todos"]:
            for block in todo.get("evidence", []):
                if block["block_id"] in blocks and blocks[block["block_id"]] != block["text"]:
                    raise ValueError("conflicting evidence text")
                blocks[block["block_id"]] = block["text"]
        return {"schema_version": "meeting-transfer.v1", "source_member_hash": member,
                "record": record, "task": task, "drafts": drafts, **related,
                "source": {"document_id": document_id, "title": "会议纪要 · 待办核对",
                           "revision_id": record["revision_id"],
                           "evidence": [{"block_id": key, "text": value} for key, value in sorted(blocks.items())]}}
    finally:
        connection.close()


def import_bundle(cp: ControlPlane, bundle: dict, *, tenant_id: str, member_id: str, member_hash: str) -> bool:
    if bundle.get("schema_version") != "meeting-transfer.v1" or bundle.get("source_member_hash") != member_hash:
        raise PermissionError("source member does not match the deployment binding")
    member = cp._actor(member_id)
    if not member.active or member.actor_type != ActorType.USER or member.tenant_id != tenant_id or member.external_ref_hash != member_hash:
        raise PermissionError("deployment member is not an active matching member")
    record, task, source = bundle["record"], bundle["task"], bundle["source"]
    if (task["status"] != TaskRunStatus.WAITING_REVIEW or task["requested_by"] != record["submitted_by"]
            or task["tenant_id"] != record["tenant_id"] or task["task_run_id"] != record["task_run_id"]):
        raise ValueError("bundle is not an untouched review task")
    document_hash = hashlib.sha256(source["document_id"].encode()).hexdigest()
    expected = record["document_id_hash"]
    if len(expected) not in (12, 64) or document_hash[:len(expected)] != expected or source["revision_id"] != record["revision_id"]:
        raise ValueError("source mismatch")
    blocks = {int(b["block_id"]): b["text"] for b in source["evidence"] if b["text"]}
    for draft in bundle["drafts"]:
        if (draft["record_id"] != record["record_id"] or draft["status"] != "NEEDS_CONFIRMATION"
                or not draft["evidence_block_ids"] or not set(draft["evidence_block_ids"]).issubset(blocks)
                or draft["assignee_actor_id"] is not None):
            raise ValueError("draft is not a verifiable unassigned candidate")
    existing = cp.store.get_meeting_record(record["record_id"])
    if existing:
        if existing["tenant_id"] == tenant_id and existing["submitted_by"] == member_id and cp.store.get_meeting_source(record["record_id"]) == source:
            return False
        raise ValueError("record already exists with different provenance")
    if cp.store.get_task_run(task["task_run_id"]):
        raise ValueError("task ID already exists")
    caps = frozenset({"doc.read", "task.write"})
    actors = [replace(member, capabilities=member.capabilities | caps),
              Actor("assistant-primary", tenant_id, ActorType.PERSONAL_ASSISTANT, caps),
              Actor("meeting-agent", tenant_id, ActorType.BUSINESS_AGENT, caps)]
    imported_task = TaskRun(**{**task, "tenant_id": tenant_id, "requested_by": member_id,
                              "assistant_actor_id": "assistant-primary", "agent_id": "meeting-agent",
                              "status": TaskRunStatus.WAITING_REVIEW})
    imported_record = MeetingRecord(**{**record, "tenant_id": tenant_id, "submitted_by": member_id})
    version = AgentVersion("meeting-agent", task["agent_version"], caps)
    binding = AssistantBinding("binding-primary", tenant_id, member_id, "assistant-primary")
    conn = cp.store.connection
    encode = cp.store._json
    conn.execute("BEGIN IMMEDIATE")
    try:
        for actor in actors:
            conn.execute("INSERT OR REPLACE INTO actors VALUES (?,?,?,?,?,?,?)", (
                actor.actor_id, actor.tenant_id, actor.actor_type, encode(actor.capabilities),
                encode(actor.roles), actor.external_ref_hash, int(actor.active),
            ))
        conn.execute("INSERT OR IGNORE INTO agent_versions VALUES (?,?,?,?,?)", (version.agent_id, version.version, encode(version.capabilities), 1, version.skill_version))
        conn.execute("INSERT OR IGNORE INTO assistant_bindings VALUES (?,?,?,?,?)", (binding.binding_id, tenant_id, member_id, binding.assistant_actor_id, 1))
        conn.execute("INSERT INTO task_runs VALUES (?,?,?)", (imported_task.task_run_id, tenant_id, encode(asdict(imported_task))))
        conn.execute("INSERT INTO meeting_records VALUES (?,?,?,?,?)", (imported_record.record_id, tenant_id, expected, imported_record.revision_id, encode(asdict(imported_record))))
        for raw in bundle["drafts"]:
            draft = MeetingTodoDraft(**raw)
            conn.execute("INSERT INTO meeting_todo_drafts VALUES (?,?,?,?)", (draft.draft_id, draft.record_id, draft.todo_id, encode(asdict(draft))))
        for step in bundle.get("task_steps", []):
            conn.execute("INSERT INTO task_steps VALUES (?,?,?)", (step["step_id"], imported_task.task_run_id, encode(step)))
        for delegation in bundle.get("delegations", []):
            data = {**delegation, "from_actor_id": "assistant-primary", "to_actor_id": "meeting-agent"}
            conn.execute("INSERT INTO delegations VALUES (?,?,?)", (data["delegation_id"], imported_task.task_run_id, encode(data)))
        for event in bundle.get("runtime_events", []):
            conn.execute("INSERT INTO runtime_events VALUES (?,?,?,?)", (f"{imported_task.task_run_id}:{event['sequence']}", imported_task.task_run_id, event["sequence"], encode(event)))
        conn.execute("INSERT INTO meeting_sources VALUES (?,?)", (record["record_id"], encode(source)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    cp._audit("MEETING_DRAFT_IMPORTED", tenant_id, member_id, imported_task.task_run_id,
              {"record_id": record["record_id"], "todo_count": len(bundle["drafts"]), "source_tenant": record["tenant_id"]}, "IMPORTED")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("export", "import"))
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/feishu_meeting_agent_probe.sqlite3"))
    parser.add_argument("--review", type=Path, default=Path("data/feishu_meeting_review.json"))
    parser.add_argument("--config", type=Path, default=Path("D:/Temp/feishu-minutes.local.json"))
    args = parser.parse_args()
    if args.mode == "export":
        bundle = export_bundle(args.db, args.review, args.config)
        args.bundle.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        print(f"BUNDLE_EXPORTED todos={len(bundle['drafts'])} evidence={len(bundle['source']['evidence'])}")
    else:
        cp = ControlPlane(SQLiteStore(os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3")))
        try:
            imported = import_bundle(cp, json.loads(args.bundle.read_text(encoding="utf-8")),
                                     tenant_id=os.environ["FDE_TENANT_ID"], member_id="member-primary",
                                     member_hash=os.environ["FDE_PREBOUND_OPEN_ID_HASH"])
            print(f"BUNDLE_IMPORTED changed={imported} feishu_writes=0")
        finally:
            cp.close()


if __name__ == "__main__":
    main()
