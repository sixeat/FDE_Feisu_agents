"""Bound personal-assistant intake for verified Feishu message events."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from .approval_card import (
    build_meeting_intake_card, build_meeting_intake_failure_card, build_meeting_review_form_card,
)
from .feishu_review import FeishuMeetingReviewGateway
from .meeting import meeting_review_snapshot
from .models import ActorType, IdentityContext
from .user_document_access import UserDocumentAuthorizationRequired


_DOC_LINK = re.compile(
    r"https://[A-Za-z0-9.-]+\.feishu\.cn/docx/[A-Za-z0-9_-]+(?:\?[^\s<>]*)?",
    re.IGNORECASE,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _attr(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


@dataclass(frozen=True)
class PersonalAssistantMessage:
    event_id: str
    tenant_key: str
    message_id: str
    chat_type: str
    message_type: str
    sender_open_id: str
    sender_type: str
    text: str

    @classmethod
    def from_callback(cls, callback: Any) -> "PersonalAssistantMessage":
        header = _attr(callback, "header")
        event = _attr(callback, "event")
        message = _attr(event, "message")
        sender = _attr(event, "sender")
        sender_id = _attr(sender, "sender_id")
        content = _attr(message, "content")
        try:
            decoded = json.loads(content) if isinstance(content, str) else content
        except ValueError as exc:
            raise ValueError("invalid Feishu message content") from exc
        values = {
            "event_id": _attr(header, "event_id"),
            "tenant_key": _attr(header, "tenant_key"),
            "message_id": _attr(message, "message_id"),
            "chat_type": _attr(message, "chat_type"),
            "message_type": _attr(message, "message_type"),
            "sender_open_id": _attr(sender_id, "open_id"),
            "sender_type": _attr(sender, "sender_type"),
        }
        if not all(isinstance(value, str) and value for value in values.values()):
            raise ValueError("Feishu message identity is incomplete")
        if values["message_type"] == "text":
            if not isinstance(decoded, dict) or not isinstance(decoded.get("text"), str):
                raise ValueError("Feishu text message is invalid")
            text = decoded["text"]
        else:
            text = ""
        return cls(**values, text=text)

    def document_links(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_DOC_LINK.findall(self.text)))


class AssistantMessageSender(Protocol):
    def send(self, open_id: str, card: dict[str, Any], idempotency_key: str) -> str: ...


def _form_review_todos(todos: list[dict[str, Any]], source: dict[str, Any] | None,
                       members: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Restrict inline edits to complete evidence and field-only uncertainty."""
    if not source or not 1 <= len(todos) <= 3 or not 1 <= len(members) <= 30:
        return None
    field_reasons = {
        "assignee_unmapped", "assignee_missing", "assignee_ambiguous",
        "due_date_unconfirmed", "date_missing", "date_ambiguous",
        "due_date_ambiguous", "relative_date_not_normalized",
    }
    evidence = {int(item["block_id"]): str(item["text"]) for item in source.get("evidence", [])}
    member_ids = {str(member["actor_id"]) for member in members}
    prepared = []
    for todo in todos:
        reasons = [str(item).lower() for item in todo.get("confirmation_reasons") or []]
        if (todo.get("status") == "DISCARDED" or not str(todo.get("title") or "").strip()
                or len(str(todo["title"])) > 200
                or todo.get("visibility_scope") != "assignee"
                or (todo.get("assignee_actor_id") and todo["assignee_actor_id"] not in member_ids)
                or any(reason not in field_reasons for reason in reasons)):
            return None
        block_ids = todo.get("evidence_block_ids") or []
        snippets = [evidence.get(int(block_id), "").strip() for block_id in block_ids]
        if not snippets or any(not snippet for snippet in snippets):
            return None
        due = str(todo.get("due_date") or "")
        try:
            due = due if len(due) == 10 and date.fromisoformat(due).isoformat() == due else ""
        except ValueError:
            due = ""
        prepared.append({**todo, "due_date": due or None, "evidence": "；".join(snippets)[:180]})
    return prepared


