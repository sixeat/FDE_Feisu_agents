import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from fde_control_plane import Actor, ActorType, ControlPlane, FeishuApprovalGateway, SQLiteStore
from fde_control_plane.feishu_approval import CardDeliveryUncertain
from fde_control_plane.meeting_approval_bridge import MeetingApprovalBridge
from test_feishu_review import DOCUMENT_ID, setup_review


class Sender:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.lose_response = False

    def send(self, recipient, card, key):
        self.calls.append((recipient, card, key))
        if self.lose_response:
            raise TimeoutError("response lost after Feishu accepted message")
        return "om_test_card"

    def update(self, message_id, card):
        self.updates.append((message_id, card))


def bridge_setup(tmp_path, *, submit=True):
    cp, review, reader, owner, task_id, edits = setup_review(tmp_path)
    cp.store.save_meeting_source("record", {
        "document_id": DOCUMENT_ID, "revision_id": "5", "title": "测试会议", "evidence": [],
    })
    approval_id = None
    if submit:
        result = review.submit(task_run_id=task_id, record_id="record", document_id=DOCUMENT_ID,
                               reviewer_identity=owner, updates_by_todo_ref=edits)
        approval_id = result.approval_ids[0]
    sender = Sender()
    gateway = FeishuApprovalGateway(cp, sender, app_id="cli_test")
    bridge = MeetingApprovalBridge(cp, gateway, reader, tenant_id="tenant")
    reader.calls.clear()
    return cp, sender, gateway, bridge, reader, task_id, approval_id


def callback(value, message_id="om_test_card", open_id="ou_submitter", event_id="card-click"):
    return SimpleNamespace(
        header=SimpleNamespace(app_id="cli_test", event_id=event_id),
        event=SimpleNamespace(context=SimpleNamespace(open_message_id=message_id),
                              operator=SimpleNamespace(open_id=open_id),
                              action=SimpleNamespace(tag="button", value=value)),
    )


def test_reviewed_meeting_card_replays_after_restart_and_callback_only_enqueues(tmp_path):
    cp, sender, gateway, bridge, reader, task_id, approval_id = bridge_setup(tmp_path)
    assert bridge.preview("record", "member")[0]["card_status"] == "NOT_SENT"
    assert reader.calls == sender.calls == []
    assert bridge.send_pending("record", "member", "ou_submitter")[0]["card_status"] == "SENT"
    assert reader.calls == [DOCUMENT_ID]
    assert cp.store.get_approval_delivery_attempt(approval_id)["status"] == "SENT"
    assert cp.store.list_tool_calls(task_id) == []
    value = sender.calls[0][1]["elements"][-1]["actions"][0]["value"]
    path = cp.store.path
    cp.close()
    reopened = ControlPlane(SQLiteStore(path))
    new_gateway = FeishuApprovalGateway(reopened, sender, app_id="cli_test")
    new_bridge = MeetingApprovalBridge(reopened, new_gateway, reader, tenant_id="tenant")
    assert new_bridge.send_pending("record", "member", "ou_submitter")[0]["card_status"] == "SENT"
    assert len(sender.calls) == 1
    first = new_gateway.receive(callback(value))
    assert first["accepted"] is True
    assert new_gateway.receive(callback(value))["duplicate"] is True
    calls = reopened.store.list_tool_calls(task_id)
    assert len(calls) == 1 and calls[0]["status"] == "PREPARED"
    new_gateway.update_decided_card(approval_id, assignee_name="当前成员")
    assert "等待任务执行" in sender.updates[0][1]["header"]["title"]["content"]
    assert not any(element.get("tag") == "action" for element in sender.updates[0][1]["elements"])
    reopened.close()


