"""Bound personal-assistant intake for verified Feishu message events."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from .approval_card import build_meeting_intake_card, build_meeting_intake_failure_card
from .feishu_review import FeishuMeetingReviewGateway
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


class FeishuPersonalAssistantGateway:
    """Accept one member's p2p docx link and delegate to the meeting Agent.

    The caller must be the official SDK's verified event dispatcher. No raw
    webhook body or H5-supplied identity is accepted by this boundary.
    """

    def __init__(
        self, meeting_gateway: FeishuMeetingReviewGateway, *, app_id: str,
        tenant_key: str, tenant_id: str, sender: AssistantMessageSender | None = None,
        review_url: str | None = None,
    ) -> None:
        self.meeting_gateway = meeting_gateway
        self.control_plane = meeting_gateway.control_plane
        self.app_id = app_id
        self.tenant_key = tenant_key
        self.tenant_id = tenant_id
        self.sender = sender
        self.review_url = review_url

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
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fde:assistant-intake:{result['record_id']}"))
        if not store.claim_assistant_card_delivery(result["record_id"], identity.actor_id, key):
            prior = store.get_assistant_card_delivery(result["record_id"])
            response["card_status"] = "SENT" if prior["status"] == "SENT" else "UNKNOWN"
            return response
        todos = self.control_plane.store.list_meeting_todos(result["record_id"])
        card = build_meeting_intake_card(
            title=str(result["title"]), todos=todos, review_url=self.review_url,
        )
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
