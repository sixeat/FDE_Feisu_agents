"""Verify a Feishu meeting review form before creating approval proposals."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from .approval_card import MEETING_REVIEW_FORM_ACTION
from .feishu_review import FeishuMeetingReviewGateway
from .meeting import meeting_review_snapshot, review_todo_ref
from .models import ActorType, IdentityContext, TaskRunStatus


def _attr(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_review_submitted_card(todo_count: int, review_url: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": False, "enable_forward": False},
        "header": {"title": {"tag": "plain_text", "content": "会议审核已提交"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content":
                f"已确认 {todo_count} 条待办并生成审批提案。若未收到审批卡片，请在审核页查看派送状态；当前尚未创建飞书任务。"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "查看会议审核"},
             "type": "default", "url": review_url},
        ]},
    }


def build_review_processing_card(review_url: str) -> dict[str, Any]:
    return {
        "schema": "2.0", "config": {"update_multi": False, "enable_forward": False},
        "header": {"title": {"tag": "plain_text", "content": "会议审核正在核对"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content":
                "已接收审核提交，正在核对成员权限和源文档版本。尚未创建飞书任务。"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "查看会议审核"},
             "type": "default", "url": review_url},
        ]},
    }


def build_review_rejected_card(review_url: str) -> dict[str, Any]:
    card = build_review_processing_card(review_url)
    card["header"]["title"]["content"] = "会议审核未通过核对"
    card["body"]["elements"][0]["text"]["content"] = (
        "本次提交未生成审批或飞书任务。请打开审核页查看当前草稿及源文档状态后重新处理。"
    )
    return card


class FeishuMeetingCardReviewGateway:
    """Only verified Card 2.0 callbacks from the delivered form may submit."""

    def __init__(self, meeting_gateway: FeishuMeetingReviewGateway, *, app_id: str,
                 tenant_id: str, tenant_key: str) -> None:
        self.meeting_gateway = meeting_gateway
        self.cp = meeting_gateway.control_plane
        self.app_id = app_id
        self.tenant_id = tenant_id
        self.tenant_key = tenant_key

    def enqueue(self, callback: Any) -> dict[str, Any]:
        """Validate callback binding and persist the form without a remote API call."""
        header, event = _attr(callback, "header"), _attr(callback, "event")
        action, operator = _attr(event, "action"), _attr(event, "operator")
        message_id = _attr(_attr(event, "context"), "open_message_id")
        open_id, values = _attr(operator, "open_id"), _attr(action, "form_value")
        if (_attr(header, "app_id") != self.app_id or _attr(operator, "tenant_key") != self.tenant_key
                or _attr(action, "tag") != "button" or _attr(action, "name") != MEETING_REVIEW_FORM_ACTION
                or not isinstance(message_id, str) or not message_id
                or not isinstance(open_id, str) or not open_id or not isinstance(values, dict)
                or len(values) > 6 or any(not isinstance(key, str) or not isinstance(value, str)
                                           or len(value) > 128 for key, value in values.items())):
            return {"accepted": False, "status": "INVALID_CALLBACK"}
        delivery = self.cp.store.get_assistant_card_delivery_by_message(message_id)
        if delivery is None or delivery["status"] != "SENT" or not delivery["review_snapshot"]:
            return {"accepted": False, "status": "UNBOUND_CARD"}
        record = self.cp.store.get_meeting_record(str(delivery["record_id"]))
        if (record is None or record["tenant_id"] != self.tenant_id
                or record["submitted_by"] != delivery["recipient_actor_id"]):
            return {"accepted": False, "status": "UNBOUND_RECORD"}
        try:
            actor = self.cp.verify_identity(IdentityContext(
                self.tenant_id, str(delivery["recipient_actor_id"]), "feishu_event", _hash(open_id),
            ))
            if actor.actor_type != ActorType.USER:
                raise PermissionError("review operator is not a user")
        except (KeyError, PermissionError):
            return {"accepted": False, "status": "UNBOUND_OPERATOR"}
        payload = {
            "header": {"app_id": self.app_id},
            "event": {"action": {"tag": "button", "name": MEETING_REVIEW_FORM_ACTION,
                                 "form_value": values},
                      "operator": {"open_id": open_id, "tenant_key": self.tenant_key},
                      "context": {"open_message_id": message_id}},
        }
        if len(json.dumps(payload, ensure_ascii=False)) > 4096:
            return {"accepted": False, "status": "INVALID_CALLBACK"}
        queued = self.cp.store.enqueue_meeting_card_review(message_id, self.tenant_id, payload)
        return {"accepted": True, "status": "QUEUED" if queued else "DUPLICATE"}

    def receive(self, callback: Any) -> dict[str, Any]:
        header, event = _attr(callback, "header"), _attr(callback, "event")
        action = _attr(event, "action")
        if _attr(header, "app_id") != self.app_id or _attr(action, "tag") != "button" or _attr(action, "name") != MEETING_REVIEW_FORM_ACTION:
            return {"accepted": False, "status": "INVALID_ACTION"}
        message_id = _attr(_attr(event, "context"), "open_message_id")
        operator = _attr(event, "operator")
        open_id = _attr(operator, "open_id")
        if _attr(operator, "tenant_key") != self.tenant_key:
            return {"accepted": False, "status": "TENANT_MISMATCH"}
        values = _attr(action, "form_value")
        if not isinstance(message_id, str) or not message_id or not isinstance(open_id, str) or not open_id or not isinstance(values, dict):
            return {"accepted": False, "status": "INVALID_CALLBACK"}
        store = self.cp.store
        delivery = store.get_assistant_card_delivery_by_message(message_id)
        if delivery is None or delivery["status"] != "SENT" or not delivery["review_snapshot"]:
            return {"accepted": False, "status": "UNBOUND_CARD"}
        record_id = str(delivery["record_id"])
        record = store.get_meeting_record(record_id)
        source = store.get_meeting_source(record_id)
        if (record is None or source is None or record["tenant_id"] != self.tenant_id
                or record["submitted_by"] != delivery["recipient_actor_id"]):
            return {"accepted": False, "status": "UNBOUND_RECORD"}
        try:
            actor = self.cp._actor(str(delivery["recipient_actor_id"]))
            identity = IdentityContext(record["tenant_id"], actor.actor_id, "feishu_event", _hash(open_id))
            self.cp.verify_identity(identity)
            if actor.actor_type != ActorType.USER or not actor.external_ref_hash:
                raise PermissionError("card operator is not a member")
            task = self.cp._task(record["task_run_id"])
            if task.tenant_id != actor.tenant_id:
                return {"accepted": False, "status": "STALE_REVIEW"}
            if task.status == TaskRunStatus.WAITING_APPROVAL:
                # A worker may restart after the proposal commit but before
                # acknowledging this inbox item. Never submit a second time.
                approvals = store.list_approvals(task.task_run_id)
                if approvals:
                    return {"accepted": True, "status": "ALREADY_SUBMITTED", "record_id": record_id,
                            "todo_count": len(store.list_meeting_todos(record_id)),
                            "approval_count": len(approvals)}
            if task.status != TaskRunStatus.WAITING_REVIEW:
                return {"accepted": False, "status": "STALE_REVIEW"}
            drafts = store.list_meeting_todos(record_id)
            current_snapshot = meeting_review_snapshot(record, task.status, drafts)
            if current_snapshot != delivery["review_snapshot"] or not 1 <= len(drafts) <= 3:
                return {"accepted": False, "status": "STALE_REVIEW"}
            expected = {key for index in range(len(drafts)) for key in (f"assignee_{index}", f"due_{index}")}
            if set(values) != expected:
                return {"accepted": False, "status": "INVALID_FIELDS"}
            members = {member["actor_id"] for member in store.list_bound_members(actor.tenant_id)}
            updates = {}
            for index, draft in enumerate(drafts):
                assignee, due = values[f"assignee_{index}"], values[f"due_{index}"]
                if not isinstance(assignee, str) or assignee not in members or not isinstance(due, str):
                    return {"accepted": False, "status": "INVALID_FIELDS"}
                if len(due) != 10 or date.fromisoformat(due).isoformat() != due:
                    return {"accepted": False, "status": "INVALID_FIELDS"}
                updates[review_todo_ref(draft["todo_id"])] = {
                    "assignee_actor_id": assignee, "due_date": due,
                }
            submission = self.meeting_gateway.submit(
                task_run_id=task.task_run_id, record_id=record_id,
                document_id=str(source["document_id"]), reviewer_identity=identity,
                updates_by_todo_ref=updates,
            )
            return {"accepted": True, "status": "WAITING_APPROVAL", "record_id": record_id,
                    "todo_count": len(drafts), "approval_count": len(submission.approval_ids)}
        except (KeyError, PermissionError, ValueError, RuntimeError):
            return {"accepted": False, "status": "REVIEW_REJECTED"}


__all__ = ["FeishuMeetingCardReviewGateway", "build_review_processing_card",
           "build_review_rejected_card", "build_review_submitted_card"]
