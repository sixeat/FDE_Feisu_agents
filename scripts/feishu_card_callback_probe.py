"""Minimal Feishu card callback probe for phase 0.

The probe uses the official lark-oapi dispatcher for challenge, decryption and
signature verification. It records only hashed identifiers and action metadata;
it never creates a task, writes a document, or prints callback payloads.
"""

from __future__ import annotations

import hashlib
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import lark_oapi as lark
from lark_oapi.core.const import (
    CONTENT_TYPE,
    LARK_REQUEST_NONCE,
    LARK_REQUEST_SIGNATURE,
    LARK_REQUEST_TIMESTAMP,
    X_REQUEST_ID,
)
from lark_oapi.core.model import RawRequest
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)


def short_hash(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _header(headers: dict[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None) if event else None
    operator_id = getattr(operator, "operator_id", None) if operator else None
    action = getattr(event, "action", None) if event else None
    action_tag = getattr(action, "tag", None) if action else None
    print(
        "CARD_CALLBACK_EVENT "
        f"event_id_hash={short_hash(getattr(data, 'event_id', None))} "
        f"operator_id_hash={short_hash(operator_id)} "
        f"action_tag_hash={short_hash(action_tag)}",
        flush=True,
    )
    return P2CardActionTriggerResponse(
        {"toast": {"type": "success", "content": "已收到测试卡片回调"}}
    )


ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
DISPATCHER = (
    lark.EventDispatcherHandler.builder(ENCRYPT_KEY, VERIFICATION_TOKEN)
    .register_p2_card_action_trigger(on_card_action)
    .build()
)


class CallbackHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP API name
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        raw = RawRequest()
        raw.uri = self.path
        raw.body = body
        raw.headers = {
            LARK_REQUEST_TIMESTAMP: _header(dict(self.headers.items()), LARK_REQUEST_TIMESTAMP),
            LARK_REQUEST_NONCE: _header(dict(self.headers.items()), LARK_REQUEST_NONCE),
            LARK_REQUEST_SIGNATURE: _header(dict(self.headers.items()), LARK_REQUEST_SIGNATURE),
            X_REQUEST_ID: _header(dict(self.headers.items()), X_REQUEST_ID),
        }
        response = DISPATCHER.do(raw)
        content = response.content or b""
        self.send_response(response.status_code or 500)
        self.send_header(CONTENT_TYPE, response.headers.get(CONTENT_TYPE, "application/json"))
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)
        print(
            "CARD_CALLBACK_HTTP "
            f"status={response.status_code} request_id_hash={short_hash(raw.headers.get(X_REQUEST_ID))}",
            flush=True,
        )

    def log_message(self, format: str, *args: Any) -> None:
        # Do not print request paths, headers, or payloads.
        return


def main() -> int:
    if not ENCRYPT_KEY or not VERIFICATION_TOKEN:
        print(
            "缺少 FEISHU_ENCRYPT_KEY 或 FEISHU_VERIFICATION_TOKEN；"
            "只在本机环境变量中设置，不要粘贴到聊天。",
            file=sys.stderr,
        )
        return 2
    host = os.environ.get("FEISHU_CALLBACK_HOST", "127.0.0.1")
    port = int(os.environ.get("FEISHU_CALLBACK_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), CallbackHandler)
    print(
        f"CARD_CALLBACK_PROBE_START host={host} port={port} path=/feishu/callback",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("CARD_CALLBACK_PROBE_STOP", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
