"""Operator-only reviewed meeting card bridge; never starts a task worker."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane import ControlPlane, FeishuApprovalGateway, FeishuCardSender, SQLiteStore
from fde_control_plane.approval_card import build_meeting_decided_card
from fde_control_plane.feishu_review import FeishuDocumentRevisionReader
from fde_control_plane.meeting_approval_bridge import MeetingApprovalBridge


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


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

        with quiet_sdk_logs(), ThreadPoolExecutor(max_workers=1) as updater:
            def on_card_action(data):
                value = getattr(getattr(getattr(data, "event", None), "action", None), "value", None) or {}
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

            handler = lark.EventDispatcherHandler.builder("", "").register_p2_card_action_trigger(on_card_action).build()
            print("MEETING_APPROVAL_LISTENING task_worker=OFF task_write=0", flush=True)
            connection = lark.ws.Client(app_id, secret, event_handler=handler, log_level=lark.LogLevel.ERROR)
            connection.on_reconnecting = lambda: print("MEETING_APPROVAL_CONNECTION status=RECONNECTING task_write=0", flush=True)
            connection.on_reconnected = lambda: print("MEETING_APPROVAL_CONNECTION status=CONNECTED task_write=0", flush=True)
            connection.start()
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
