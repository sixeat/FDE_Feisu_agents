"""Dispatch reviewed meeting approvals without executing the task outbox."""

from __future__ import annotations

import hashlib
from typing import Any

from .feishu_approval import CardDeliveryUncertain, FeishuApprovalGateway
from .feishu_review import DocumentRevisionReader
from .meeting import MEETING_TASK_TOOL
from .models import ActorType, ApprovalStatus, TaskRunStatus
from .service import ControlPlane


class MeetingApprovalBridge:
    def __init__(self, control_plane: ControlPlane, gateway: FeishuApprovalGateway | None,
                 revision_reader: DocumentRevisionReader | None, *, tenant_id: str) -> None:
        self.cp = control_plane
        self.gateway = gateway
        self.revision_reader = revision_reader
        self.tenant_id = tenant_id

    def _reviewed(self, record_id: str, member_id: str):
        record = self.cp.store.get_meeting_record(record_id)
        member = self.cp._actor(member_id)
        if (record is None or record["tenant_id"] != self.tenant_id
                or record["submitted_by"] != member_id or member.tenant_id != self.tenant_id
                or not member.active or member.actor_type != ActorType.USER):
            raise PermissionError("meeting does not belong to the configured member and tenant")
        task = self.cp._task(record["task_run_id"])
        if task.tenant_id != self.tenant_id or task.requested_by != member_id:
            raise PermissionError("meeting task owner mismatch")
        if task.status != TaskRunStatus.WAITING_APPROVAL:
            raise ValueError("meeting must be submitted for approval first")
        permission = self.cp.authorize_operation(task.task_run_id, MEETING_TASK_TOOL)
        if not permission.allowed:
            raise PermissionError(permission.reason)
        approvals = [self.cp._approval_from_row(row) for row in self.cp.store.list_approvals(task.task_run_id)]
        for approval in approvals:
            origin = approval.proposal.arguments.get("origin") or {}
            if (approval.proposal.operation_type != "task.create"
                    or origin.get("type") != "meeting" or origin.get("record_id") != record_id
                    or str(origin.get("revision_id")) != str(record["revision_id"])
                    or origin.get("document_id_hash") != record["document_id_hash"]
                    or approval.eligible_approver_ids != frozenset({member_id})
                    or approval.eligible_approver_roles):
                raise PermissionError("approval is not bound to this reviewed meeting")
        return record, task, approvals

    def preview(self, record_id: str, member_id: str) -> list[dict[str, Any]]:
        _, _, approvals = self._reviewed(record_id, member_id)
        items = []
        for approval in approvals:
            delivery = self.cp.store.get_approval_delivery(approval.approval_id)
            attempt = self.cp.store.get_approval_delivery_attempt(approval.approval_id)
            status = "SENT" if delivery else "UNKNOWN" if attempt else "NOT_SENT"
            items.append({"approval_id": approval.approval_id, "status": approval.status.value,
                          "card_status": status})
        return items

    def send_pending(self, record_id: str, member_id: str, recipient_open_id: str) -> list[dict[str, Any]]:
        record, task, approvals = self._reviewed(record_id, member_id)
        items = self.preview(record_id, member_id)
        pending = {item["approval_id"] for item in items
                   if item["status"] == "PENDING" and item["card_status"] == "NOT_SENT"}
        if not pending:
            return items
        if self.gateway is None or self.revision_reader is None:
            raise RuntimeError("card sender and revision reader are not configured")
        source = self.cp.store.get_meeting_source(record_id)
        if not source or str(source.get("revision_id")) != str(record["revision_id"]):
            raise ValueError("meeting source snapshot is missing or mismatched")
        expected = record["document_id_hash"]
        if len(expected) not in {12, 64} or hashlib.sha256(source["document_id"].encode()).hexdigest()[:len(expected)] != expected:
            raise PermissionError("meeting source document mismatch")
        try:
            revision = self.revision_reader.get_revision(source["document_id"])
        except Exception:
            self.cp._audit("APPROVAL_CARD_SOURCE_CHECK", self.tenant_id, member_id,
                           task.task_run_id, {"record_id": record_id}, "ERROR")
            raise
        result = "MATCH" if str(revision) == str(record["revision_id"]) else "STALE"
        self.cp._audit("APPROVAL_CARD_SOURCE_CHECK", self.tenant_id, member_id,
                       task.task_run_id, {"record_id": record_id}, result)
        if result == "STALE":
            raise ValueError("source document changed before card delivery")
        for approval in approvals:
            if approval.approval_id not in pending or approval.status != ApprovalStatus.PENDING:
                continue
            assignee = str(approval.proposal.arguments["assignee_actor_id"])
            try:
                self.gateway.send(
                    approval.approval_id, recipient_actor_id=member_id, recipient_open_id=recipient_open_id,
                    assignee_name="当前成员" if assignee == member_id else assignee,
                )
            except CardDeliveryUncertain:
                # Persisted send fences retain ambiguity; continue other proposals.
                continue
        return self.preview(record_id, member_id)


__all__ = ["MeetingApprovalBridge"]
