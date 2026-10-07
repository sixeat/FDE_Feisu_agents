"""Read-only Feishu file-message download probe for phase 0.

This helper validates the file-message path without persisting the uploaded
file. It prints only short hashes, byte length, and API status. The downloaded
bytes remain in memory and are discarded after the callback returns.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any

import lark_oapi as lark


def short_hash(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:12]


def on_message(data: Any) -> None:
    event = getattr(data, "event", None)
    message = getattr(event, "message", None) if event else None
    if getattr(message, "message_type", None) != "file":
        return

    raw_content = getattr(message, "content", None) or ""
    try:
        content = json.loads(raw_content)
    except (TypeError, ValueError):
        print("FILE_DOWNLOAD_PARSE_ERROR: content_not_json", flush=True)
        return

    file_key = content.get("file_key")
    message_id = getattr(message, "message_id", None)
    if not file_key or not message_id:
        print(
            "FILE_DOWNLOAD_SKIPPED: missing_file_key_or_message_id",
            flush=True,
        )
        return

    print(
        "FILE_MESSAGE_RECEIVED"
        f" message_id_hash={short_hash(message_id)}"
        f" file_key_hash={short_hash(file_key)}",
        flush=True,
    )

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("FILE_DOWNLOAD_SKIPPED: missing_local_credentials", flush=True)
        return

    try:
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        request = (
            lark.api.im.v1.GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type("file")
            .build()
        )
        response = client.im.v1.message_resource.get(request)
        if getattr(response, "code", None) != 0:
            print(
                "FILE_DOWNLOAD_RESULT"
                f" code={getattr(response, 'code', None)}"
                f" msg={getattr(response, 'msg', None)}",
                flush=True,
            )
            return

        stream = getattr(response, "file", None)
        if stream is None:
            print("FILE_DOWNLOAD_RESULT code=0 msg=no_file_stream", flush=True)
            return

        chunks: list[bytes] = []
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        print(
            "FILE_DOWNLOAD_RESULT"
            f" code=0 file_name_hash={short_hash(getattr(response, 'file_name', None))}"
            f" byte_count={len(payload)} content_hash={bytes_hash(payload)}",
            flush=True,
        )
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"FILE_DOWNLOAD_ERROR: {type(exc).__name__}: {str(exc)[:160]}",
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

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    print("FILE_DOWNLOAD_PROBE_START: 按 Ctrl+C 停止。", flush=True)
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
