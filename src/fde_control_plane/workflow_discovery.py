"""Member-authorized, read-only discovery of repeated workflow signals."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from .models import ActorType, AuditEvent, IdentityContext
from .service import ControlPlane
from .user_document_access import (
    CHAT_GROUP_READ_SCOPE, CHAT_P2P_READ_SCOPE, UserDocumentAuthorizationRequired,
    UserTokenVault,
)

WORKFLOW_SOURCE_CONSENT_VERSION = "workflow-source-v2"


@dataclass(frozen=True)
class ChatMessage:
    message_id: str
    created_at: datetime
    text: str


class UserChatHistoryReader(Protocol):
    def list_messages(self, identity: IdentityContext, chat_id: str, chat_type: str,
                      start_at: datetime, end_at: datetime) -> list[ChatMessage]: ...


class FeishuUserChatHistoryReader:
    """Read bounded text messages with the subscribed member's user token."""

    def __init__(self, vault: UserTokenVault, client: httpx.Client | None = None, *,
                 api_origin: str = "https://open.feishu.cn", max_messages: int = 1000) -> None:
        if not api_origin.startswith("https://") or max_messages < 1 or max_messages > 5000:
            raise ValueError("chat reader configuration is invalid")
        self.vault = vault
        self.client = client or httpx.Client(timeout=12.0)
        self.api_origin = api_origin.rstrip("/")
        self.max_messages = max_messages

    def list_messages(self, identity: IdentityContext, chat_id: str, chat_type: str,
                      start_at: datetime, end_at: datetime) -> list[ChatMessage]:
        scope = CHAT_GROUP_READ_SCOPE if chat_type == "GROUP" else CHAT_P2P_READ_SCOPE
        token = self.vault.get(identity, required_scope=scope)
        start_epoch = int(start_at.timestamp())
        end_epoch = int(end_at.timestamp())
        if start_epoch >= end_epoch:
            raise ValueError("chat history window is invalid")
        page_token: str | None = None
        messages: list[ChatMessage] = []
        for _ in range((self.max_messages + 99) // 100):
            try:
                response = self.client.get(
                    f"{self.api_origin}/open-apis/im/v1/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "container_id_type": "chat", "container_id": chat_id,
                        "start_time": str(start_epoch), "end_time": str(end_epoch),
                        "sort_type": "ByCreateTimeAsc", "page_size": "100",
                        **({"page_token": page_token} if page_token else {}),
                    },
                )
            except httpx.HTTPError as exc:
                raise RuntimeError("FEISHU_CHAT_READ_TRANSPORT_ERROR") from exc
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("FEISHU_CHAT_READ_INVALID_RESPONSE") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("FEISHU_CHAT_READ_INVALID_RESPONSE")
            if response.status_code >= 400 or payload.get("code") != 0:
                code = payload.get("code", response.status_code)
                raise RuntimeError(f"FEISHU_CHAT_READ_{code}")
            data = payload.get("data") or {}
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise RuntimeError("FEISHU_CHAT_READ_INVALID_RESPONSE")
            for item in items:
                if not isinstance(item, dict):
                    raise RuntimeError("FEISHU_CHAT_READ_INVALID_RESPONSE")
                if item.get("msg_type") != "text":
                    continue
                body = item.get("body") or {}
                if not isinstance(body, dict):
                    raise RuntimeError("FEISHU_CHAT_READ_INVALID_RESPONSE")
                content = body.get("content") or "{}"
                try:
                    decoded = json.loads(content)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(decoded, dict):
                    continue
                text = decoded.get("text", "")
                if not isinstance(text, str) or not text.strip():
                    continue
                try:
                    created_ms = int(item.get("create_time"))
                    created_at = datetime.fromtimestamp(created_ms / 1000, timezone.utc)
                except (TypeError, ValueError, OSError):
                    raise RuntimeError("FEISHU_CHAT_READ_INVALID_TIMESTAMP")
                message_id = item.get("message_id")
                if not isinstance(message_id, str) or not message_id:
                    raise RuntimeError("FEISHU_CHAT_READ_MESSAGE_ID_MISSING")
                messages.append(ChatMessage(message_id, created_at, text))
                if len(messages) > self.max_messages:
                    raise RuntimeError("FEISHU_CHAT_READ_WINDOW_TOO_LARGE")
            if not data.get("has_more"):
                return messages
            page_token = data.get("page_token")
            if not page_token:
                raise RuntimeError("FEISHU_CHAT_READ_PAGE_TOKEN_MISSING")
        raise RuntimeError("FEISHU_CHAT_READ_PAGE_LIMIT")


