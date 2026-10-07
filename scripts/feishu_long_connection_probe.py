"""Feishu long-connection event probe for phase 0.

This is a disposable verification helper, not the product runtime. It prints
only event type and lengths plus short hashes of remote identifiers. It never
prints or stores app secrets, message content, or full user/chat IDs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from typing import Any

import lark_oapi as lark

_APP_ID = ""
_APP_SECRET = ""


def short_hash(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def capture_tenant_key(value: str | None) -> None:
    """Optionally save the tenant key locally for one-time deployment setup."""
    if os.environ.get("FEISHU_CAPTURE_TENANT_KEY") != "1" or not value:
        return
    capture_path = os.environ.get("FEISHU_TENANT_KEY_FILE", "data/feishu_tenant_key.txt")
    path = os.path.abspath(capture_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value)


def on_message(data: Any) -> None:
    header = getattr(data, "header", None)
    capture_tenant_key(getattr(header, "tenant_key", None))
    event = getattr(data, "event", None)
    message = getattr(event, "message", None) if event else None
    sender = getattr(event, "sender", None) if event else None
    sender_id = getattr(sender, "sender_id", None) if sender else None
    open_id = getattr(sender_id, "open_id", None) if sender_id else None

    record = {
        "event": "im.message.receive_v1",
        "event_id_hash": short_hash(
            getattr(header, "event_id", None)
        ),
        "message_id_hash": short_hash(getattr(message, "message_id", None)),
        "chat_id_hash": short_hash(getattr(message, "chat_id", None)),
        "sender_id_hash": short_hash(open_id),
        "chat_type": getattr(message, "chat_type", None),
        "message_type": getattr(message, "message_type", None),
        "content_length": len(getattr(message, "content", None) or ""),
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)

    if os.environ.get("FEISHU_PROBE_REPLY") != "1":
        return
    if getattr(sender, "sender_type", None) == "app":
        return
    message_id = getattr(message, "message_id", None)
    if not message_id:
        print("PROBE_REPLY_SKIPPED: message_id_missing", flush=True)
        return

    try:
        client = lark.Client.builder().app_id(_APP_ID).app_secret(_APP_SECRET).build()
        body = (
            lark.api.im.v1.model.ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": "已收到测试消息（Phase0 探针回复）"}, ensure_ascii=False))
            .reply_in_thread(False)
            .uuid(str(uuid.uuid5(uuid.NAMESPACE_URL, message_id)))
            .build()
        )
        request = (
            lark.api.im.v1.model.ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response = client.im.v1.message.reply(request)
        print(
            f"PROBE_REPLY_RESULT: code={getattr(response, 'code', None)} "
            f"msg={getattr(response, 'msg', None)}",
            flush=True,
        )
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"PROBE_REPLY_ERROR: {type(exc).__name__}: {str(exc)[:160]}",
            flush=True,
        )


def main() -> int:
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print(
            "缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET；只在本机环境变量中设置，不要粘贴到聊天。",
            file=sys.stderr,
        )
        return 2

    global _APP_ID, _APP_SECRET
    _APP_ID = app_id
    _APP_SECRET = app_secret

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    print(
        "LONG_CONNECTION_PROBE_START: 正在建立长连接；按 Ctrl+C 停止。",
        flush=True,
    )
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )
    client.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