class FeishuPersonalAssistantGateway:
    """Accept one member's p2p docx link and delegate to the meeting Agent.

    The caller must be the official SDK's verified event dispatcher. No raw
    webhook body or H5-supplied identity is accepted by this boundary.
    """

    def __init__(
        self, meeting_gateway: FeishuMeetingReviewGateway, *, app_id: str,
        tenant_key: str, tenant_id: str, sender: AssistantMessageSender | None = None,
        review_url: str | None = None, card_review_enabled: bool = False,
    ) -> None:
        self.meeting_gateway = meeting_gateway
        self.control_plane = meeting_gateway.control_plane
        self.app_id = app_id
        self.tenant_key = tenant_key
        self.tenant_id = tenant_id
        self.sender = sender
        self.review_url = review_url
        self.card_review_enabled = card_review_enabled

    def receive(self, callback: Any) -> dict[str, Any]:
        header = _attr(callback, "header")
        if _attr(header, "app_id") != self.app_id:
            return {"accepted": False, "status": "APP_MISMATCH"}
        try:
            event = PersonalAssistantMessage.from_callback(callback)
        except ValueError:
            return {"accepted": False, "status": "INVALID_EVENT"}
        if event.tenant_key != self.tenant_key:
            return {"accepted": False, "status": "TENANT_MISMATCH"}
        if event.sender_type != "user" or event.chat_type != "p2p":
            return {"accepted": False, "status": "UNSUPPORTED_SENDER_OR_CHAT"}
        if event.message_type != "text":
            return {"accepted": True, "status": "IGNORED_MESSAGE_TYPE"}
        links = event.document_links()
        if len(links) != 1:
            return {"accepted": True, "status": "NO_DOCUMENT_LINK" if not links else "MULTIPLE_DOCUMENT_LINKS"}
        actor = self.control_plane.store.find_actor_by_external_hash(self.tenant_id, _hash(event.sender_open_id))
        if actor is None or actor["actor_type"] != ActorType.USER.value or not actor["active"]:
            return {"accepted": False, "status": "MEMBER_NOT_ONBOARDED"}
        identity = IdentityContext(
            self.tenant_id, str(actor["actor_id"]), "feishu_event", _hash(event.sender_open_id),
        )
        try:
            result = self.meeting_gateway.start_meeting(document_url=links[0], requester_identity=identity)
        except UserDocumentAuthorizationRequired:
            return self._notify_failure(event, identity, authorization_required=True)
        except (KeyError, PermissionError, ValueError, RuntimeError):
            return self._notify_failure(event, identity, authorization_required=False)
        response = {"accepted": True, "status": "MEETING_STARTED", **result}
        if not result["created"] and result.get("task_status") == "FAILED":
            return {**response, **self._notify_failure(event, identity, authorization_required=False)}
        if not result["created"] or self.sender is None or not self.review_url:
            return response
        store = self.control_plane.store
        todos = store.list_meeting_todos(result["record_id"])
        source = store.get_meeting_source(result["record_id"])
        directory = store.list_bound_members(self.tenant_id)
        candidates = _form_review_todos(todos, source, directory) if self.card_review_enabled else None
        snapshot = None
        if candidates is not None:
            names = [str(member["name"] or member["actor_id"]) for member in directory]
            labels = [{"actor_id": str(member["actor_id"]),
                       "name": name if names.count(name) == 1 else f"{name} ({member['actor_id']})"}
                      for member, name in zip(directory, names)]
            card = build_meeting_review_form_card(
                title=str(result["title"]), todos=candidates, members=labels,
                review_url=self.review_url,
            )
            record = store.get_meeting_record(result["record_id"])
            task = store.get_task_run(result["task_run_id"])
            snapshot = meeting_review_snapshot(record, task["status"], todos)
            response["card_mode"] = "FORM"
        else:
            card = build_meeting_intake_card(
                title=str(result["title"]), todos=todos, review_url=self.review_url,
            )
            response["card_mode"] = "H5_REVIEW"
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fde:assistant-intake:{result['record_id']}"))
        if not store.claim_assistant_card_delivery(result["record_id"], identity.actor_id, key, snapshot):
            prior = store.get_assistant_card_delivery(result["record_id"])
            response["card_status"] = "SENT" if prior["status"] == "SENT" else "UNKNOWN"
            return response
        try:
            card_message_id = self.sender.send(event.sender_open_id, card, key)
            store.finish_assistant_card_delivery(result["record_id"], status="SENT", message_id=card_message_id)
            response["card_message_id"] = card_message_id
            response["card_status"] = "SENT"
        except Exception:
            store.finish_assistant_card_delivery(result["record_id"], status="UNKNOWN")
            response["card_status"] = "UNKNOWN"
        return response

    def _notify_failure(self, event: PersonalAssistantMessage, identity: IdentityContext,
                        *, authorization_required: bool) -> dict[str, Any]:
        response: dict[str, Any] = {
            "accepted": False,
            "status": "DOCUMENT_AUTH_REQUIRED" if authorization_required else "MEETING_INTAKE_FAILED",
        }
        if self.sender is None:
            return response
        key = "assistant-intake-failure:" + _hash(f"{identity.tenant_id}:{event.message_id}")
        delivery_key = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        store = self.control_plane.store
        if not store.claim_notification(
            key, tenant_id=identity.tenant_id, recipient_actor_id=identity.actor_id,
            channel="FEISHU_CARD", claim_token=delivery_key,
            data={"reason": response["status"]},
        ):
            prior = store.get_notification(key)
            if (prior is None or prior["tenant_id"] != identity.tenant_id
                    or prior["recipient_actor_id"] != identity.actor_id
                    or prior["channel"] != "FEISHU_CARD"):
                raise ValueError("assistant failure notice conflicts with prior recipient")
            response["card_status"] = "SENT" if prior and prior["status"] == "SENT" else "UNKNOWN"
            return response
        authorization_url = None
        if authorization_required and self.review_url:
            parsed = urlsplit(self.review_url)
            if parsed.scheme == "https" and parsed.hostname:
                authorization_url = urlunsplit((parsed.scheme, parsed.netloc, "/oauth/start", "", ""))
        card = build_meeting_intake_failure_card(
            authorization_required=authorization_required, authorization_url=authorization_url,
        )
        try:
            message_id = self.sender.send(event.sender_open_id, card, delivery_key)
            if not message_id:
                raise TimeoutError("Feishu failure card returned no message id")
            store.complete_notification(key, delivery_key, "SENT", {"message_id": message_id})
            response["card_status"] = "SENT"
        except Exception:
            store.complete_notification(key, delivery_key, "UNKNOWN")
            response["card_status"] = "UNKNOWN"
        return response


__all__ = ["AssistantMessageSender", "FeishuPersonalAssistantGateway", "PersonalAssistantMessage"]
