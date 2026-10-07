"""Feishu long-connection probe for card action callbacks.

This disposable phase-0 helper registers the card action callback on the same
WebSocket transport used by the message probe. It only returns a Toast and
prints hashed metadata; it creates a task only when an explicit write flag is set.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from typing import Any

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)


_APP_ID = ""
_APP_SECRET = ""
_HANDLED_SOURCE_MESSAGES: set[str] = set()


def capture_operator_open_id(operator_open_id: str | None) -> None:
    """Optionally save a callback operator ID to a local ignored file."""
    if os.environ.get("FEISHU_CAPTURE_OPERATOR_OPEN_ID") != "1" or not operator_open_id:
        return
    capture_path = os.environ.get("FEISHU_OPERATOR_OPEN_ID_FILE", "data/feishu_operator_open_id.txt")
    path = os.path.abspath(capture_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(operator_open_id)


def short_hash(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def build_assigned_task(operator_open_id: str, message_id: str, client_token: str) -> Any:
    member = (
        lark.api.task.v2.model.Member.builder()
        .id(operator_open_id)
        .type("user")
        .role("assignee")
        .build()
    )
    source_message = (
        lark.api.task.v2.model.OriginSourceMessage.builder()
        .message_id(message_id)
        .build()
    )
    source = (
        lark.api.task.v2.model.OriginReferResource.builder()
        .resource_id(message_id)
        .type("message")
        .source_message(source_message)
        .build()
    )
    origin = lark.api.task.v2.model.Origin.builder().refer_resources([source]).build()
    return (
        lark.api.task.v2.model.InputTask.builder()
        .summary("Phase0归属测试任务-Alpha")
        .description("阶段0测试：验证责任人、来源消息和任务可见性；验证后可删除或归档。")
        .members([member])
        .origin(origin)
        .client_token(client_token)
        .build()
    )


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None) if event else None
    operator_id = getattr(operator, "operator_id", None) if operator else None
    context = getattr(event, "context", None) if event else None
    action = getattr(event, "action", None) if event else None
    action_tag = getattr(action, "tag", None) if action else None
    header = getattr(data, "header", None)
    event_id = getattr(header, "event_id", None)
    open_chat_id = getattr(context, "open_chat_id", None)
    open_message_id = getattr(context, "open_message_id", None)
    operator_open_id = getattr(operator, "open_id", None)
    capture_operator_open_id(operator_open_id)
    print(
        "CARD_CALLBACK_EVENT "
        f"event_id_hash={short_hash(event_id)} "
        f"operator_id_hash={short_hash(operator_id)} "
        f"operator_open_id_hash={short_hash(operator_open_id)} "
        f"chat_id_hash={short_hash(open_chat_id)} "
        f"message_id_hash={short_hash(open_message_id)} "
        f"action_tag_hash={short_hash(action_tag)}",
        flush=True,
    )
    if os.environ.get("FEISHU_TASK_ASSIGNMENT_DRY_RUN") == "1":
        stable_source = open_message_id or event_id or "missing-source"
        client_token = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fde:assigned:{stable_source}"))
        print(
            "ASSIGNED_TASK_PLAN "
            f"assignee_open_id_hash={short_hash(operator_open_id)} "
            f"origin_chat_id_hash={short_hash(open_chat_id)} "
            f"origin_message_id_hash={short_hash(open_message_id)} "
            f"client_token_hash={short_hash(client_token)}",
            flush=True,
        )
    if os.environ.get("FEISHU_TASK_ASSIGNMENT_WRITE") != "1":
        return P2CardActionTriggerResponse(
            {"toast": {"type": "success", "content": "已收到测试卡片回调"}}
        )
    if not operator_open_id or not open_message_id:
        return P2CardActionTriggerResponse(
            {"toast": {"type": "warning", "content": "缺少责任人或来源消息，未创建任务"}}
        )
    if open_message_id in _HANDLED_SOURCE_MESSAGES:
        print("ASSIGNED_TASK_SKIPPED reason=source_message_already_handled", flush=True)
        return P2CardActionTriggerResponse(
            {"toast": {"type": "success", "content": "该测试动作已处理"}}
        )
    _HANDLED_SOURCE_MESSAGES.add(open_message_id)
    stable_source = open_message_id or event_id or "missing-source"
    client_token = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fde:assigned:{stable_source}"))
    try:
        client = lark.Client.builder().app_id(_APP_ID).app_secret(_APP_SECRET).build()
        task = build_assigned_task(operator_open_id, open_message_id, client_token)
        request = (
            lark.api.task.v2.model.CreateTaskRequest.builder()
            .request_body(task)
            .build()
        )
        response = client.task.v2.task.create(request)
        print(
            f"ASSIGNED_TASK_CREATE_RESULT code={getattr(response, 'code', None)} "
            f"msg={getattr(response, 'msg', None)}",
            flush=True,
        )
        if getattr(response, "code", None) != 0:
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "归属测试任务创建失败"}}
            )
        remote_task = getattr(getattr(response, "data", None), "task", None)
        task_guid = getattr(remote_task, "guid", None)
        print(f"ASSIGNED_TASK_REMOTE_ID_HASH={short_hash(task_guid)}", flush=True)
        if not task_guid:
            print("ASSIGNED_TASK_RECONCILIATION_REQUIRED missing_remote_guid", file=sys.stderr)
            return P2CardActionTriggerResponse(
                {"toast": {"type": "warning", "content": "任务结果未知，需要对账"}}
            )
        query = (
            lark.api.task.v2.model.GetTaskRequest.builder()
            .task_guid(task_guid)
            .build()
        )
        query_response = client.task.v2.task.get(query)
        print(
            f"ASSIGNED_TASK_QUERY_RESULT code={getattr(query_response, 'code', None)} "
            f"msg={getattr(query_response, 'msg', None)}",
            flush=True,
        )
        if getattr(query_response, "code", None) == 0:
            return P2CardActionTriggerResponse(
                {"toast": {"type": "success", "content": "已创建归属测试任务"}}
            )
        return P2CardActionTriggerResponse(
            {"toast": {"type": "warning", "content": "任务已创建但查询失败，需要对账"}}
        )
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"ASSIGNED_TASK_UNKNOWN: {type(exc).__name__}: {str(exc)[:160]}",
            file=sys.stderr,
            flush=True,
        )
        return P2CardActionTriggerResponse(
            {"toast": {"type": "warning", "content": "任务结果未知，需要对账"}}
        )


def on_message(data: Any) -> None:
    event = getattr(data, "event", None)
    message = getattr(event, "message", None) if event else None
    sender = getattr(event, "sender", None) if event else None
    if getattr(sender, "sender_type", None) == "app":
        return
    message_id = getattr(message, "message_id", None)
    if not message_id:
        print("CARD_REPLY_SKIPPED: message_id_missing", flush=True)
        return
    if os.environ.get("FEISHU_CARD_REPLY") != "1":
        return

    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "阶段 0 测试卡片：点击按钮验证回调。"},
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "lark_md", "content": "确认测试回调"},
                        "type": "primary",
                        "value": {"action": "phase0_card_confirm"},
                    }
                ],
            },
        ],
    }
    try:
        client = lark.Client.builder().app_id(_APP_ID).app_secret(_APP_SECRET).build()
        body = (
            lark.api.im.v1.model.ReplyMessageRequestBody.builder()
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .reply_in_thread(False)
            .uuid(str(uuid.uuid5(uuid.NAMESPACE_URL, message_id + ":card")))
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
            f"CARD_REPLY_RESULT: code={getattr(response, 'code', None)} "
            f"msg={getattr(response, 'msg', None)}",
            flush=True,
        )
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"CARD_REPLY_ERROR: {type(exc).__name__}: {str(exc)[:160]}",
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
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    print(
        "LONG_CONNECTION_CARD_PROBE_START: 正在建立长连接；按 Ctrl+C 停止。",
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
