"""Fresh-source check before a Feishu meeting review becomes approval proposals."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Protocol

from .models import ActorType, IdentityContext
from .orchestration import AgentOrchestrator, ReviewSubmission
from .service import ControlPlane


class DocumentRevisionReader(Protocol):
    def get_revision(self, document_id: str) -> str: ...


class DocumentContentReader(DocumentRevisionReader, Protocol):
    def read_document(self, document_id: str) -> dict[str, Any]: ...


class EventDocumentAccessVerifier(Protocol):
    """Prove a bot-event sender can read this document as that member."""

    def verify(self, identity: IdentityContext, document_id: str) -> None: ...


class FeishuDocumentRevisionReader:
    """Read only docx metadata; the SDK client owns Feishu credentials."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def get_revision(self, document_id: str) -> str:
        import lark_oapi as lark

        request = lark.api.docx.v1.model.GetDocumentRequest.builder().document_id(document_id).build()
        response = self.client.docx.v1.document.get(request)
        code = getattr(response, "code", None)
        if code != 0:
            raise RuntimeError(f"FEISHU_DOCUMENT_READ_{code if code is not None else 'UNKNOWN'}")
        document = getattr(getattr(response, "data", None), "document", None)
        revision_id = getattr(document, "revision_id", None)
        if revision_id is None:
            raise RuntimeError("FEISHU_DOCUMENT_REVISION_MISSING")
        return str(revision_id)

    def read_document(self, document_id: str) -> dict[str, Any]:
        """Read metadata and descendant text blocks for a bounded Agent run."""
        import lark_oapi as lark

        meta_request = lark.api.docx.v1.model.GetDocumentRequest.builder().document_id(document_id).build()
        meta = self.client.docx.v1.document.get(meta_request)
        if getattr(meta, "code", None) != 0:
            raise RuntimeError(f"FEISHU_DOCUMENT_READ_{getattr(meta, 'code', 'UNKNOWN')}")
        document = getattr(getattr(meta, "data", None), "document", None)
        revision_id = getattr(document, "revision_id", None)
        if revision_id is None:
            raise RuntimeError("FEISHU_DOCUMENT_REVISION_MISSING")
        text_fields = (
            "page", "text", "heading1", "heading2", "heading3", "heading4",
            "heading5", "heading6", "heading7", "heading8", "heading9",
            "bullet", "ordered", "code", "quote", "todo",
        )

        def block_text(block: Any) -> str:
            parts: list[str] = []
            for field_name in text_fields:
                field = getattr(block, field_name, None)
                for element in getattr(field, "elements", None) or []:
                    content = getattr(getattr(element, "text_run", None), "content", None)
                    if content:
                        parts.append(content)
            return "".join(parts).strip()

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
            response = self.client.docx.v1.document_block_children.get(builder.build())
            if getattr(response, "code", None) != 0:
                raise RuntimeError(f"FEISHU_DOCUMENT_BLOCKS_READ_{getattr(response, 'code', 'UNKNOWN')}")
            data = getattr(response, "data", None)
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
                raise RuntimeError("FEISHU_DOCUMENT_BLOCKS_PAGINATION_STALLED")
            page_token = next_page
        else:
            raise RuntimeError("FEISHU_DOCUMENT_BLOCKS_PAGINATION_LIMIT")
        if not blocks:
            raise RuntimeError("FEISHU_DOCUMENT_BLOCKS_EMPTY")
        return {
            "document_id": document_id,
            "revision_id": str(revision_id),
            "title": str(getattr(document, "title", None) or "会议纪要"),
            "blocks": blocks,
        }


