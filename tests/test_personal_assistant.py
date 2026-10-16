import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import meeting_approval_bridge, personal_assistant_intake

from fde_control_plane import (
    Actor, ActorType, AgentOrchestrator, AgentVersion, AssistantBinding,
    ControlPlane, FeishuMeetingReviewGateway, FeishuPersonalAssistantGateway,
    MeetingTodoDraft, RuntimeResult, SQLiteStore,
)
from fde_control_plane.personal_assistant import _form_review_todos
from fde_control_plane.user_document_access import UserDocumentAuthorizationRequired
from fde_control_plane.meeting_card_review import FeishuMeetingCardReviewGateway
from fde_control_plane.orchestration import _unique_assignee_suggestions


DOC_ID = "test-docx-id"
DOC_HASH = hashlib.sha256(DOC_ID.encode()).hexdigest()[:12]
CAPS = frozenset({"doc.read", "task.write"})


class FakeRuntime:
    calls = 0

    def run_once(self, request):
        self.calls += 1
        return RuntimeResult("SUCCEEDED", {
            "schema_version": "meeting-agent.v1",
            "source": {"document_id_hash": DOC_HASH, "revision_id": "5", "source_block_ids": [2]},
            "summary": {"text": "会议摘要", "evidence_block_ids": [2]},
            "todos": [{
                "todo_id": "todo-2", "title": "整理部署文档", "evidence_block_ids": [2],
                "assignee_candidate": {"display_name": "张三", "status": "candidate"},
                "due_date_candidate": {"raw_text": None, "normalized_date": None, "status": "missing"},
                "needs_confirmation": True, "confirmation_reasons": ["date_missing"],
            }],
            "relations": [],
        }, None)


class FakeReader:
    calls = 0

    def read_document(self, document_id):
        self.calls += 1
        assert document_id == DOC_ID
        return {"document_id": document_id, "revision_id": "5", "title": "例会纪要",
                "blocks": [{"index": 2, "block_type": 2, "text": "张三整理部署文档"}]}


class FakeAccessVerifier:
    def __init__(self):
        self.calls = []
        self.revision_calls = []
        self.revision = "5"

    def read_document(self, identity, document_id):
        self.calls.append((identity.actor_id, document_id))
        return {"document_id": document_id, "revision_id": "5", "title": "例会纪要",
                "blocks": [{"index": 2, "block_type": 2, "text": "张三整理部署文档"}]}

    def get_revision(self, identity, document_id):
        self.revision_calls.append((identity.actor_id, document_id))
        return self.revision


class FakeSender:
    def __init__(self):
        self.calls = []
        self.updates = []

    def send(self, open_id, card, idempotency_key):
        self.calls.append((open_id, card, idempotency_key))
        return "om_card"

    def update(self, message_id, card):
        self.updates.append((message_id, card))


def callback(*, open_id="ou_member", tenant_key="tenant-key", app_id="cli_app", message_id="om_source",
             chat_type="p2p", sender_type="user", message_type="text", text=None):
    if text is None:
        text = "请整理会议纪要 https://tenant.feishu.cn/docx/test-docx-id?from=copy"
    return SimpleNamespace(
        header=SimpleNamespace(app_id=app_id, tenant_key=tenant_key, event_id="event-1"),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_type=sender_type, sender_id=SimpleNamespace(open_id=open_id)),
            message=SimpleNamespace(message_id=message_id, chat_type=chat_type, message_type=message_type,
                                    content=json.dumps({"text": text})),
        ),
    )


def setup_gateway(tmp_path):
    cp = ControlPlane(SQLiteStore(tmp_path / "assistant.sqlite3"))
    cp.register_actor(Actor("member", "tenant", ActorType.USER, CAPS,
                            external_ref_hash=hashlib.sha256(b"ou_member").hexdigest()))
    cp.register_actor(Actor("assistant", "tenant", ActorType.PERSONAL_ASSISTANT, CAPS))
    cp.register_actor(Actor("meeting-agent", "tenant", ActorType.BUSINESS_AGENT, CAPS))
    cp.register_agent_version(AgentVersion("meeting-agent", 1, CAPS))
    cp.bind_assistant(AssistantBinding("binding", "tenant", "member", "assistant"))
    runtime, reader, sender = FakeRuntime(), FakeReader(), FakeSender()
    review = FeishuMeetingReviewGateway(cp, AgentOrchestrator(cp, runtime), reader, FakeAccessVerifier())
    gateway = FeishuPersonalAssistantGateway(
        review, app_id="cli_app", tenant_key="tenant-key", tenant_id="tenant",
        sender=sender, review_url="https://fde.example.test/h5#meetings",
        card_review_enabled=True,
    )
    return cp, gateway, runtime, reader, sender


