"""Controlled real-tenant probe for the stage-2 meeting workflow.

Default mode only reads a dedicated fictional document and prints hashes. Set
``FEISHU_STAGE2_WRITE=1`` to create exactly one confirmed test task through the
same workflow and adapter used by the local vertical slice. Never use this
against production documents.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import lark_oapi as lark

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane import (  # noqa: E402
    Actor,
    ActorType,
    AgentVersion,
    AssistantBinding,
    ControlPlane,
    FeishuTaskGateway,
    MeetingOutboxWorker,
    MeetingWorkflow,
    SQLiteStore,
)


def short_hash(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


TEXT_FIELDS = ("page", "text", "heading1", "heading2", "heading3", "heading4", "heading5", "heading6",
               "heading7", "heading8", "heading9", "bullet", "ordered", "code", "quote", "equation", "todo")


def block_text(block: Any) -> str:
    parts: list[str] = []
    for name in TEXT_FIELDS:
        field = getattr(block, name, None)
        for element in getattr(field, "elements", None) or []:
            run = getattr(element, "text_run", None)
            content = getattr(run, "content", None)
            if content:
                parts.append(content)
    return "".join(parts).strip()


def main() -> int:
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    document_id = os.environ.get("FEISHU_DOCUMENT_ID")
    assignee_open_id = os.environ.get("FEISHU_STAGE2_ASSIGNEE_OPEN_ID")
    due_date = os.environ.get("FEISHU_STAGE2_DUE_DATE")
    if not app_id or not app_secret or not document_id:
        print("缺少 FEISHU_APP_ID、FEISHU_APP_SECRET 或 FEISHU_DOCUMENT_ID", file=sys.stderr)
        return 2
    if os.environ.get("FEISHU_STAGE2_WRITE") == "1" and (not assignee_open_id or not due_date):
        print("写入模式还需要 FEISHU_STAGE2_ASSIGNEE_OPEN_ID 和 FEISHU_STAGE2_DUE_DATE", file=sys.stderr)
        return 2

    try:
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        meta = client.docx.v1.document.get(
            lark.api.docx.v1.model.GetDocumentRequest.builder().document_id(document_id).build()
        )
        if getattr(meta, "code", None) != 0:
            print(f"STAGE2_DOCUMENT_META code={getattr(meta, 'code', None)} msg={getattr(meta, 'msg', None)}")
            return 1
        document = getattr(getattr(meta, "data", None), "document", None)
        revision_id = str(getattr(document, "revision_id", ""))
        blocks_response = client.docx.v1.document_block_children.get(
            lark.api.docx.v1.model.GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id).block_id(document_id).page_size(500).with_descendants(True).build()
        )
        if getattr(blocks_response, "code", None) != 0:
            print(f"STAGE2_DOCUMENT_BLOCKS code={getattr(blocks_response, 'code', None)} msg={getattr(blocks_response, 'msg', None)}")
            return 1
        raw_blocks = getattr(getattr(blocks_response, "data", None), "items", None) or []
        blocks = [{"index": i, "block_type": getattr(block, "block_type", 0), "text": block_text(block)}
                  for i, block in enumerate(raw_blocks, start=1)]
        print(f"STAGE2_DOCUMENT_READ code=0 document_id_hash={short_hash(document_id)} revision_id={revision_id} block_count={len(blocks)}")

        with TemporaryDirectory(prefix="fde-stage2-real-") as temp:
            cp = ControlPlane(SQLiteStore(Path(temp) / "stage2.sqlite3"))
            caps = frozenset({"task.write", "doc.read"})
            cp.register_actor(Actor("member-1", "tenant-1", ActorType.USER, caps))
            cp.register_actor(Actor("assistant-1", "tenant-1", ActorType.PERSONAL_ASSISTANT, caps))
            cp.register_actor(Actor("meeting-agent", "tenant-1", ActorType.BUSINESS_AGENT, caps))
            cp.register_actor(Actor("admin-1", "tenant-1", ActorType.USER, roles=frozenset({"admin"})))
            cp.register_agent_version(AgentVersion("meeting-agent", 1, caps))
            cp.bind_assistant(AssistantBinding("binding-1", "tenant-1", "member-1", "assistant-1"))
            task = cp.create_task_run("member-1", "assistant-1", "meeting-agent", 1)
            workflow = MeetingWorkflow(cp)
            prepared = workflow.ingest_document_blocks(
                record_id="real-stage2-record", tenant_id="tenant-1", submitted_by="member-1",
                document_id_hash=short_hash(document_id) or "missing", revision_id=revision_id, blocks=blocks,
            )
            payload = __import__("fde_control_plane.meeting_input", fromlist=["normalize_document_blocks"]).normalize_document_blocks(
                document_id_hash=short_hash(document_id) or "missing", revision_id=revision_id, blocks=blocks
            )
            # This probe intentionally confirms one fictional item after the user supplies the date.
            if not payload["todos"]:
                print("STAGE2_NO_TODO_CANDIDATE", file=sys.stderr)
                cp.close()
                return 1
            payload["todos"] = payload["todos"][:1]
            if os.environ.get("FEISHU_STAGE2_WRITE") == "1":
                todo = payload["todos"][0]
                todo["needs_confirmation"] = False
                todo["confirmation_reasons"] = []
                todo["due_date_candidate"] = {"raw_text": due_date, "normalized_date": due_date, "status": "confirmed"}
                drafts = workflow.build_drafts(record_id=prepared.record.record_id, payload=payload, assignee_actor_by_todo={todo["todo_id"]: "member-1"})
                proposals = workflow.create_task_proposals(prepared.record.record_id, task.task_run_id)
                proposal, approval = proposals[0]
                decision = cp.handle_approval_callback("stage2-real-callback", approval.approval_id, "admin-1", True, 1, proposal.proposal_hash)
                gateway = FeishuTaskGateway(client, actor_open_ids={"member-1": assignee_open_id})
                result = MeetingOutboxWorker(cp, gateway).execute(decision["tool_call_id"])
                print(f"STAGE2_TASK_RESULT status={result.status} remote_id_hash={short_hash(result.remote_task_id)} notification={result.notification_status}")
                if os.environ.get("FEISHU_STAGE2_CLEANUP") == "1" and result.remote_task_id:
                    deleted = gateway.delete_task(result.remote_task_id)
                    print(f"STAGE2_TASK_CLEANUP deleted={deleted} remote_id_hash={short_hash(result.remote_task_id)}")
                cp.close()
                return 0 if result.status == "SUCCEEDED" else 1
            print(f"STAGE2_DRY_RUN contract_status={prepared.validation.status} todo_count={len(payload['todos'])} write=0")
            cp.close()
        return 0
    except Exception as exc:
        print(f"STAGE2_PROBE_ERROR type={type(exc).__name__} detail={str(exc)[:160]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
