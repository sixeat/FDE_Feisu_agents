"""Bind Feishu approval cards and callbacks to durable control-plane approvals."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Protocol

from .approval_card import build_meeting_approval_card, build_meeting_decided_card, handle_meeting_approval_action
from .models import ActorType, ApprovalStatus, IdentityContext
from .service import ControlPlane


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CardSender(Protocol):
    def send(self, open_id: str, card: dict[str, Any], idempotency_key: str) -> str: ...
    def update(self, message_id: str, card: dict[str, Any]) -> None: ...


class CardDeliveryUncertain(RuntimeError):
    """A prior send may have reached Feishu; do not resend without reconciliation."""


class FeishuCardSender:
    def __init__(self, client: Any) -> None:
        self.client = client

    def send(self, open_id: str, card: dict[str, Any], idempotency_key: str) -> str:
        import lark_oapi as lark

        body = (
            lark.api.im.v1.model.CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .uuid(idempotency_key)
            .build()
        )
        request = (
            lark.api.im.v1.model.CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(body)
            .build()
        )
        response = self.client.im.v1.message.create(request)
        if getattr(response, "code", None) != 0:
            raise RuntimeError(f"FEISHU_CARD_SEND_{getattr(response, 'code', 'UNKNOWN')}")
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not message_id:
            raise TimeoutError("Feishu card send returned no message id")
        return str(message_id)

    def update(self, message_id: str, card: dict[str, Any]) -> None:
        import lark_oapi as lark

        body = (
            lark.api.im.v1.model.PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = (
            lark.api.im.v1.model.PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response = self.client.im.v1.message.patch(request)
        if getattr(response, "code", None) != 0:
            raise RuntimeError(f"FEISHU_CARD_UPDATE_{getattr(response, 'code', 'UNKNOWN')}")


class FeishuApprovalGateway:
    def __init__(self, control_plane: ControlPlane, sender: CardSender, *, app_id: str) -> None:
        self.control_plane = control_plane
        self.sender = sender
        self.app_id = app_id

    def send(self, approval_id: str, *, recipient_actor_id: str,
             recipient_open_id: str, assignee_name: str,
             approve_label: str = "批准创建任务") -> str:
        row = self.control_plane.store.get_approval(approval_id)
        if row is None:
            raise KeyError(approval_id)
        approval = self.control_plane._approval_from_row(row)
        actor = self.control_plane._actor(recipient_actor_id)
        task = self.control_plane._task(approval.task_run_id)
        bound = self.control_plane.store.find_actor_by_external_hash(actor.tenant_id, _hash(recipient_open_id))
        if (approval.status != ApprovalStatus.PENDING
                or recipient_actor_id not in approval.eligible_approver_ids
                or actor.actor_type != ActorType.USER
                or not actor.active
                or actor.tenant_id != task.tenant_id
                or not actor.external_ref_hash
                or actor.external_ref_hash != _hash(recipient_open_id)
                or bound is None or bound["actor_id"] != recipient_actor_id):
            raise PermissionError("card recipient is not the bound meeting approver")
        prior = self.control_plane.store.get_approval_delivery(approval_id)
        if prior is not None:
            if prior["recipient_actor_id"] != recipient_actor_id or prior["proposal_hash"] != approval.proposal.proposal_hash:
                raise ValueError("approval delivery conflicts with current proposal")
            return str(prior["message_id"])
        card = build_meeting_approval_card(
            approval, assignee_name=assignee_name, approve_label=approve_label
        )
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fde:meeting-approval:{approval_id}:{approval.proposal.proposal_hash}"))
        store = self.control_plane.store
        if not store.claim_approval_delivery(approval_id, recipient_actor_id, approval.proposal.proposal_hash, key):
            prior = store.get_approval_delivery(approval_id)
            if prior is not None and prior["recipient_actor_id"] == recipient_actor_id and prior["proposal_hash"] == approval.proposal.proposal_hash:
                return str(prior["message_id"])
            raise CardDeliveryUncertain("card delivery requires reconciliation before another send")
        try:
            message_id = self.sender.send(recipient_open_id, card, key)
            if not message_id:
                raise TimeoutError("card send returned no message id")
            store.save_approval_delivery(approval_id, message_id, recipient_actor_id, approval.proposal.proposal_hash)
            store.finish_approval_delivery_attempt(approval_id, "SENT")
        except Exception as exc:
            store.finish_approval_delivery_attempt(approval_id, "UNKNOWN")
            self.control_plane._audit(
                "APPROVAL_CARD_DELIVERY", task.tenant_id, recipient_actor_id, task.task_run_id,
                {"approval_id": approval_id, "error_type": type(exc).__name__}, "UNKNOWN",
            )
            raise CardDeliveryUncertain("card delivery result is unknown; reconcile before resending") from exc
        self.control_plane._audit(
            "APPROVAL_CARD_DELIVERY", task.tenant_id, recipient_actor_id, task.task_run_id,
            {"approval_id": approval_id}, "SENT",
        )
        return message_id

    def receive(self, callback: Any) -> dict[str, Any]:
        """Call only from Feishu's verified event dispatcher/long connection."""
        header = getattr(callback, "header", None)
        event = getattr(callback, "event", None)
        context = getattr(event, "context", None)
        operator = getattr(event, "operator", None)
        action = getattr(event, "action", None)
        value = getattr(action, "value", None)
        if not header or getattr(header, "app_id", None) != self.app_id:
            return {"accepted": False, "reason": "callback app mismatch"}
        if getattr(action, "tag", None) != "button" or not isinstance(value, dict):
            return {"accepted": False, "reason": "invalid card action"}
        approval_id = value.get("approval_id")
        message_id = getattr(context, "open_message_id", None)
        open_id = getattr(operator, "open_id", None)
        if not isinstance(approval_id, str) or not message_id or not open_id:
            return {"accepted": False, "reason": "callback identity or message missing"}
        delivery = self.control_plane.store.get_approval_delivery(approval_id)
        if delivery is None or delivery["message_id"] != message_id:
            return {"accepted": False, "reason": "card message is not bound to approval"}
        actor = self.control_plane._actor(delivery["recipient_actor_id"])
        if not actor.external_ref_hash or actor.external_ref_hash != _hash(open_id):
            return {"accepted": False, "reason": "card operator is not the recipient"}
        approval_row = self.control_plane.store.get_approval(approval_id)
        if approval_row is None:
            return {"accepted": False, "reason": "unknown approval"}
        approval = self.control_plane._approval_from_row(approval_row)
        if delivery["proposal_hash"] != approval.proposal.proposal_hash:
            return {"accepted": False, "reason": "card proposal changed"}
        event_id = getattr(header, "event_id", None) or _hash(
            json.dumps([message_id, open_id, value], ensure_ascii=True, sort_keys=True)
        )
        identity = IdentityContext(
            self.control_plane._task(approval.task_run_id).tenant_id,
            actor.actor_id, "application", _hash(open_id),
        )
        try:
            return handle_meeting_approval_action(
                self.control_plane, event_id=event_id, verified_operator=identity, value=value
            )
        except (KeyError, PermissionError, ValueError):
            return {"accepted": False, "reason": "invalid or unauthorized approval action"}

    def update_decided_card(self, approval_id: str, *, assignee_name: str,
                            test_mode: bool = False) -> None:
        row = self.control_plane.store.get_approval(approval_id)
        delivery = self.control_plane.store.get_approval_delivery(approval_id)
        if row is None or delivery is None:
            raise KeyError(approval_id)
        approval = self.control_plane._approval_from_row(row)
        card = build_meeting_decided_card(
            approval, assignee_name=assignee_name, test_mode=test_mode
        )
        self.sender.update(str(delivery["message_id"]), card)


__all__ = ["CardDeliveryUncertain", "FeishuApprovalGateway", "FeishuCardSender"]
