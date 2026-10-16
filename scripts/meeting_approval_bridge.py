"""Operator-only reviewed meeting card bridge; never starts a task worker."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from fde_control_plane import (
    AgentOrchestrator, ControlPlane, FeishuApprovalGateway, FeishuCardSender,
    FeishuMeetingReviewGateway, HermesRuntimeAdapter, SQLiteStore,
)
from fde_control_plane.approval_card import MEETING_REVIEW_FORM_ACTION, build_meeting_decided_card
from fde_control_plane.feishu_review import FeishuDocumentRevisionReader
from fde_control_plane.meeting_card_review import (
    FeishuMeetingCardReviewGateway, build_review_processing_card,
    build_review_rejected_card, build_review_submitted_card,
)
from fde_control_plane.meeting_approval_bridge import MeetingApprovalBridge
from fde_control_plane.user_document_access import FeishuUserDocumentReader, UserTokenVault
from personal_assistant_intake import enqueue_callback, process_one


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def card_form_probe_result(action):
    """Inspect only the shape of a test form callback; no business mutation."""
    values = getattr(action, "form_value", None)
    if not isinstance(values, dict) or set(values) != {"assignee_0", "due_0"}:
        return {"accepted": False, "field_count": 0}
    if any(not isinstance(value, str) or not value or len(value) > 128 for value in values.values()):
        return {"accepted": False, "field_count": 0}
    return {"accepted": True, "field_count": len(values)}


@contextmanager
def quiet_sdk_logs():
    # Connection exceptions can include a credential-bearing websocket URL.
    logger = logging.getLogger("Lark")
    previous = logger.disabled
    logger.disabled = True
    try:
        yield
    finally:
        logger.disabled = previous


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preview", "send", "listen", "refresh"])
    parser.add_argument("--db", default=os.environ.get("FDE_DB_PATH"))
    parser.add_argument("--tenant", default=os.environ.get("FDE_TENANT_ID", "sixeat"))
    parser.add_argument("--member", default="member-primary")
    parser.add_argument("--record")
    parser.add_argument("--recipient-file", default=os.environ.get("FDE_APPROVAL_RECIPIENT_FILE"))
    args = parser.parse_args()
    if not args.db or not Path(args.db).is_file():
        parser.error("--db must name an existing control-plane database")
    if args.mode != "listen" and not args.record:
        parser.error("--record is required outside listener mode")
    if args.mode == "send" and not args.recipient_file:
        parser.error("send requires --recipient-file (raw open_id is never accepted on the command line)")
    return args


def client_from_environment():
    import lark_oapi as lark

    app_id, secret = os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not secret:
        raise ValueError("Feishu credentials are missing from environment")
    return app_id, secret, lark.Client.builder().app_id(app_id).app_secret(secret).timeout(12).build()


def report(items):
    for item in items:
        print(f"MEETING_APPROVAL approval_hash={short_hash(item['approval_id'])} "
              f"status={item['status']} card={item['card_status']} task_write=0", flush=True)


def process_review_one(*, db_path, tenant_id, tenant_key, app_id, client,
                       user_client, token_key, review_url,
                       user_reader=None, sender=None, dispatch=None):
    """Process one durable form outside the card callback; never execute tasks."""
    with SQLiteStore(db_path) as review_store:
        review_cp = ControlPlane(review_store)
        reader = user_reader or FeishuUserDocumentReader(UserTokenVault(review_store, token_key), user_client)
        card_sender = sender or FeishuCardSender(client)
        claim = str(uuid.uuid4())
        item = review_store.claim_meeting_card_review(tenant_id, claim, lease_seconds=120)
        if item is None:
            return "EMPTY"
        review_gateway = FeishuMeetingReviewGateway(
            review_cp, AgentOrchestrator(review_cp, HermesRuntimeAdapter()),
            FeishuDocumentRevisionReader(client), reader,
        )
        result = FeishuMeetingCardReviewGateway(
            review_gateway, app_id=app_id, tenant_id=tenant_id, tenant_key=tenant_key,
        ).receive(item["data"])
        status = "DONE" if result.get("accepted") else "REJECTED"
        if not review_store.finish_meeting_card_review(item["message_id"], claim, status, {
            "status": result.get("status"), "accepted": bool(result.get("accepted")),
        }):
            raise RuntimeError("meeting review claim was lost")
        card = (build_review_submitted_card(int(result["todo_count"]), review_url) if result.get("accepted")
                else build_review_rejected_card(review_url))
        try:
            card_sender.update(item["message_id"], card)
        except Exception as exc:
            print(f"MEETING_CARD_REVIEW_UPDATE status=UNKNOWN message_hash={short_hash(item['message_id'])} "
                  f"error_type={type(exc).__name__} task_write=0", flush=True)
        if result.get("accepted"):
            delivery = review_store.get_assistant_card_delivery_by_message(item["message_id"])
            record_id = result["record_id"]
            recipient = item["data"]["event"]["operator"]["open_id"]
            try:
                if dispatch is not None:
                    dispatch(record_id, delivery["recipient_actor_id"], recipient)
                else:
                    approval_bridge = MeetingApprovalBridge(
                        review_cp, FeishuApprovalGateway(review_cp, card_sender, app_id=app_id),
                        FeishuDocumentRevisionReader(client), tenant_id=tenant_id,
                    )
                    approval_bridge.send_pending(record_id, delivery["recipient_actor_id"], recipient)
            except Exception as exc:
                print(f"MEETING_CARD_REVIEW_DISPATCH status=RETRY_REQUIRED record_hash={short_hash(record_id)} "
                      f"error_type={type(exc).__name__} task_write=0", flush=True)
                return "DISPATCH_RETRY_REQUIRED"
        return status


def main() -> int:
    args = arguments()
    with SQLiteStore(args.db) as store:
        cp = ControlPlane(store)
        if args.mode == "preview":
            # Preview does not construct an SDK client or contact Feishu.
            bridge = MeetingApprovalBridge(cp, None, None, tenant_id=args.tenant)
            report(bridge.preview(args.record, args.member))
            return 0
        app_id, secret, client = client_from_environment()
        gateway = FeishuApprovalGateway(cp, FeishuCardSender(client), app_id=app_id)
        bridge = MeetingApprovalBridge(cp, gateway, FeishuDocumentRevisionReader(client), tenant_id=args.tenant)
        if args.mode == "send":
            recipient = Path(args.recipient_file).read_text(encoding="utf-8").strip()
            if not recipient:
                raise ValueError("recipient file is empty")
            items = bridge.send_pending(args.record, args.member, recipient)
            report(items)
            return 2 if any(item["card_status"] == "UNKNOWN" for item in items) else 0
        if args.mode == "refresh":
            for item in bridge.preview(args.record, args.member):
                if item["status"] in {"APPROVED", "REJECTED"} and item["card_status"] == "SENT":
                    row = cp._approval_from_row(store.get_approval(item["approval_id"]))
                    assignee = row.proposal.arguments["assignee_actor_id"]
                    gateway.update_decided_card(item["approval_id"], assignee_name="当前成员" if assignee == args.member else assignee)
                    print(f"MEETING_APPROVAL_REFRESH approval_hash={short_hash(item['approval_id'])} status=UPDATED task_write=0", flush=True)
            return 0

        import lark_oapi as lark
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        def patch_decision(approval_id):
            # A separate connection keeps HTTP PATCH out of the callback path.
            try:
                with SQLiteStore(args.db) as patch_store:
                    patch_cp = ControlPlane(patch_store)
                    patch_gateway = FeishuApprovalGateway(patch_cp, FeishuCardSender(client), app_id=app_id)
                    approval = patch_cp._approval_from_row(patch_store.get_approval(approval_id))
                    assignee = str(approval.proposal.arguments["assignee_actor_id"])
                    patch_gateway.update_decided_card(approval_id, assignee_name=assignee)
                print(f"MEETING_APPROVAL_REFRESH approval_hash={short_hash(approval_id)} status=UPDATED task_write=0", flush=True)
            except Exception as exc:
                print(f"MEETING_APPROVAL_REFRESH approval_hash={short_hash(approval_id)} "
                      f"status=RETRY_REQUIRED error_type={type(exc).__name__} task_write=0", flush=True)

        assistant_enabled = os.environ.get("FDE_ASSISTANT_INTAKE_ENABLED") == "1"
        card_review_enabled = assistant_enabled and os.environ.get("FDE_ASSISTANT_CARD_REVIEW_ENABLED") == "1"
        assistant_stop = threading.Event()
        assistant_thread = None
        review_thread = None
        if assistant_enabled:
            tenant_key = os.environ.get("FEISHU_TENANT_KEY", "").strip()
            review_url = os.environ.get("FDE_ASSISTANT_REVIEW_URL", "").strip()
            if not tenant_key or not review_url:
                raise ValueError("assistant intake requires tenant key and review URL")
            worker_client = client_from_environment()[2]
            token_key = os.environ.get("FDE_USER_TOKEN_KEY", "").strip()
            if not token_key:
                raise ValueError("assistant intake requires user token encryption key")
            user_client = (lark.Client.builder().app_id(app_id).app_secret(secret).timeout(12)
                           .enable_set_token(True).build())

            def assistant_worker():
                while not assistant_stop.is_set():
                    try:
                        status = process_one(
                            db_path=args.db, tenant_id=args.tenant, tenant_key=tenant_key,
                            app_id=app_id, client=worker_client, review_url=review_url,
                            token_key=token_key, user_client=user_client,
                            card_review_enabled=card_review_enabled,
                        )
                        if status == "EMPTY":
                            assistant_stop.wait(2)
                    except Exception as exc:
                        print(f"ASSISTANT_WORKER status=ERROR error_type={type(exc).__name__} task_write=0", flush=True)
                        assistant_stop.wait(5)

            assistant_thread = threading.Thread(target=assistant_worker, name="assistant-intake", daemon=True)

            if card_review_enabled:
                def review_worker():
                    while not assistant_stop.is_set():
                        try:
                            status = process_review_one(
                                db_path=args.db, tenant_id=args.tenant, tenant_key=tenant_key,
                                app_id=app_id, client=worker_client, user_client=user_client,
                                token_key=token_key, review_url=review_url,
                            )
                            if status == "EMPTY":
                                assistant_stop.wait(2)
                        except Exception as exc:
                            print(f"MEETING_CARD_REVIEW_WORKER status=ERROR "
                                  f"error_type={type(exc).__name__} task_write=0", flush=True)
                            assistant_stop.wait(5)

                review_thread = threading.Thread(target=review_worker, name="meeting-card-review", daemon=True)

            def on_message(callback):
                try:
                    with SQLiteStore(args.db) as inbox_store:
                        status = enqueue_callback(
                            inbox_store, callback, app_id=app_id,
                            tenant_key=tenant_key, tenant_id=args.tenant,
                        )
                    print(f"ASSISTANT_INTAKE status={status} task_write=0", flush=True)
                except Exception as exc:
                    print(f"ASSISTANT_INTAKE status=ERROR error_type={type(exc).__name__} task_write=0", flush=True)

        with quiet_sdk_logs(), ThreadPoolExecutor(max_workers=1) as updater:
            def on_card_action(data):
                action = getattr(getattr(data, "event", None), "action", None)
                if (os.environ.get("FDE_CARD_FORM_PROBE_ENABLED") == "1"
                        and getattr(action, "name", None) == "fde_card_form_probe"):
                    result = card_form_probe_result(action)
                    print(f"CARD_FORM_PROBE accepted={result['accepted']} "
                          f"field_count={result['field_count']} task_write=0", flush=True)
                    return P2CardActionTriggerResponse({
                        "toast": {"type": "success" if result["accepted"] else "warning",
                                  "content": "测试表单回调已收到，不会创建任务" if result["accepted"]
                                             else "测试表单字段不完整"},
                    })
                if card_review_enabled and getattr(action, "name", None) == MEETING_REVIEW_FORM_ACTION:
                    try:
                        with SQLiteStore(args.db) as review_store:
                            review_cp = ControlPlane(review_store)
                            review_gateway = FeishuMeetingReviewGateway(
                                review_cp, AgentOrchestrator(review_cp, HermesRuntimeAdapter()),
                                FeishuDocumentRevisionReader(client),
                            )
                            result = FeishuMeetingCardReviewGateway(
                                review_gateway, app_id=app_id, tenant_id=args.tenant,
                                tenant_key=tenant_key,
                            ).enqueue(data)
                    except Exception as exc:
                        print(f"MEETING_CARD_REVIEW status=ERROR error_type={type(exc).__name__} task_write=0", flush=True)
                        return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "审核暂时无法核对，请稍后重试"}})
                    print(f"MEETING_CARD_REVIEW_ENQUEUE accepted={result.get('accepted', False)} "
                          f"status={result.get('status')} task_write=0", flush=True)
                    if not result.get("accepted"):
                        return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "审核未通过校验，请刷新或打开审核页"}})
                    if result["status"] == "DUPLICATE":
                        return P2CardActionTriggerResponse({
                            "toast": {"type": "success", "content": "已收到该卡片的提交，请查看审核状态"},
                        })
                    return P2CardActionTriggerResponse({
                        "toast": {"type": "success", "content": "提交已接收，正在核对"},
                        "card": {"type": "raw", "data": build_review_processing_card(review_url)},
                    })
                value = getattr(action, "value", None) or {}
                approval_id = value.get("approval_id") if isinstance(value, dict) else None
                row = store.get_approval(approval_id) if isinstance(approval_id, str) else None
                if row is None or cp._task(row["task_run_id"]).tenant_id != args.tenant:
                    return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "审批不存在或无权访问"}})
                result = gateway.receive(data)
                print(f"MEETING_APPROVAL_CALLBACK accepted={result.get('accepted', False)} "
                      f"status={result.get('status', 'NONE')} duplicate={result.get('duplicate', False)} task_write=0", flush=True)
                response = {"toast": {"type": "success" if result.get("accepted") else "warning",
                                      "content": "已记录决定，任务尚未执行" if result.get("accepted") else "审批未通过校验"}}
                if result.get("accepted"):
                    approval = cp._approval_from_row(store.get_approval(approval_id))
                    assignee = str(approval.proposal.arguments["assignee_actor_id"])
                    response["card"] = {"type": "raw", "data": build_meeting_decided_card(approval, assignee_name=assignee)}
                    updater.submit(patch_decision, approval_id)
                return P2CardActionTriggerResponse(response)

            builder = lark.EventDispatcherHandler.builder("", "").register_p2_card_action_trigger(on_card_action)
            if assistant_enabled:
                builder = builder.register_p2_im_message_receive_v1(on_message)
            handler = builder.build()
            print(f"MEETING_APPROVAL_LISTENING task_worker=OFF assistant_intake={'ON' if assistant_enabled else 'OFF'} task_write=0", flush=True)
            connection = lark.ws.Client(app_id, secret, event_handler=handler, log_level=lark.LogLevel.ERROR)
            connection.on_reconnecting = lambda: print("MEETING_APPROVAL_CONNECTION status=RECONNECTING task_write=0", flush=True)
            connection.on_reconnected = lambda: print("MEETING_APPROVAL_CONNECTION status=CONNECTED task_write=0", flush=True)
            if assistant_thread is not None:
                assistant_thread.start()
            if review_thread is not None:
                review_thread.start()
            try:
                connection.start()
            finally:
                assistant_stop.set()
                if assistant_thread is not None:
                    assistant_thread.join(timeout=5)
                if review_thread is not None:
                    review_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as exc:
        # SDK exceptions can contain credential-bearing connection URLs.
        print(f"MEETING_APPROVAL_BRIDGE_FAILED error_type={type(exc).__name__} task_write=0", file=sys.stderr)
        raise SystemExit(1)