def test_response_loss_stays_unknown_and_does_not_resend_after_restart(tmp_path):
    cp, sender, _, bridge, reader, task_id, approval_id = bridge_setup(tmp_path)
    sender.lose_response = True
    result = bridge.send_pending("record", "member", "ou_submitter")
    assert result[0]["card_status"] == "UNKNOWN"
    assert cp.store.get_approval_delivery_attempt(approval_id)["status"] == "UNKNOWN"
    assert cp.store.get_approval_delivery(approval_id) is None
    assert cp.store.list_tool_calls(task_id) == []
    path = cp.store.path
    cp.close()
    sender.lose_response = False
    reopened = ControlPlane(SQLiteStore(path))
    new_gateway = FeishuApprovalGateway(reopened, sender, app_id="cli_test")
    new_bridge = MeetingApprovalBridge(reopened, new_gateway, reader, tenant_id="tenant")
    assert new_bridge.send_pending("record", "member", "ou_submitter")[0]["card_status"] == "UNKNOWN"
    assert len(sender.calls) == 1
    reopened.close()


def test_send_claim_blocks_a_second_connection_and_a_crashed_sender(tmp_path):
    cp, sender, gateway, _, _, _, approval_id = bridge_setup(tmp_path)
    second_cp = ControlPlane(SQLiteStore(cp.store.path))
    second_gateway = FeishuApprovalGateway(second_cp, sender, app_id="cli_test")
    original = sender.send

    def overlapping_send(recipient, card, key):
        with pytest.raises(CardDeliveryUncertain):
            second_gateway.send(approval_id, recipient_actor_id="member", recipient_open_id="ou_submitter", assignee_name="member")
        return original(recipient, card, key)

    sender.send = overlapping_send
    gateway.send(approval_id, recipient_actor_id="member", recipient_open_id="ou_submitter", assignee_name="member")
    assert len(sender.calls) == 1
    second_cp.close()


def test_persisted_inflight_attempt_requires_reconciliation(tmp_path):
    cp, sender, gateway, bridge, _, _, approval_id = bridge_setup(tmp_path)
    proposal_hash = cp.store.get_approval(approval_id)["proposal_hash"]
    assert cp.store.claim_approval_delivery(approval_id, "member", proposal_hash, "crashed-before-send")
    assert bridge.preview("record", "member")[0]["card_status"] == "UNKNOWN"
    with pytest.raises(CardDeliveryUncertain):
        gateway.send(approval_id, recipient_actor_id="member", recipient_open_id="ou_submitter", assignee_name="member")
    assert sender.calls == []


def test_unreviewed_other_member_and_other_tenant_cannot_dispatch(tmp_path):
    cp, sender, gateway, bridge, reader, task_id, _ = bridge_setup(tmp_path, submit=False)
    with pytest.raises(ValueError, match="submitted"):
        bridge.send_pending("record", "member", "ou_submitter")
    with pytest.raises(PermissionError):
        bridge.preview("record", "admin")
    with pytest.raises(PermissionError):
        MeetingApprovalBridge(cp, gateway, reader, tenant_id="other").preview("record", "member")
    assert reader.calls == sender.calls == []
    assert cp.store.list_tool_calls(task_id) == []


def test_stale_or_swapped_source_and_revoked_agent_block_card_delivery(tmp_path):
    cp, sender, _, bridge, reader, _, approval_id = bridge_setup(tmp_path)
    reader.revision = "6"
    with pytest.raises(ValueError, match="source document changed"):
        bridge.send_pending("record", "member", "ou_submitter")
    assert cp.store.get_approval_delivery_attempt(approval_id) is None
    source = cp.store.get_meeting_source("record")
    # Fault injection bypasses the insert-only source snapshot API.
    cp.store.connection.execute("UPDATE meeting_sources SET data_json = ? WHERE record_id = 'record'",
                               (cp.store._json({**source, "document_id": "other-doc"}),))
    with pytest.raises(PermissionError, match="source document mismatch"):
        bridge.send_pending("record", "member", "ou_submitter")
    cp.register_actor(Actor("agent", "tenant", ActorType.BUSINESS_AGENT, active=False))
    with pytest.raises(PermissionError):
        bridge.send_pending("record", "member", "ou_submitter")
    assert sender.calls == []