class WorkflowDiscoveryService:
    def __init__(self, control_plane: ControlPlane, reader: UserChatHistoryReader | None,
                 *, token_vault: UserTokenVault | None = None,
                 clock=lambda: datetime.now(timezone.utc)) -> None:
        self.cp = control_plane
        self.store = control_plane.store
        self.reader = reader
        self.token_vault = token_vault
        self.clock = clock

    def _member(self, identity: IdentityContext, *, required_scope: str | None = None) -> None:
        actor = self.cp.verify_identity(identity)
        if identity.auth_mode != "user_oauth" or actor.actor_type != ActorType.USER:
            raise PermissionError("a member OAuth identity is required")
        if required_scope is not None and required_scope not in identity.scopes:
            raise PermissionError("member OAuth identity is missing chat-read permission")
        binding = self.store.find_actor_by_external_hash(identity.tenant_id, identity.subject_ref_hash)
        if binding is None or binding["actor_id"] != actor.actor_id:
            raise PermissionError("a bound Feishu member identity is required")

    def subscribe(self, identity: IdentityContext, chat_id: str, chat_type: str,
                  display_label: str, consent_confirmed: bool,
                  consent_version: str) -> dict[str, Any]:
        if consent_confirmed is not True:
            raise ValueError("explicit workflow source consent is required")
        if consent_version != WORKFLOW_SOURCE_CONSENT_VERSION:
            raise ValueError("workflow source consent changed; reload the page")
        if chat_type not in {"GROUP", "P2P"}:
            raise ValueError("Feishu chat type is invalid")
        required_scope = CHAT_GROUP_READ_SCOPE if chat_type == "GROUP" else CHAT_P2P_READ_SCOPE
        self._member(identity, required_scope=required_scope)
        chat_id = chat_id.strip()
        display_label = display_label.strip()
        if not re.fullmatch(r"oc_[A-Za-z0-9_-]{6,120}", chat_id):
            raise ValueError("Feishu chat id is invalid")
        if not 2 <= len(display_label) <= 80:
            raise ValueError("source label must contain 2 to 80 characters")
        subscription_id = "workflow-source-" + secrets.token_hex(10)
        self.store.create_workflow_subscription(
            subscription_id=subscription_id, tenant_id=identity.tenant_id,
            member_id=identity.actor_id, chat_id=chat_id, chat_type=chat_type,
            display_label=display_label,
            fingerprint_salt=secrets.token_hex(32),
            consent_version=consent_version,
        )
        self.cp._audit("WORKFLOW_SOURCE_SUBSCRIBED", identity.tenant_id, identity.actor_id, None,
                       {"subscription_id": subscription_id,
                        "source_hash": hashlib.sha256(chat_id.encode()).hexdigest(),
                        "consent_version": consent_version}, "ACTIVE")
        return {"subscription_id": subscription_id, "status": "ACTIVE"}

    def list_subscriptions(self, identity: IdentityContext) -> list[dict[str, Any]]:
        self._member(identity)
        return [dict(row) for row in self.store.list_workflow_subscriptions(
            identity.tenant_id, identity.actor_id,
        )]

    def renew_consent(self, identity: IdentityContext, subscription_id: str,
                      consent_confirmed: bool, consent_version: str) -> dict[str, Any]:
        if consent_confirmed is not True:
            raise ValueError("explicit workflow source consent is required")
        if consent_version != WORKFLOW_SOURCE_CONSENT_VERSION:
            raise ValueError("workflow source consent changed; reload the page")
        self._member(identity)
        subscription = self.store.get_workflow_subscription(
            subscription_id, identity.tenant_id, identity.actor_id,
        )
        if subscription is None or subscription["status"] != "ACTIVE":
            raise ValueError("active chat subscription was not found")
        if not self.store.renew_workflow_subscription_consent(
            subscription_id, identity.tenant_id, identity.actor_id, consent_version,
        ):
            raise ValueError("active chat subscription was not found")
        self.cp._audit("WORKFLOW_SOURCE_CONSENT_RENEWED", identity.tenant_id,
                       identity.actor_id, None,
                       {"subscription_id": subscription_id,
                        "consent_version": consent_version}, "ACTIVE")
        updated = self.store.get_workflow_subscription(
            subscription_id, identity.tenant_id, identity.actor_id,
        )
        return {"subscription_id": subscription_id, "status": "ACTIVE",
                "consent_version": updated["consent_version"],
                "consented_at": updated["consented_at"]}

    def chat_authorization_status(self, identity: IdentityContext) -> dict[str, bool]:
        self._member(identity)
        if self.token_vault is None:
            return {"group_chat": False, "p2p_chat": False}
        return self.token_vault.chat_authorization_status(identity)

    def revoke(self, identity: IdentityContext, subscription_id: str) -> dict[str, Any]:
        self._member(identity)
        source_hash = hashlib.sha256(subscription_id.encode()).hexdigest()
        revoke_event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="WORKFLOW_SOURCE_REVOKED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id, task_run_id=None,
            correlation_id=None, payload_hash=source_hash, outcome="REVOKED",
        )
        impact_event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12),
            event_type="WORKFLOW_AGENT_SOURCE_REVOKED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id, task_run_id=None,
            correlation_id=None, payload_hash=source_hash, outcome="STALE",
        )
        if not self.store.revoke_workflow_subscription(subscription_id, identity.tenant_id,
                                                       identity.actor_id, revoke_event,
                                                       impact_event):
            raise ValueError("active chat subscription was not found")
        return {"subscription_id": subscription_id, "status": "REVOKED"}

    def scan_recent(self, identity: IdentityContext, subscription_id: str) -> dict[str, Any]:
        subscription = self.store.get_workflow_subscription(
            subscription_id, identity.tenant_id, identity.actor_id,
        )
        if subscription is None or subscription["status"] != "ACTIVE":
            raise PermissionError("active member chat subscription is required")
        if subscription["consent_version"] != WORKFLOW_SOURCE_CONSENT_VERSION:
            self.cp._audit("WORKFLOW_DISCOVERY_SCAN_BLOCKED", identity.tenant_id,
                           identity.actor_id, None,
                           {"subscription_id": subscription_id,
                            "reason": "CONSENT_VERSION_OUTDATED"}, "BLOCKED")
            raise ValueError("读取说明已更新，请在原订阅中重新确认后再扫描。")
        required_scope = (CHAT_GROUP_READ_SCOPE if subscription["chat_type"] == "GROUP"
                          else CHAT_P2P_READ_SCOPE)
        self._member(identity, required_scope=required_scope)
        if self.reader is None:
            raise RuntimeError("member-authorized Feishu chat reader is not configured")
        end_at = self.clock().astimezone(timezone.utc)
        start_at = end_at - timedelta(hours=24)
        try:
            messages = self.reader.list_messages(identity, subscription["chat_id"],
                                                 subscription["chat_type"], start_at, end_at)
        except UserDocumentAuthorizationRequired as exc:
            self.cp._audit("WORKFLOW_DISCOVERY_SCAN_BLOCKED", identity.tenant_id,
                           identity.actor_id, None,
                           {"subscription_id": subscription_id,
                            "reason": "USER_AUTHORIZATION_REQUIRED"}, "BLOCKED")
            raise
        except RuntimeError as exc:
            error_code = str(exc)
            if not re.fullmatch(r"FEISHU_CHAT_READ_[A-Z0-9_]{1,64}", error_code):
                error_code = "FEISHU_CHAT_READ_FAILED"
            self.cp._audit("WORKFLOW_DISCOVERY_SCAN_FAILED", identity.tenant_id,
                           identity.actor_id, None,
                           {"subscription_id": subscription_id, "error_code": error_code},
                           "FAILED")
            raise RuntimeError(error_code) from exc
        grouped: dict[str, list[tuple[datetime, str]]] = {}
        salt = bytes.fromhex(subscription["fingerprint_salt"])
        for message in messages:
            if not start_at <= message.created_at.astimezone(timezone.utc) <= end_at:
                continue
            normalized = " ".join(unicodedata.normalize("NFKC", message.text).casefold().split())
            if len(normalized) < 12 or len(normalized) > 4000:
                continue
            fingerprint = hmac.new(salt, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
            evidence_ref = hmac.new(
                salt, f"{subscription_id}:{message.message_id}".encode(), hashlib.sha256,
            ).hexdigest()[:24]
            grouped.setdefault(fingerprint, []).append((message.created_at.astimezone(timezone.utc), evidence_ref))
        candidates = []
        for fingerprint, matches in grouped.items():
            unique = {ref: seen for seen, ref in matches}
            evidence = [
                {"evidence_ref": ref, "seen_at": seen.isoformat()}
                for ref, seen in sorted(unique.items(), key=lambda item: (item[1], item[0]))
            ]
            candidates.append({
                "opportunity_id": "workflow-opportunity-" + secrets.token_hex(10),
                "signature_hash": fingerprint,
                "first_seen": min(unique.values()).isoformat(),
                "last_seen": max(unique.values()).isoformat(),
                "evidence": evidence,
            })
        def check_authorization() -> None:
            self._member(identity, required_scope=required_scope)
            if self.token_vault is not None:
                self.token_vault.get(identity, required_scope=required_scope)

        try:
            rows = self.store.upsert_workflow_opportunities(
                tenant_id=identity.tenant_id, subscription_id=subscription_id,
                consent_version=WORKFLOW_SOURCE_CONSENT_VERSION, candidates=candidates,
                authorization_check=check_authorization,
            )
        except UserDocumentAuthorizationRequired:
            self.cp._audit("WORKFLOW_DISCOVERY_SCAN_BLOCKED", identity.tenant_id,
                           identity.actor_id, None,
                           {"subscription_id": subscription_id,
                            "reason": "USER_AUTHORIZATION_LOST_DURING_SCAN"}, "BLOCKED")
            raise
        except PermissionError:
            self.cp._audit("WORKFLOW_DISCOVERY_SCAN_BLOCKED", identity.tenant_id,
                           identity.actor_id, None,
                           {"subscription_id": subscription_id,
                            "reason": "MEMBER_AUTHORIZATION_CHANGED"}, "BLOCKED")
            raise
        created = [row["opportunity_id"] for row in rows if row["occurrence_count"] >= 2]
        self.cp._audit("WORKFLOW_DISCOVERY_SCAN", identity.tenant_id, identity.actor_id, None,
                       {"subscription_id": subscription_id, "window_hours": 24,
                        "message_count": len(messages), "candidate_count": len(created)}, "SUCCEEDED")
        return {"subscription_id": subscription_id, "window_hours": 24,
                "messages_scanned": len(messages), "opportunity_ids": sorted(set(created)),
                "raw_message_content_retained": False}

    def preview_evidence(self, identity: IdentityContext,
                         opportunity_id: str) -> dict[str, Any]:
        """Re-read matching evidence for its owner without retaining message text."""
        self._member(identity)
        opportunity = self.store.get_workflow_opportunity(
            opportunity_id, identity.tenant_id, identity.actor_id,
        )
        if opportunity is None:
            raise ValueError("workflow opportunity was not found")
        subscription = self.store.get_workflow_subscription(
            opportunity["subscription_id"], identity.tenant_id, identity.actor_id,
        )
        if subscription is None or subscription["status"] != "ACTIVE":
            raise PermissionError("active member chat subscription is required")
        if subscription["consent_version"] != WORKFLOW_SOURCE_CONSENT_VERSION:
            raise ValueError("读取说明已更新，请在原订阅中重新确认后再预览。")
        required_scope = (CHAT_GROUP_READ_SCOPE if subscription["chat_type"] == "GROUP"
                          else CHAT_P2P_READ_SCOPE)
        self._member(identity, required_scope=required_scope)
        if self.reader is None:
            raise RuntimeError("member-authorized Feishu chat reader is not configured")
        end_at = self.clock().astimezone(timezone.utc)
        start_at = end_at - timedelta(hours=24)
        try:
            messages = self.reader.list_messages(identity, subscription["chat_id"],
                                                 subscription["chat_type"], start_at, end_at)
        except UserDocumentAuthorizationRequired:
            raise
        except RuntimeError as exc:
            raise RuntimeError("FEISHU_CHAT_READ_FAILED") from exc
        salt = bytes.fromhex(subscription["fingerprint_salt"])
        known_refs = set(json.loads(opportunity["evidence_refs_json"]))
        matches = []
        for message in messages:
            when = message.created_at.astimezone(timezone.utc)
            if not start_at <= when <= end_at:
                continue
            normalized = " ".join(unicodedata.normalize("NFKC", message.text).casefold().split())
            signature = hmac.new(salt, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
            if signature != opportunity["signature_hash"]:
                continue
            evidence_ref = hmac.new(
                salt, f"{subscription['subscription_id']}:{message.message_id}".encode(),
                hashlib.sha256,
            ).hexdigest()[:24]
            if evidence_ref in known_refs:
                matches.append({"evidence_ref": evidence_ref, "created_at": when.isoformat(),
                                "excerpt": " ".join(message.text.split())[:200]})
        def check_authorization() -> None:
            self._member(identity, required_scope=required_scope)
            if self.token_vault is not None:
                self.token_vault.get(identity, required_scope=required_scope)

        self.store.verify_workflow_subscription_access(
            tenant_id=identity.tenant_id, subscription_id=subscription["subscription_id"],
            consent_version=WORKFLOW_SOURCE_CONSENT_VERSION,
            authorization_check=check_authorization,
        )
        selected = sorted(
            {sample["evidence_ref"]: sample for sample in matches}.values(),
            key=lambda sample: sample["created_at"], reverse=True,
        )[:3]
        self.cp._audit("WORKFLOW_EVIDENCE_PREVIEWED", identity.tenant_id,
                       identity.actor_id, None,
                       {"opportunity_id": opportunity_id, "sample_count": len(selected)},
                       "SUCCEEDED")
        return {"opportunity_id": opportunity_id, "samples": selected,
                "window_hours": 24, "persisted_message_text": False}

    def list_opportunities(self, identity: IdentityContext) -> list[dict[str, Any]]:
        self._member(identity)
        result = []
        for row in self.store.list_workflow_opportunities(identity.tenant_id, identity.actor_id):
            result.append({
                "opportunity_id": row["opportunity_id"], "status": row["status"],
                "source_label": row["display_label"],
                "source_active": row["source_status"] == "ACTIVE",
                "source_consent_current": row["source_consent_version"] == WORKFLOW_SOURCE_CONSENT_VERSION,
                "pattern_type": "EXACT_NORMALIZED_TEXT_REPEAT",
                "occurrence_count": row["occurrence_count"], "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "evidence_refs": json.loads(row["evidence_refs_json"]),
                "decision_reason": row["decision_reason"], "agent_draft_id": row["agent_draft_id"],
            })
        return result

    def decide(self, identity: IdentityContext, opportunity_id: str,
               decision: str, reason: str) -> dict[str, Any]:
        self._member(identity)
        if decision not in {"ACCEPTED", "REJECTED", "DEFERRED"}:
            raise ValueError("workflow opportunity decision is invalid")
        reason = reason.strip()
        if not 5 <= len(reason) <= 500:
            raise ValueError("decision reason must contain 5 to 500 characters")
        decision_id = "workflow-decision-" + secrets.token_hex(10)
        if not self.store.decide_workflow_opportunity(
            opportunity_id=opportunity_id, tenant_id=identity.tenant_id,
            member_id=identity.actor_id, decision_id=decision_id, decision=decision,
            reason=reason, decided_by=identity.actor_id,
        ):
            raise ValueError("discovered opportunity is unavailable or already decided")
        self.cp._audit("WORKFLOW_OPPORTUNITY_DECIDED", identity.tenant_id, identity.actor_id, None,
                       {"opportunity_id": opportunity_id, "decision": decision,
                        "decision_id": decision_id}, decision)
        return {"opportunity_id": opportunity_id, "status": decision}

    def create_agent_draft(self, identity: IdentityContext, opportunity_id: str,
                           name: str, description: str) -> dict[str, Any]:
        self._member(identity)
        name, description = name.strip(), description.strip()
        if not 2 <= len(name) <= 100 or not 1 <= len(description) <= 2000:
            raise ValueError("Agent name or description is invalid")
        opportunity = self.store.get_workflow_opportunity(
            opportunity_id, identity.tenant_id, identity.actor_id,
        )
        if opportunity is None or opportunity["status"] != "ACCEPTED":
            raise ValueError("only an accepted workflow opportunity can create an Agent draft")
        draft_id = "agent-draft-" + secrets.token_hex(10)
        data = {
            "name": name, "actor_type": "BUSINESS_AGENT", "description": description,
            "capabilities": [], "skill_version": None, "base_version": None,
            "workflow_opportunity_id": opportunity_id,
            "workflow_evidence_refs": json.loads(opportunity["evidence_refs_json"]),
            "workflow_occurrence_count": opportunity["occurrence_count"],
            "requested_tool_policy": {"allowed_tools": []},
        }
        if not self.store.link_workflow_opportunity_draft(
            opportunity_id=opportunity_id, tenant_id=identity.tenant_id,
            member_id=identity.actor_id, draft_id=draft_id, draft_data=data,
        ):
            raise ValueError("accepted workflow opportunity changed before Agent draft creation")
        self.cp._audit("AGENT_CONFIG_DRAFT_CREATED_FROM_WORKFLOW", identity.tenant_id,
                       identity.actor_id, None,
                       {"draft_id": draft_id, "opportunity_id": opportunity_id,
                        "capability_count": 0}, "PENDING_REVIEW")
        return {"draft_id": draft_id, "opportunity_id": opportunity_id,
                "name": name, "status": "PENDING_REVIEW", "remote_write": False}


__all__ = ["CHAT_GROUP_READ_SCOPE", "CHAT_P2P_READ_SCOPE", "WORKFLOW_SOURCE_CONSENT_VERSION",
           "ChatMessage", "FeishuUserChatHistoryReader",
           "UserChatHistoryReader", "WorkflowDiscoveryService"]