def test_p2p_message_routes_through_bound_assistant_and_returns_draft_card(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    result = gateway.receive(callback())
    assert result["created"] is True
    assert result["card_status"] == "SENT"
    assert runtime.calls == 1 and reader.calls == 0
    assert cp.store.get_task_run(result["task_run_id"])["assistant_actor_id"] == "assistant"
    assert cp.store.get_meeting_record(result["record_id"])["submitted_by"] == "member"
    assert len(cp.store.list_meeting_todos(result["record_id"])) == 1
    assert cp.store.list_tool_calls(result["task_run_id"]) == []
    assert sender.calls[0][0] == "ou_member"
    assert result["card_mode"] == "FORM"
    assert sender.calls[0][1]["schema"] == "2.0"
    assert sender.calls[0][1]["body"]["elements"][1]["tag"] == "form"
    submit_button = sender.calls[0][1]["body"]["elements"][1]["elements"][-1]
    assert submit_button["action_type"] == "form_submit"
    assert "form_action_type" not in submit_button
    delivery = cp.store.get_assistant_card_delivery(result["record_id"])
    assert len(delivery["review_snapshot"]) == 64


def test_card_review_flag_off_keeps_existing_h5_card(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    gateway.card_review_enabled = False
    result = gateway.receive(callback())
    assert result["card_mode"] == "H5_REVIEW"
    assert sender.calls[0][1]["elements"][-1]["actions"][0]["url"].endswith("#meetings")
    assert cp.store.get_assistant_card_delivery(result["record_id"])["review_snapshot"] is None


def test_existing_assistant_card_table_gains_review_snapshot_column(tmp_path):
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE assistant_card_deliveries ("
            "record_id TEXT PRIMARY KEY, recipient_actor_id TEXT NOT NULL, "
            "idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, "
            "message_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.commit()
    finally:
        connection.close()
    with SQLiteStore(path) as store:
        columns = {row["name"] for row in store.connection.execute(
            "PRAGMA table_info(assistant_card_deliveries)").fetchall()}
        assert "review_snapshot" in columns


def review_callback(*, open_id="ou_member", message_id="om_card", app_id="cli_app",
                    tenant_key="tenant-key", values=None):
    return SimpleNamespace(
        header=SimpleNamespace(app_id=app_id, event_id="review-event"),
        event=SimpleNamespace(
            context=SimpleNamespace(open_message_id=message_id),
            operator=SimpleNamespace(open_id=open_id, tenant_key=tenant_key),
            action=SimpleNamespace(tag="button", name="meeting_review_submit",
                                   form_value=values if values is not None else {
                                       "assignee_0": "member", "due_0": "2026-10-20",
                                   }),
        ),
    )


def test_bound_form_review_creates_proposals_without_remote_task_write(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    intake = gateway.receive(callback())
    card_review = FeishuMeetingCardReviewGateway(gateway.meeting_gateway, app_id="cli_app",
                                                 tenant_id="tenant", tenant_key="tenant-key")
    result = card_review.receive(review_callback())
    assert result["accepted"] is True and result["status"] == "WAITING_APPROVAL"
    assert result["approval_count"] == 1
    assert gateway.meeting_gateway.event_access_verifier.revision_calls == [("member", DOC_ID)]
    assert cp.store.get_task_run(intake["task_run_id"])["status"] == "WAITING_APPROVAL"
    assert cp.store.list_meeting_todos(intake["record_id"])[0]["status"] == "READY_FOR_APPROVAL"
    assert cp.store.list_tool_calls(intake["task_run_id"]) == []
    assert card_review.receive(review_callback())["status"] == "ALREADY_SUBMITTED"
    assert len(cp.store.list_approvals(intake["task_run_id"])) == 1


def test_card_callback_queues_before_remote_revision_and_worker_recovers(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    intake = gateway.receive(callback())
    verifier = gateway.meeting_gateway.event_access_verifier
    card_review = FeishuMeetingCardReviewGateway(gateway.meeting_gateway, app_id="cli_app",
                                                 tenant_id="tenant", tenant_key="tenant-key")
    assert card_review.enqueue(review_callback()) == {"accepted": True, "status": "QUEUED"}
    assert card_review.enqueue(review_callback()) == {"accepted": True, "status": "DUPLICATE"}
    assert verifier.revision_calls == []
    assert cp.store.list_approvals(intake["task_run_id"]) == []
    dispatched = []
    path = str(tmp_path / "assistant.sqlite3")
    assert meeting_approval_bridge.process_review_one(
        db_path=path, tenant_id="tenant", tenant_key="tenant-key", app_id="cli_app",
        client=object(), user_client=object(), token_key="unused", review_url="https://fde.example.test/h5",
        user_reader=verifier, sender=sender, dispatch=lambda *args: dispatched.append(args),
    ) == "DONE"
    assert verifier.revision_calls == [("member", DOC_ID)]
    assert len(cp.store.list_approvals(intake["task_run_id"])) == 1
    assert dispatched == [(intake["record_id"], "member", "ou_member")]
    assert sender.updates[0][1]["header"]["title"]["content"] == "会议审核已提交"
    assert meeting_approval_bridge.process_review_one(
        db_path=path, tenant_id="tenant", tenant_key="tenant-key", app_id="cli_app",
        client=object(), user_client=object(), token_key="unused", review_url="https://fde.example.test/h5",
        user_reader=verifier, sender=sender,
    ) == "EMPTY"


def test_reclaimed_card_review_does_not_create_duplicate_proposals(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    intake = gateway.receive(callback())
    card_review = FeishuMeetingCardReviewGateway(gateway.meeting_gateway, app_id="cli_app",
                                                 tenant_id="tenant", tenant_key="tenant-key")
    assert card_review.enqueue(review_callback())["accepted"] is True
    cp.store.claim_meeting_card_review("tenant", "dead-worker", lease_seconds=1)
    cp.store.connection.execute(
        "UPDATE meeting_card_review_inbox SET lease_until = 0 WHERE message_id = 'om_card'"
    )
    # Simulate a worker that committed the review before losing its inbox ack.
    assert card_review.receive(review_callback())["accepted"] is True
    dispatched = []
    assert meeting_approval_bridge.process_review_one(
        db_path=str(tmp_path / "assistant.sqlite3"), tenant_id="tenant", tenant_key="tenant-key",
        app_id="cli_app", client=object(), user_client=object(), token_key="unused",
        review_url="https://fde.example.test/h5",
        user_reader=gateway.meeting_gateway.event_access_verifier, sender=sender,
        dispatch=lambda *args: dispatched.append(args),
    ) == "DONE"
    assert len(cp.store.list_approvals(intake["task_run_id"])) == 1
    assert len(dispatched) == 1


def test_card_review_queue_rejects_wrong_operator_and_worker_rejects_stale_snapshot(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    intake = gateway.receive(callback())
    card_review = FeishuMeetingCardReviewGateway(gateway.meeting_gateway, app_id="cli_app",
                                                 tenant_id="tenant", tenant_key="tenant-key")
    assert card_review.enqueue(review_callback(open_id="ou_other"))["status"] == "UNBOUND_OPERATOR"
    assert card_review.enqueue(review_callback())["status"] == "QUEUED"
    draft = cp.store.list_meeting_todos(intake["record_id"])[0]
    cp.store.save_meeting_todo(MeetingTodoDraft(**{**draft, "title": "更新后的标题"}))
    dispatched = []
    assert meeting_approval_bridge.process_review_one(
        db_path=str(tmp_path / "assistant.sqlite3"), tenant_id="tenant", tenant_key="tenant-key",
        app_id="cli_app", client=object(), user_client=object(), token_key="unused",
        review_url="https://fde.example.test/h5",
        user_reader=gateway.meeting_gateway.event_access_verifier, sender=sender,
        dispatch=lambda *args: dispatched.append(args),
    ) == "REJECTED"
    assert dispatched == [] and cp.store.list_approvals(intake["task_run_id"]) == []
    assert sender.updates[0][1]["header"]["title"]["content"] == "会议审核未通过核对"


def test_form_delivery_survives_store_restart(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    intake = gateway.receive(callback())
    path = cp.store.path
    cp.store.close()
    reopened = ControlPlane(SQLiteStore(path))
    review = FeishuMeetingReviewGateway(
        reopened, AgentOrchestrator(reopened, FakeRuntime()), FakeReader(), FakeAccessVerifier(),
    )
    card_review = FeishuMeetingCardReviewGateway(review, app_id="cli_app",
                                                 tenant_id="tenant", tenant_key="tenant-key")
    assert card_review.receive(review_callback())["accepted"] is True
    assert reopened.store.get_task_run(intake["task_run_id"])["status"] == "WAITING_APPROVAL"
    reopened.store.close()


def test_card_review_rejects_other_operator_message_stale_draft_and_invalid_fields(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    intake = gateway.receive(callback())
    card_review = FeishuMeetingCardReviewGateway(gateway.meeting_gateway, app_id="cli_app",
                                                 tenant_id="tenant", tenant_key="tenant-key")
    assert card_review.receive(review_callback(open_id="ou_other"))["accepted"] is False
    assert card_review.receive(review_callback(message_id="other_card"))["status"] == "UNBOUND_CARD"
    assert card_review.receive(review_callback(app_id="other_app"))["status"] == "INVALID_ACTION"
    assert card_review.receive(review_callback(tenant_key="other"))["status"] == "TENANT_MISMATCH"
    assert card_review.receive(review_callback(values={"assignee_0": "member", "due_0": "not-a-date"}))["status"] == "REVIEW_REJECTED"
    assert card_review.receive(review_callback(values={"assignee_0": "member", "due_0": "2026-10-20", "extra": "x"}))["status"] == "INVALID_FIELDS"
    assert cp.store.list_approvals(intake["task_run_id"]) == []
    draft = cp.store.list_meeting_todos(intake["record_id"])[0]
    cp.store.save_meeting_todo(MeetingTodoDraft(
        **{**draft, "title": "人工修改的标题"},
    ))
    assert card_review.receive(review_callback())["status"] == "STALE_REVIEW"
    assert cp.store.list_approvals(intake["task_run_id"]) == []


def test_card_review_rejects_source_revision_change_without_app_fallback(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    intake = gateway.receive(callback())
    gateway.meeting_gateway.event_access_verifier.revision = "6"
    card_review = FeishuMeetingCardReviewGateway(gateway.meeting_gateway, app_id="cli_app",
                                                 tenant_id="tenant", tenant_key="tenant-key")
    assert card_review.receive(review_callback())["status"] == "REVIEW_REJECTED"
    assert reader.calls == 0
    assert cp.store.list_approvals(intake["task_run_id"]) == []


def test_conflicted_or_large_review_stays_on_h5_path():
    source = {"evidence": [{"block_id": 2, "text": "带证据的会议结论"}]}
    member = [{"actor_id": "member", "name": "张三"}]
    draft = {"title": "整理文档", "status": "NEEDS_CONFIRMATION", "visibility_scope": "assignee",
             "evidence_block_ids": [2], "confirmation_reasons": ["date_missing"]}
    assert _form_review_todos([draft], source, member) is not None
    assert _form_review_todos([{**draft, "confirmation_reasons": ["POSSIBLE_DUPLICATE_OR_CONFLICT"]}],
                              source, member) is None
    assert _form_review_todos([draft] * 4, source, member) is None
    assert _form_review_todos([{**draft, "evidence_block_ids": [999]}], source, member) is None


def test_exact_unique_member_name_prefills_candidate_but_duplicate_name_does_not():
    payload = {"todos": [{"todo_id": "todo-1", "assignee_candidate": {
        "display_name": "张三", "status": "candidate",
    }}]}
    assert _unique_assignee_suggestions(payload, [{"actor_id": "a", "name": "张三"}]) == {"todo-1": "a"}
    assert _unique_assignee_suggestions(payload, [
        {"actor_id": "a", "name": "张三"}, {"actor_id": "b", "name": "张三"},
    ]) == {}
    payload["todos"][0]["assignee_candidate"]["status"] = "missing"
    assert _unique_assignee_suggestions(payload, [{"actor_id": "a", "name": "张三"}]) == {}


def test_unique_member_name_is_prefilled_in_card_but_still_requires_submission(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    cp.store.connection.execute(
        "INSERT INTO member_profiles(actor_id, tenant_id, display_name) VALUES (?, ?, ?)",
        ("member", "tenant", "张三"),
    )
    intake = gateway.receive(callback())
    draft = cp.store.list_meeting_todos(intake["record_id"])[0]
    assert draft["assignee_actor_id"] == "member"
    form = sender.calls[0][1]["body"]["elements"][1]
    select = next(field for field in form["elements"] if field.get("tag") == "select_static")
    assert select["initial_option"] == "member"
    assert cp.store.list_approvals(intake["task_run_id"]) == []


def test_duplicate_message_does_not_repeat_runtime_or_card(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    first = gateway.receive(callback())
    second = gateway.receive(callback())
    assert second["deduplicated"] is True
    assert second["task_run_id"] == first["task_run_id"]
    assert runtime.calls == 1
    assert len(sender.calls) == 1


def test_card_send_uncertainty_is_fenced_across_replay(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)

    def timeout(*_):
        raise TimeoutError("response lost")

    sender.send = timeout
    first = gateway.receive(callback())
    assert first["card_status"] == "UNKNOWN"
    second = gateway.receive(callback())
    assert second["card_status"] == "UNKNOWN"
    assert runtime.calls == 1
    assert cp.store.get_assistant_card_delivery(first["record_id"])["status"] == "UNKNOWN"


def test_untrusted_and_unonboarded_messages_do_not_read_document(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    for event in (
        callback(app_id="wrong"), callback(tenant_key="wrong"), callback(open_id="ou_other"),
        callback(sender_type="app"), callback(chat_type="group"),
    ):
        assert gateway.receive(event)["accepted"] is False
    assert runtime.calls == reader.calls == 0
    assert sender.calls == []


def test_event_without_user_document_access_proof_fails_before_app_read(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    gateway.meeting_gateway.event_access_verifier = None
    assert gateway.receive(callback())["status"] == "MEETING_INTAKE_FAILED"
    assert runtime.calls == reader.calls == 0
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == "ou_member"
    assert "test-docx-id" not in json.dumps(sender.calls[0][1])
    assert "actions" not in sender.calls[0][1]["elements"][-1]


def test_expired_grant_prompts_bound_member_once_without_app_read(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)

    class ExpiredGrant:
        def read_document(self, identity, document_id):
            assert identity.auth_mode == "feishu_event"
            raise UserDocumentAuthorizationRequired("expired")

    gateway.meeting_gateway.event_access_verifier = ExpiredGrant()
    first = gateway.receive(callback())
    second = gateway.receive(callback())
    assert first["status"] == second["status"] == "DOCUMENT_AUTH_REQUIRED"
    assert first["card_status"] == second["card_status"] == "SENT"
    assert runtime.calls == reader.calls == 0
    assert len(sender.calls) == 1
    recipient, card, _ = sender.calls[0]
    assert recipient == "ou_member"
    assert card["elements"][-1]["actions"][0]["url"] == "https://fde.example.test/oauth/start"
    assert "test-docx-id" not in json.dumps(card)
    notices = cp.store.connection.execute("SELECT * FROM notification_deliveries").fetchall()
    assert len(notices) == 1 and notices[0]["status"] == "SENT"


def test_failure_card_unknown_result_is_not_resent(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    gateway.meeting_gateway.event_access_verifier = None
    calls = []

    def timeout(*args):
        calls.append(args)
        raise TimeoutError("reply lost")

    sender.send = timeout
    assert gateway.receive(callback())["card_status"] == "UNKNOWN"
    assert gateway.receive(callback())["card_status"] == "UNKNOWN"
    assert len(calls) == 1
    notice = cp.store.connection.execute("SELECT status FROM notification_deliveries").fetchone()
    assert notice["status"] == "UNKNOWN"


def test_failed_runtime_informs_member_without_creating_draft_or_task(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)

    def failed_run(request):
        runtime.calls += 1
        return RuntimeResult("FAILED", None, "MODEL_UNAVAILABLE")

    runtime.run_once = failed_run
    result = gateway.receive(callback())
    assert result["status"] == "MEETING_INTAKE_FAILED"
    assert result["task_status"] == "FAILED"
    assert result["card_status"] == "SENT"
    assert runtime.calls == 1 and reader.calls == 0
    assert cp.store.get_meeting_record(result["record_id"]) is None
    assert cp.store.list_tool_calls(result["task_run_id"]) == []
    assert "MODEL_UNAVAILABLE" not in json.dumps(sender.calls[0][1])


def test_event_document_access_denial_never_reads_as_application(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)

    class DeniedUserReader:
        def read_document(self, identity, document_id):
            assert identity.auth_mode == "feishu_event"
            assert document_id == DOC_ID
            raise RuntimeError("FEISHU_DOCUMENT_READ_403")

    gateway.meeting_gateway.event_access_verifier = DeniedUserReader()
    assert gateway.receive(callback())["status"] == "MEETING_INTAKE_FAILED"
    assert runtime.calls == reader.calls == 0
    assert len(sender.calls) == 1
    assert "actions" not in sender.calls[0][1]["elements"][-1]


def test_ambiguous_or_unrelated_messages_do_not_start_meeting(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    assert gateway.receive(callback(text="你好"))["status"] == "NO_DOCUMENT_LINK"
    assert gateway.receive(callback(text=(
        "https://tenant.feishu.cn/docx/test-docx-id "
        "https://tenant.feishu.cn/docx/second-docx-id"
    )))["status"] == "MULTIPLE_DOCUMENT_LINKS"
    assert gateway.receive(callback(message_type="file"))["status"] == "IGNORED_MESSAGE_TYPE"
    assert runtime.calls == reader.calls == 0


def test_verified_callback_is_durably_queued_and_processed_off_callback(tmp_path, monkeypatch):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    db_path = str(tmp_path / "assistant.sqlite3")
    event = callback()
    assert personal_assistant_intake.enqueue_callback(
        cp.store, event, app_id="cli_app", tenant_key="tenant-key", tenant_id="tenant",
    ) == "QUEUED"
    assert personal_assistant_intake.enqueue_callback(
        cp.store, event, app_id="cli_app", tenant_key="tenant-key", tenant_id="tenant",
    ) == "DUPLICATE"
    assert runtime.calls == reader.calls == 0
    monkeypatch.setattr(personal_assistant_intake, "HermesRuntimeAdapter", lambda: runtime)
    monkeypatch.setattr(personal_assistant_intake, "FeishuDocumentRevisionReader", lambda _: reader)
    monkeypatch.setattr(personal_assistant_intake, "FeishuCardSender", lambda _: sender)
    assert personal_assistant_intake.process_one(
        db_path=db_path, tenant_id="tenant", tenant_key="tenant-key", app_id="cli_app",
        client=object(), review_url="https://fde.example.test/h5#meetings",
        event_access_verifier=FakeAccessVerifier(),
    ) == "DONE"
    assert runtime.calls == 1 and reader.calls == 0
    assert len(sender.calls) == 1
    assert personal_assistant_intake.process_one(
        db_path=db_path, tenant_id="tenant", tenant_key="tenant-key", app_id="cli_app",
        client=object(), review_url="https://fde.example.test/h5#meetings",
        event_access_verifier=FakeAccessVerifier(),
    ) == "EMPTY"


def test_inbox_worker_without_user_access_verifier_does_not_read_document(tmp_path, monkeypatch):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    assert personal_assistant_intake.enqueue_callback(
        cp.store, callback(), app_id="cli_app", tenant_key="tenant-key", tenant_id="tenant",
    ) == "QUEUED"
    monkeypatch.setattr(personal_assistant_intake, "HermesRuntimeAdapter", lambda: runtime)
    monkeypatch.setattr(personal_assistant_intake, "FeishuDocumentRevisionReader", lambda _: reader)
    monkeypatch.setattr(personal_assistant_intake, "FeishuCardSender", lambda _: sender)
    with pytest.raises(RuntimeError, match="user-scoped document reader"):
        personal_assistant_intake.process_one(
            db_path=str(tmp_path / "assistant.sqlite3"), tenant_id="tenant", tenant_key="tenant-key",
            app_id="cli_app", client=object(), review_url="https://fde.example.test/h5#meetings",
        )
    assert runtime.calls == reader.calls == 0
    assert sender.calls == []
    row = cp.store.connection.execute("SELECT status FROM assistant_message_inbox").fetchone()
    assert row["status"] == "PENDING"


def test_inbox_worker_missing_grant_sends_auth_notice_and_clears_event(tmp_path, monkeypatch):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    assert personal_assistant_intake.enqueue_callback(
        cp.store, callback(), app_id="cli_app", tenant_key="tenant-key", tenant_id="tenant",
    ) == "QUEUED"

    class ExpiredGrant:
        def read_document(self, identity, document_id):
            raise UserDocumentAuthorizationRequired("expired")

    monkeypatch.setattr(personal_assistant_intake, "HermesRuntimeAdapter", lambda: runtime)
    monkeypatch.setattr(personal_assistant_intake, "FeishuDocumentRevisionReader", lambda _: reader)
    monkeypatch.setattr(personal_assistant_intake, "FeishuCardSender", lambda _: sender)
    assert personal_assistant_intake.process_one(
        db_path=str(tmp_path / "assistant.sqlite3"), tenant_id="tenant", tenant_key="tenant-key",
        app_id="cli_app", client=object(), review_url="https://fde.example.test/h5#meetings",
        event_access_verifier=ExpiredGrant(),
    ) == "DONE"
    assert len(sender.calls) == 1
    assert runtime.calls == reader.calls == 0
    row = cp.store.connection.execute("SELECT status, data_json, result_json FROM assistant_message_inbox").fetchone()
    assert row["status"] == "DONE" and row["data_json"] == "{}"
    assert "ou_member" not in row["result_json"] and "test-docx-id" not in row["result_json"]


def test_invalid_token_key_does_not_claim_inbox_message(tmp_path):
    cp, _, _, _, _ = setup_gateway(tmp_path)
    assert personal_assistant_intake.enqueue_callback(
        cp.store, callback(), app_id="cli_app", tenant_key="tenant-key", tenant_id="tenant",
    ) == "QUEUED"
    with pytest.raises(ValueError, match="encryption key"):
        personal_assistant_intake.process_one(
            db_path=str(tmp_path / "assistant.sqlite3"), tenant_id="tenant",
            tenant_key="tenant-key", app_id="cli_app", client=object(),
            review_url="https://fde.example.test/h5#meetings", token_key="invalid",
            user_client=SimpleNamespace(config=SimpleNamespace(enable_set_token=True)),
        )
    row = cp.store.connection.execute("SELECT status FROM assistant_message_inbox").fetchone()
    assert row["status"] == "PENDING"


def test_reclaimed_inbox_message_keeps_single_agent_execution(tmp_path, monkeypatch):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    db_path = str(tmp_path / "assistant.sqlite3")
    assert personal_assistant_intake.enqueue_callback(
        cp.store, callback(), app_id="cli_app", tenant_key="tenant-key", tenant_id="tenant",
    ) == "QUEUED"
    claimed = cp.store.claim_assistant_message("tenant", "dead-worker", lease_seconds=1)
    cp.store.connection.execute(
        "UPDATE assistant_message_inbox SET lease_until = 0 WHERE message_hash = ?",
        (claimed["message_hash"],),
    )
    monkeypatch.setattr(personal_assistant_intake, "HermesRuntimeAdapter", lambda: runtime)
    monkeypatch.setattr(personal_assistant_intake, "FeishuDocumentRevisionReader", lambda _: reader)
    monkeypatch.setattr(personal_assistant_intake, "FeishuCardSender", lambda _: sender)
    assert personal_assistant_intake.process_one(
        db_path=db_path, tenant_id="tenant", tenant_key="tenant-key", app_id="cli_app",
        client=object(), review_url="https://fde.example.test/h5#meetings",
        event_access_verifier=FakeAccessVerifier(),
    ) == "DONE"
    assert runtime.calls == 1
    assert len(sender.calls) == 1