def test_wrong_recipient_and_duplicate_identity_binding_never_claim_send(tmp_path):
    cp, sender, _, bridge, _, _, approval_id = bridge_setup(tmp_path)
    with pytest.raises(PermissionError):
        bridge.send_pending("record", "member", "ou_wrong")
    assert cp.store.get_approval_delivery_attempt(approval_id) is None
    cp.register_actor(Actor("duplicate-member", "tenant", ActorType.USER,
                            external_ref_hash=hashlib.sha256(b"ou_submitter").hexdigest()))
    with pytest.raises(PermissionError):
        bridge.send_pending("record", "member", "ou_submitter")
    assert sender.calls == []


def test_rejecting_a_card_never_enqueues_a_task(tmp_path):
    cp, sender, gateway, bridge, _, task_id, _ = bridge_setup(tmp_path)
    bridge.send_pending("record", "member", "ou_submitter")
    value = sender.calls[0][1]["elements"][-1]["actions"][1]["value"]
    assert gateway.receive(callback(value, open_id="ou_other"))["accepted"] is False
    assert gateway.receive(callback(value))["status"] == "REJECTED"
    assert cp.store.list_tool_calls(task_id) == []


def test_forged_proposal_origin_cannot_dispatch(tmp_path):
    cp, sender, _, bridge, _, _, approval_id = bridge_setup(tmp_path)
    approval = cp._approval_from_row(cp.store.get_approval(approval_id))
    approval.proposal.arguments["origin"]["record_id"] = "other-record"
    cp.store.save_approval(approval, cp._approval_data(approval))
    with pytest.raises(PermissionError, match="not bound"):
        bridge.send_pending("record", "member", "ou_submitter")
    assert sender.calls == []


def test_operator_listener_handles_verified_callback_and_patches_without_worker(tmp_path, monkeypatch, capsys):
    import lark_oapi as lark

    cp, sender, _, bridge, _, task_id, _ = bridge_setup(tmp_path)
    bridge.send_pending("record", "member", "ou_submitter")
    value = sender.calls[0][1]["elements"][-1]["actions"][0]["value"]
    db_path = cp.store.path
    cp.close()
    spec = importlib.util.spec_from_file_location("test_bridge_script", Path(__file__).parents[1] / "scripts/meeting_approval_bridge.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "arguments", lambda: SimpleNamespace(
        mode="listen", db=db_path, tenant="tenant", member="member", record=None, recipient_file=None,
    ))
    monkeypatch.setattr(script, "client_from_environment", lambda: ("cli_test", "never-printed-secret", object()))
    monkeypatch.setattr(script, "FeishuCardSender", lambda client: sender)
    responses = []

    class FakeBuilder:
        def register_p2_card_action_trigger(self, handler):
            self.handler = handler
            return self

        def build(self):
            return self.handler

    class FakeConnection:
        def __init__(self, app_id, secret, *, event_handler, log_level):
            self.handler = event_handler

        def start(self):
            responses.append(self.handler(callback(value, open_id="ou_other")))
            responses.append(self.handler(callback(value)))
            responses.append(self.handler(callback(value)))

    monkeypatch.setattr(lark.EventDispatcherHandler, "builder", lambda *args: FakeBuilder())
    monkeypatch.setattr(lark.ws, "Client", FakeConnection)
    assert script.main() == 0
    output = capsys.readouterr().out
    assert "never-printed-secret" not in output
    assert "task_worker=OFF" in output and "duplicate=True" in output
    assert responses[0].toast.type == "warning"
    assert responses[1].toast.type == "success"
    assert sender.updates and sender.updates[0][0] == "om_test_card"
    with SQLiteStore(db_path) as store:
        assert store.list_tool_calls(task_id)[0]["status"] == "PREPARED"
