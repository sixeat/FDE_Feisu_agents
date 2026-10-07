"""Export one persisted meeting draft set as a human-review JSON payload.

This is read-only: it never calls Feishu, Hermes, approval APIs, or task APIs.
The exported payload is the contract a future H5/card editor can consume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "feishu_meeting_agent_probe.sqlite3"
DEFAULT_OUTPUT = ROOT / "data" / "feishu_meeting_review.json"
DEFAULT_CONFIG = Path("D:/Temp/feishu-minutes.local.json")


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def load_review(db_path: Path, record_id: str | None) -> dict[str, Any]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        if record_id is None:
            row = connection.execute(
                "SELECT record_id FROM meeting_records ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise ValueError("no meeting record exists")
            record_id = str(row["record_id"])
        record = connection.execute(
            "SELECT data_json FROM meeting_records WHERE record_id = ?", (record_id,)
        ).fetchone()
        if record is None:
            raise ValueError("meeting record does not exist")
        drafts = connection.execute(
            "SELECT data_json FROM meeting_todo_drafts WHERE record_id = ? ORDER BY rowid",
            (record_id,),
        ).fetchall()
        record_data = json.loads(record["data_json"])
        draft_data = [json.loads(row["data_json"]) for row in drafts]
        return {
            "schema_version": "meeting-review.v1",
            "record_id_hash": short_hash(record_id),
            "document_id_hash": record_data["document_id_hash"],
            "revision_id": record_data["revision_id"],
            "contract_status": record_data["contract_status"],
            "reviewer_rule": "meeting_submitter_only",
            "write_policy": "no_feishu_write_until_review_and_approval",
            "todos": [
                {
                    "todo_ref": short_hash(str(draft["todo_id"])),
                    "title": draft["title"],
                    "assignee_actor_id": draft["assignee_actor_id"],
                    "due_date": draft["due_date"],
                    "evidence_block_ids": draft["evidence_block_ids"],
                    "status": draft["status"],
                    "confirmation_reasons": draft["confirmation_reasons"],
                    "allowed_review_actions": ["set_title", "set_assignee", "set_due_date", "discard"],
                }
                for draft in draft_data
            ],
        }
    finally:
        connection.close()


def attach_evidence(
    payload: dict[str, Any], *, document_id: str, revision_id: str,
    blocks: list[dict[str, Any]],
) -> None:
    if short_hash(document_id) != payload["document_id_hash"]:
        raise ValueError("source document does not match the meeting record")
    if str(revision_id) != str(payload["revision_id"]):
        raise ValueError("source document revision changed since Agent extraction")
    indexed = {int(block["index"]): str(block["text"]) for block in blocks}
    for todo in payload["todos"]:
        evidence = []
        for block_id in todo["evidence_block_ids"]:
            text = indexed.get(int(block_id))
            if not text:
                raise ValueError("a cited source block is no longer readable")
            evidence.append({"block_id": int(block_id), "text": text})
        todo["evidence"] = evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--record-id")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--include-evidence", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    payload = load_review(args.db, args.record_id)
    if args.include_evidence:
        import lark_oapi as lark
        from feishu_meeting_agent_ingest_probe import document_id_from_config, read_document

        config = json.loads(args.config.read_text(encoding="utf-8"))
        document_id = document_id_from_config(config)
        client = lark.Client.builder().app_id(str(config["app_id"])).app_secret(str(config["app_secret"])).build()
        revision_id, blocks = read_document(client, document_id)
        attach_evidence(payload, document_id=document_id, revision_id=revision_id, blocks=blocks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"MEETING_REVIEW_EXPORT record_hash={payload['record_id_hash']} "
        f"revision_id={payload['revision_id']} todo_count={len(payload['todos'])} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
