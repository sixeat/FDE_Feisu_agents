"""Fresh-source check before a Feishu meeting review becomes approval proposals."""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

from .models import ActorType, IdentityContext
from .orchestration import AgentOrchestrator, ReviewSubmission
from .service import ControlPlane


class DocumentRevisionReader(Protocol):
    def get_revision(self, document_id: str) -> str: ...


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


class FeishuMeetingReviewGateway:
    """Accept edits only from an OAuth-verified, bound meeting submitter."""

    def __init__(
        self, control_plane: ControlPlane, orchestrator: AgentOrchestrator,
        revision_reader: DocumentRevisionReader,
    ) -> None:
        self.control_plane = control_plane
        self.orchestrator = orchestrator
        self.revision_reader = revision_reader

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


__all__ = ["DocumentRevisionReader", "FeishuDocumentRevisionReader", "FeishuMeetingReviewGateway"]