class FeishuMeetingReviewGateway:
    """Accept edits only from an OAuth-verified, bound meeting submitter."""

    def __init__(
        self, control_plane: ControlPlane, orchestrator: AgentOrchestrator,
        revision_reader: DocumentRevisionReader,
        event_access_verifier: EventDocumentAccessVerifier | None = None,
    ) -> None:
        self.control_plane = control_plane
        self.orchestrator = orchestrator
        self.revision_reader = revision_reader
        self.event_access_verifier = event_access_verifier

    @staticmethod
    def document_id_from_url(value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value.strip())
        if parsed.scheme != "https" or not parsed.hostname or not (
            parsed.hostname == "feishu.cn" or parsed.hostname.endswith(".feishu.cn")
        ):
            raise ValueError("请输入飞书 docx 链接")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] != "docx" or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
            raise ValueError("请输入飞书 docx 链接")
        return parts[1]

    def start_meeting(
        self, *, document_url: str, requester_identity: IdentityContext,
        agent_id: str = "meeting-agent",
    ) -> dict[str, Any]:
        """Read one document and route it through the member's assistant."""
        member = self.control_plane.verify_identity(requester_identity)
        if requester_identity.auth_mode not in {"user_oauth", "feishu_event"} or member.actor_type != ActorType.USER:
            raise PermissionError("会议任务必须由飞书成员身份发起")
        binding = self.control_plane.store.get_binding(member.actor_id, member.tenant_id)
        if binding is None:
            raise PermissionError("成员尚未绑定个人助手")
        assistant_id = str(binding["assistant_actor_id"])
        agent = self.control_plane._actor(agent_id)
        if agent.actor_type != ActorType.BUSINESS_AGENT or agent.tenant_id != member.tenant_id:
            raise PermissionError("会议 Agent 不属于当前租户")
        version_rows = [row for row in self.control_plane.store.list_agent_versions([agent_id]) if row["active"]]
        if not agent.active or not version_rows:
            raise PermissionError("会议 Agent 当前不可用")
        version = int(version_rows[0]["version"])
        document_id = self.document_id_from_url(document_url)
        if requester_identity.auth_mode == "feishu_event":
            if self.event_access_verifier is None:
                raise PermissionError("用户级文档访问尚未验证")
            self.event_access_verifier.verify(requester_identity, document_id)
        reader = self.revision_reader
        if not hasattr(reader, "read_document"):
            raise RuntimeError("会议文档读取器未配置")
        source = reader.read_document(document_id)  # type: ignore[attr-defined]
        revision_id = str(source["revision_id"])
        document_hash = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:12]
        dispatch_key = hashlib.sha256(
            f"meeting:{member.tenant_id}:{member.actor_id}:{document_id}:{revision_id}:{agent_id}:{version}".encode()
        ).hexdigest()
        record_id = "meeting-" + dispatch_key[:24]
        execution = self.orchestrator.execute_meeting_request(
            requester_identity=requester_identity,
            member_actor_id=member.actor_id,
            assistant_actor_id=assistant_id,
            agent_id=agent_id,
            agent_version=version,
            document_id_hash=document_hash,
            revision_id=revision_id,
            blocks=list(source["blocks"]),
            valid_block_ids={int(block["index"]) for block in source["blocks"]},
            record_id=record_id,
            dispatch_key=dispatch_key,
        )
        if execution.meeting_result is not None and self.control_plane.store.get_meeting_source(record_id) is None:
            self.control_plane.store.save_meeting_source(record_id, {
                "document_id": document_id,
                "revision_id": revision_id,
                "title": source.get("title") or "会议纪要",
                "evidence": [
                    {"block_id": int(block["index"]), "text": str(block.get("text") or "")}
                    for block in source["blocks"] if str(block.get("text") or "").strip()
                ],
            })
        record = self.control_plane.store.get_meeting_record(record_id)
        drafts = self.control_plane.store.list_meeting_todos(record_id) if record else []
        task_row = self.control_plane.store.get_task_run(execution.task_run_id) or {}
        runtime_status = execution.runtime_result.status
        created = bool(record is not None and task_row.get("status") == "WAITING_REVIEW")
        return {
            "record_id": record_id,
            "task_run_id": execution.task_run_id,
            "title": source.get("title") or "会议纪要",
            "runtime_status": runtime_status,
            "task_status": task_row.get("status"),
            "error_code": execution.runtime_result.error_code,
            "created": created,
            "contract_status": record.get("contract_status") if record else None,
            "todo_count": len(drafts),
            "needs_confirmation": sum(d["status"] == "NEEDS_CONFIRMATION" for d in drafts),
            "deduplicated": execution.deduplicated,
        }

    def submit(
        self, *, task_run_id: str, record_id: str, document_id: str,
        reviewer_identity: IdentityContext,
        updates_by_todo_ref: dict[str, dict[str, Any]],
    ) -> ReviewSubmission:
        record = self.control_plane.store.get_meeting_record(record_id)
        if record is None:
            raise KeyError(record_id)
        task = self.control_plane._task(task_run_id)
        reviewer = self.control_plane.verify_identity(reviewer_identity)
        if not self.control_plane._actor(task.agent_id).active or not self.control_plane._agent_version(task.agent_id, task.agent_version).active:
            raise PermissionError("meeting agent is paused")
        if (
            record.get("task_run_id") != task_run_id
            or record["tenant_id"] != task.tenant_id
            or record["submitted_by"] != task.requested_by
            or reviewer.actor_type != ActorType.USER
            or reviewer.actor_id != task.requested_by
            or reviewer_identity.auth_mode != "user_oauth"
            or not reviewer.external_ref_hash
            or reviewer.external_ref_hash != reviewer_identity.subject_ref_hash
        ):
            raise PermissionError("review is not bound to the meeting submitter")
        expected_hash = str(record["document_id_hash"])
        actual_hash = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
        if len(expected_hash) not in {12, 64} or actual_hash[:len(expected_hash)] != expected_hash:
            raise PermissionError("source document does not match the meeting record")
        try:
            current_revision = self.revision_reader.get_revision(document_id)
        except Exception:
            self.control_plane._audit(
                "FEISHU_DOCUMENT_REVISION_CHECK", task.tenant_id, reviewer.actor_id,
                task_run_id, {"document_id_hash": expected_hash}, "ERROR",
            )
            raise
        if current_revision != str(record["revision_id"]):
            self.control_plane._audit(
                "FEISHU_DOCUMENT_REVISION_CHECK", task.tenant_id, reviewer.actor_id,
                task_run_id, {"document_id_hash": expected_hash}, "STALE",
            )
            raise ValueError("source document changed since Agent extraction")
        self.control_plane._audit(
            "FEISHU_DOCUMENT_REVISION_CHECK", task.tenant_id, reviewer.actor_id,
            task_run_id, {"document_id_hash": expected_hash}, "MATCH",
        )
        return self.orchestrator.submit_meeting_review_refs(
            task_run_id=task_run_id, record_id=record_id,
            reviewer_identity=reviewer_identity,
            updates_by_todo_ref=updates_by_todo_ref,
        )


__all__ = ["DocumentRevisionReader", "DocumentContentReader", "EventDocumentAccessVerifier", "FeishuDocumentRevisionReader", "FeishuMeetingReviewGateway"]
