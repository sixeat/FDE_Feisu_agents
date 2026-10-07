"""Isolated Feishu card transport probe; it never executes the task outbox."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane import (  # noqa: E402
    Actor, ActorType, AgentVersion, AssistantBinding, ControlPlane,
    FeishuApprovalGateway, FeishuCardSender, MeetingWorkflow, SQLiteStore,
    build_meeting_decided_card,
)


CONFIG = Path("D:/Temp/feishu-minutes.local.json")
OPEN_ID_FILE = ROOT / "data" / "feishu_operator_open_id.txt"
DB_FILE = ROOT / "data" / "feishu_approval_card_probe.sqlite3"
RECORD_ID = "probe-meeting-state-v2"
OPERATION_ID = f"meeting-{RECORD_ID}-probe-todo"
LEGACY_OPERATION_ID = "meeting-probe-meeting-probe-todo"


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def credentials() -> tuple[str, str]:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    return str(config["app_id"]), str(config["app_secret"])


def prepare() -> int:
    app_id, app_secret = credentials()
    open_id = OPEN_ID_FILE.read_text(encoding="utf-8").strip()
    if not open_id:
        raise ValueError("operator open id file is empty")
    cp = ControlPlane(SQLiteStore(DB_FILE))
    caps = frozenset({"doc.read", "task.write"})
    cp.register_actor(Actor("probe-member", "probe-tenant", ActorType.USER, caps, external_ref_hash=hashlib.sha256(open_id.encode()).hexdigest()))
    cp.register_actor(Actor("probe-assistant", "probe-tenant", ActorType.PERSONAL_ASSISTANT, caps))
    cp.register_actor(Actor("probe-meeting-agent", "probe-tenant", ActorType.BUSINESS_AGENT, caps))
    cp.register_agent_version(AgentVersion("probe-meeting-agent", 1, caps))
    cp.bind_assistant(AssistantBinding("probe-binding", "probe-tenant", "probe-member", "probe-assistant"))
    existing = cp.get_approval_for_operation(OPERATION_ID)
    if existing is None:
        task = cp.create_task_run("probe-member", "probe-assistant", "probe-meeting-agent", 1)
        payload = {
            "schema_version": "meeting-agent.v1",
            "source": {"document_id_hash": "probe-fictional-source", "revision_id": "1", "source_block_ids": [1]},
            "summary": {"text": "审批卡片传输测试", "evidence_block_ids": [1]},
            "todos": [{
                "todo_id": "probe-todo", "title": "测试：验证审批卡片回调（不会创建任务）",
                "evidence_block_ids": [1], "assignee_candidate": {"display_name": "测试成员"},
                "due_date_candidate": {"raw_text": "2026-10-10", "normalized_date": "2026-10-10", "status": "normalized"},
                "needs_confirmation": False,
            }],
            "relations": [],
        }
        workflow = MeetingWorkflow(cp)
        workflow.ingest(
            record_id=RECORD_ID, tenant_id="probe-tenant", submitted_by="probe-member",
            payload=payload, document_id_hash="probe-fictional-source", revision_id="1",
            valid_block_ids={1}, task_run_id=task.task_run_id, source_type="test_fixture",
        )
        workflow.build_drafts(record_id=RECORD_ID, payload=payload,
                              assignee_actor_by_todo={"probe-todo": "probe-member"})
        _, existing = workflow.create_task_proposals(RECORD_ID, task.task_run_id)[0]
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    gateway = FeishuApprovalGateway(cp, FeishuCardSender(client), app_id=app_id)
    message_id = gateway.send(existing.approval_id, recipient_actor_id="probe-member",
                              recipient_open_id=open_id, assignee_name="测试成员",
                              approve_label="确认测试回调")
    print(f"CARD_PROBE_SENT approval_hash={short_hash(existing.approval_id)} message_hash={short_hash(message_id)} task_write=0", flush=True)
    cp.close()
    return 0


def listen() -> int:
    app_id, app_secret = credentials()
    cp = ControlPlane(SQLiteStore(DB_FILE))
    approval = cp.get_approval_for_operation(OPERATION_ID)
    if approval is None:
        raise ValueError("test approval is missing")
    api_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    gateway = FeishuApprovalGateway(cp, FeishuCardSender(api_client), app_id=app_id)
    finished = threading.Event()

    def on_card_action(data):
        value = getattr(getattr(getattr(data, "event", None), "action", None), "value", None) or {}
        if value.get("approval_id") != approval.approval_id:
            return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "不是当前测试卡片"}})
        result = gateway.receive(data)
        print(
            f"CARD_PROBE_CALLBACK accepted={result.get('accepted')} "
            f"status={result.get('status')} duplicate={result.get('duplicate', False)} "
            f"reason={result.get('reason', 'NONE')} task_write=0",
            flush=True,
        )
        if result.get("accepted"):
            finished.set()
        toast_type = "success" if result.get("accepted") else "warning"
        content = "已记录测试审批；不会创建飞书任务" if result.get("accepted") else "测试卡片未通过校验"
        response = {"toast": {"type": toast_type, "content": content}}
        if result.get("accepted"):
            row = cp.store.get_approval(value.get("approval_id"))
            if row is not None:
                approval = cp._approval_from_row(row)
                response["card"] = {
                    "type": "raw",
                    "data": build_meeting_decided_card(
                        approval, assignee_name="测试成员", test_mode=True
                    ),
                }
        return P2CardActionTriggerResponse(response)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_card_action_trigger(on_card_action).build()
    client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                            log_level=lark.LogLevel.ERROR, auto_reconnect=True)
    thread = threading.Thread(target=client.start, daemon=True)
    thread.start()
    print("CARD_PROBE_LISTENING timeout_seconds=180 task_write=0", flush=True)
    received = finished.wait(180)
    if received:
        gateway.update_decided_card(approval.approval_id, assignee_name="测试成员", test_mode=True)
        print("CARD_PROBE_PATCH_RESULT status=SUCCEEDED task_write=0", flush=True)
    cp.close()
    print(f"CARD_PROBE_FINISHED received={received} task_write=0", flush=True)
    return 0 if received else 1


def verify(operation_id: str = OPERATION_ID) -> int:
    app_id, app_secret = credentials()
    cp = ControlPlane(SQLiteStore(DB_FILE))
    approval = cp.get_approval_for_operation(operation_id)
    if approval is None:
        raise ValueError("test approval is missing")
    delivery = cp.store.get_approval_delivery(approval.approval_id)
    if delivery is None:
        raise ValueError("test card delivery is missing")
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    request = (
        lark.api.im.v1.model.GetMessageRequest.builder()
        .message_id(delivery["message_id"])
        .build()
    )
    response = client.im.v1.message.get(request)
    print(f"CARD_PROBE_VERIFY code={getattr(response, 'code', None)}", flush=True)
    if getattr(response, "code", None) != 0:
        return 1
    items = getattr(getattr(response, "data", None), "items", None) or []
    print(f"CARD_PROBE_VERIFY_SHAPE items_type={type(items).__name__} count={len(items)}", flush=True)
    if len(items) != 1:
        print(f"CARD_PROBE_VERIFY_ITEMS count={len(items)}", flush=True)
        return 1
    body = getattr(items[0], "body", None)
    content = getattr(body, "content", None)
    print(f"CARD_PROBE_VERIFY_BODY body_type={type(body).__name__} content_type={type(content).__name__}", flush=True)
    card = json.loads(content)
    print(f"CARD_PROBE_VERIFY_CONTENT parsed_type={type(card).__name__}", flush=True)
    if not isinstance(card, dict):
        return 1
    elements = card.get("elements") or []
    if not isinstance(elements, list):
        return 1
    serialized = json.dumps(card, ensure_ascii=False)
    resolved = "已确认测试，未创建任务" in serialized
    old_button = "确认测试回调" in serialized
    print(f"CARD_PROBE_CARD_STATE resolved={resolved} old_button_text={old_button}", flush=True)
    cp.close()
    return 0 if resolved and not old_button else 1


def patch_card(operation_id: str = OPERATION_ID) -> int:
    app_id, app_secret = credentials()
    cp = ControlPlane(SQLiteStore(DB_FILE))
    approval = cp.get_approval_for_operation(operation_id)
    if approval is None:
        raise ValueError("test approval is missing")
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    gateway = FeishuApprovalGateway(cp, FeishuCardSender(client), app_id=app_id)
    gateway.update_decided_card(approval.approval_id, assignee_name="测试成员", test_mode=True)
    print("CARD_PROBE_PATCH_RESULT status=SUCCEEDED task_write=0", flush=True)
    cp.close()
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "listen", "verify", "patch", "patch-legacy", "verify-legacy"}:
        print("usage: feishu_approval_card_probe.py prepare|listen|verify|patch|patch-legacy|verify-legacy", file=sys.stderr)
        return 2
    try:
        operations = {
            "prepare": prepare, "listen": listen, "verify": verify, "patch": patch_card,
            "patch-legacy": lambda: patch_card(LEGACY_OPERATION_ID),
            "verify-legacy": lambda: verify(LEGACY_OPERATION_ID),
        }
        return operations[sys.argv[1]]()
    except Exception as exc:
        code = str(exc) if str(exc).startswith("FEISHU_CARD_UPDATE_") else "NONE"
        print(f"CARD_PROBE_ERROR type={type(exc).__name__} code={code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
