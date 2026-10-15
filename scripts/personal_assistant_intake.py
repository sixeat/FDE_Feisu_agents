"""Queue verified Feishu p2p messages, then run the meeting Agent off-callback.

The WebSocket callback only persists a bounded document-link event. A separate
worker connection processes it; remote task writes are never performed here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane import (
    AgentOrchestrator, ControlPlane, FeishuCardSender,
    FeishuDocumentRevisionReader, FeishuMeetingReviewGateway,
    FeishuPersonalAssistantGateway, HermesRuntimeAdapter, PersonalAssistantMessage, SQLiteStore,
)
from fde_control_plane.user_document_access import FeishuUserDocumentReader, UserTokenVault


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def enqueue_callback(store: SQLiteStore, callback: object, *, app_id: str,
                     tenant_key: str, tenant_id: str) -> str:
    header = getattr(callback, "header", None)
    if getattr(header, "app_id", None) != app_id:
        return "APP_MISMATCH"
    try:
        event = PersonalAssistantMessage.from_callback(callback)
    except ValueError:
        return "INVALID_EVENT"
    if event.tenant_key != tenant_key:
        return "TENANT_MISMATCH"
    if event.sender_type != "user" or event.chat_type != "p2p" or event.message_type != "text":
        return "IGNORED"
    links = event.document_links()
    if len(links) != 1:
        return "IGNORED" if not links else "MULTIPLE_LINKS"
    message_hash = _hash(f"{tenant_id}:{event.message_id}")
    payload = {
        "header": {"app_id": app_id, "tenant_key": tenant_key, "event_id": event.event_id},
        "event": {
            "sender": {"sender_type": event.sender_type, "sender_id": {"open_id": event.sender_open_id}},
            "message": {"message_id": event.message_id, "chat_type": event.chat_type,
                        "message_type": "text", "content": json.dumps({"text": links[0]})},
        },
    }
    return "QUEUED" if store.enqueue_assistant_message(message_hash, tenant_id, payload) else "DUPLICATE"


def process_one(*, db_path: str, tenant_id: str, tenant_key: str,
                app_id: str, client: object, review_url: str,
                event_access_verifier: object | None = None,
                token_key: str | None = None, user_client: object | None = None,
                card_review_enabled: bool = False) -> str:
    with SQLiteStore(db_path) as store:
        if event_access_verifier is None and token_key and user_client is not None:
            event_access_verifier = FeishuUserDocumentReader(UserTokenVault(store, token_key), user_client)
        if event_access_verifier is None:
            raise RuntimeError("assistant worker requires a user-scoped document reader")
        token = uuid.uuid4().hex
        item = store.claim_assistant_message(tenant_id, token)
        if item is None:
            return "EMPTY"
        cp = ControlPlane(store)
        gateway = FeishuPersonalAssistantGateway(
            FeishuMeetingReviewGateway(
                cp, AgentOrchestrator(cp, HermesRuntimeAdapter()),
                FeishuDocumentRevisionReader(client),
                event_access_verifier,
            ), app_id=app_id, tenant_key=tenant_key, tenant_id=tenant_id,
            sender=FeishuCardSender(client), review_url=review_url,
            card_review_enabled=card_review_enabled,
        )
        try:
            result = gateway.receive(item["data"])
            status = "DONE" if result.get("card_status") == "SENT" else (
                "UNKNOWN" if result.get("task_status") in {"RUNNING", "RECONCILING"}
                or result.get("card_status") == "UNKNOWN" else "REJECTED"
            )
            summary = {"status": result.get("status"), "task_status": result.get("task_status"),
                       "record_id": result.get("record_id"), "card_status": result.get("card_status")}
        except Exception as exc:
            status = "UNKNOWN"
            summary = {"status": "PROCESSING_EXCEPTION", "error_type": type(exc).__name__}
        store.finish_assistant_message(item["message_hash"], token, status, summary)
        print(f"ASSISTANT_MESSAGE status={status} message_hash={item['message_hash'][:12]} "
              f"card={summary.get('card_status', 'NONE')} task_write=0", flush=True)
        return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["once"])
    parser.add_argument("--db", default=os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3"))
    args = parser.parse_args()
    app_id, secret = _required("FEISHU_APP_ID"), _required("FEISHU_APP_SECRET")
    tenant_key = _required("FEISHU_TENANT_KEY")
    tenant_id = os.environ.get("FDE_TENANT_ID", "sixeat")
    review_url = _required("FDE_ASSISTANT_REVIEW_URL")
    import lark_oapi as lark

    client = lark.Client.builder().app_id(app_id).app_secret(secret).timeout(12).build()
    token_key = os.environ.get("FDE_USER_TOKEN_KEY", "").strip()
    user_client = (lark.Client.builder().app_id(app_id).app_secret(secret).timeout(12)
                   .enable_set_token(True).build()) if token_key else None
    process_one(db_path=args.db, tenant_id=tenant_id, tenant_key=tenant_key,
                app_id=app_id, client=client, review_url=review_url,
                token_key=token_key, user_client=user_client,
                card_review_enabled=os.environ.get("FDE_ASSISTANT_CARD_REVIEW_ENABLED") == "1")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
