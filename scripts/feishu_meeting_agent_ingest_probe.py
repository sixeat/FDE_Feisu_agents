"""Read one test docx and persist Hermes meeting drafts without side effects."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import lark_oapi as lark

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane import (  # noqa: E402
    Actor, ActorType, AgentOrchestrator, AgentVersion, AssistantBinding,
    ControlPlane, HermesRuntimeAdapter, IdentityContext, SQLiteStore,
)


CONFIG = Path("D:/Temp/feishu-minutes.local.json")
OPEN_ID_FILE = ROOT / "data" / "feishu_operator_open_id.txt"
DB_FILE = ROOT / "data" / "feishu_meeting_agent_probe.sqlite3"
TEXT_FIELDS = (
    "page", "text", "heading1", "heading2", "heading3", "heading4",
    "heading5", "heading6", "heading7", "heading8", "heading9",
    "bullet", "ordered", "code", "quote", "todo",
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def document_id_from_config(config: dict[str, Any]) -> str:
    value = str(config.get("minute_token") or "")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or not (
        parsed.hostname == "feishu.cn" or parsed.hostname.endswith(".feishu.cn")
    ):
        raise ValueError("configuration does not contain a Feishu docx URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "docx":
        raise ValueError("configuration does not contain a Feishu docx URL")
    return parts[1]


def block_text(block: Any) -> str:
    parts: list[str] = []
    for name in TEXT_FIELDS:
        field = getattr(block, name, None)
        for element in getattr(field, "elements", None) or []:
            content = getattr(getattr(element, "text_run", None), "content", None)
            if content:
                parts.append(content)
    return "".join(parts).strip()


def read_document(client: Any, document_id: str) -> tuple[str, list[dict[str, Any]]]:
    meta_request = lark.api.docx.v1.model.GetDocumentRequest.builder().document_id(document_id).build()
    meta = client.docx.v1.document.get(meta_request)
    print(f"MEETING_DOC_META code={getattr(meta, 'code', None)} document_hash={digest(document_id)[:12]}", flush=True)
    if getattr(meta, "code", None) != 0:
        raise RuntimeError("document metadata read failed")
    document = getattr(getattr(meta, "data", None), "document", None)
    revision_id = str(getattr(document, "revision_id", ""))
    if not revision_id:
        raise RuntimeError("document revision is missing")
    blocks: list[dict[str, Any]] = []
    page_token = None
    for _ in range(20):
        builder = (
            lark.api.docx.v1.model.GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id).block_id(document_id)
            .page_size(500).with_descendants(True)
        )
        if page_token:
            builder = builder.page_token(page_token)
        response = client.docx.v1.document_block_children.get(builder.build())
        if getattr(response, "code", None) != 0:
            raise RuntimeError("document blocks read failed")
        data = response.data
        for block in getattr(data, "items", None) or []:
            blocks.append({
                "index": len(blocks) + 1,
                "block_type": getattr(block, "block_type", 0),
                "text": block_text(block),
            })
        if not getattr(data, "has_more", False):
            break
        next_page = getattr(data, "page_token", None)
        if not next_page or next_page == page_token:
            raise RuntimeError("document pagination did not advance")
        page_token = next_page
    else:
        raise RuntimeError("document exceeded pagination limit")
    print(
        f"MEETING_DOC_BLOCKS code=0 revision_id={revision_id} "
        f"count={len(blocks)} text_length={sum(len(block['text']) for block in blocks)}",
        flush=True,
    )
    if not blocks:
        raise RuntimeError("document has no readable blocks")
    return revision_id, blocks


def main() -> int:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    app_id, app_secret = str(config["app_id"]), str(config["app_secret"])
    document_id = document_id_from_config(config)
    open_id = OPEN_ID_FILE.read_text(encoding="utf-8").strip()
    if not open_id:
        raise ValueError("local operator binding is missing")
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    revision_id, blocks = read_document(client, document_id)
    source_hash = digest(document_id)[:12]
    attempt = os.environ.get("FEISHU_MEETING_PROBE_ATTEMPT", "1")
    if not attempt.isdecimal() or int(attempt) < 1:
        raise ValueError("probe attempt must be a positive integer")
    request_key = digest(f"{document_id}:{revision_id}:attempt:{attempt}")[:20]
    record_id = f"real-meeting-{request_key}"
    cp = ControlPlane(SQLiteStore(DB_FILE))
    caps = frozenset({"doc.read", "task.write"})
    subject_hash = digest(open_id)
    cp.register_actor(Actor("probe-member", "probe-tenant", ActorType.USER, caps, external_ref_hash=subject_hash))
    cp.register_actor(Actor("probe-assistant", "probe-tenant", ActorType.PERSONAL_ASSISTANT, caps))
    cp.register_actor(Actor("probe-meeting-agent", "probe-tenant", ActorType.BUSINESS_AGENT, caps))
    cp.register_agent_version(AgentVersion("probe-meeting-agent", 1, caps))
    cp.bind_assistant(AssistantBinding("probe-binding", "probe-tenant", "probe-member", "probe-assistant"))
    execution = AgentOrchestrator(cp, HermesRuntimeAdapter(timeout_seconds=300)).execute_meeting_request(
        requester_identity=IdentityContext("probe-tenant", "probe-member", "application", subject_hash),
        member_actor_id="probe-member", assistant_actor_id="probe-assistant",
        agent_id="probe-meeting-agent", agent_version=1,
        document_id_hash=source_hash, revision_id=revision_id, blocks=blocks,
        record_id=record_id, dispatch_key=f"docx-probe:{request_key}",
        runtime_deadline_seconds=300,
    )
    record = execution.meeting_result
    persisted_record = cp.store.get_meeting_record(record_id)
    drafts = cp.store.list_meeting_todos(record_id) if persisted_record else []
    print(
        "MEETING_AGENT_RESULT "
        f"runtime={execution.runtime_result.status} "
        f"error_code={execution.runtime_result.error_code or 'NONE'} "
        f"attempt={attempt} "
        f"deduplicated={execution.deduplicated} "
        f"contract={record.validation.status if record else persisted_record['contract_status'] if persisted_record else 'NONE'} "
        f"todo_count={len(drafts)} "
        f"needs_confirmation={sum(draft['status'] == 'NEEDS_CONFIRMATION' for draft in drafts)} "
        "card_sent=0 task_write=0",
        flush=True,
    )
    cp.close()
    return 0 if record or execution.deduplicated else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"MEETING_AGENT_PROBE_ERROR type={type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
