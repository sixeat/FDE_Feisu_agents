import hashlib
import json
from types import SimpleNamespace

from scripts import personal_assistant_intake

from fde_control_plane import (
    Actor, ActorType, AgentOrchestrator, AgentVersion, AssistantBinding,
    ControlPlane, FeishuMeetingReviewGateway, FeishuPersonalAssistantGateway,
    RuntimeResult, SQLiteStore,
)


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

    def verify(self, identity, document_id):
        self.calls.append((identity.actor_id, document_id))


class FakeSender:
    def __init__(self):
        self.calls = []

    def send(self, open_id, card, idempotency_key):
        self.calls.append((open_id, card, idempotency_key))
        return "om_card"


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
    )
    return cp, gateway, runtime, reader, sender


def test_p2p_message_routes_through_bound_assistant_and_returns_draft_card(tmp_path):
    cp, gateway, runtime, reader, sender = setup_gateway(tmp_path)
    result = gateway.receive(callback())
    assert result["created"] is True
    assert result["card_status"] == "SENT"
    assert runtime.calls == reader.calls == 1
    assert cp.store.get_task_run(result["task_run_id"])["assistant_actor_id"] == "assistant"
    assert cp.store.get_meeting_record(result["record_id"])["submitted_by"] == "member"
    assert len(cp.store.list_meeting_todos(result["record_id"])) == 1
    assert cp.store.list_tool_calls(result["task_run_id"]) == []
    assert sender.calls[0][0] == "ou_member"
    assert sender.calls[0][1]["elements"][-1]["actions"][0]["url"].endswith("#meetings")


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
    assert sender.calls == []


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
    assert runtime.calls == reader.calls == 1
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
    assert personal_assistant_intake.process_one(
        db_path=str(tmp_path / "assistant.sqlite3"), tenant_id="tenant", tenant_key="tenant-key",
        app_id="cli_app", client=object(), review_url="https://fde.example.test/h5#meetings",
    ) == "REJECTED"
    assert runtime.calls == reader.calls == 0
    assert sender.calls == []


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
