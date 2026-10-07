import hashlib

import pytest

from fde_control_plane import (
    Actor, ActorType, AgentOrchestrator, AgentVersion, AssistantBinding,
    ControlPlane, FeishuMeetingReviewGateway, IdentityContext, RuntimeResult,
    SQLiteStore, TaskRunStatus, review_todo_ref,
)


DOCUMENT_ID = "test-docx-id"
DOCUMENT_HASH = hashlib.sha256(DOCUMENT_ID.encode()).hexdigest()[:12]
CAPS = frozenset({"doc.read", "task.write"})
SUBJECT = hashlib.sha256(b"ou_submitter").hexdigest()


class FakeRuntime:
    def run_once(self, request):
        return RuntimeResult("SUCCEEDED", {
            "schema_version": "meeting-agent.v1",
            "source": {"document_id_hash": DOCUMENT_HASH, "revision_id": "5", "source_block_ids": [2]},
            "summary": {"text": "会议摘要", "evidence_block_ids": [2]},
            "todos": [{
                "todo_id": "todo-2", "title": "整理部署文档", "evidence_block_ids": [2],
                "assignee_candidate": {"display_name": "张三", "status": "candidate"},
                "due_date_candidate": {"raw_text": None, "normalized_date": None, "status": "missing"},
                "needs_confirmation": True, "confirmation_reasons": ["date_missing"],
            }],
            "relations": [],
        }, None)


class FakeRevisionReader:
    def __init__(self, revision="5"):
        self.revision = revision
        self.calls = []

    def get_revision(self, document_id):
        self.calls.append(document_id)
        return self.revision


def setup_review(tmp_path, revision="5"):
    cp = ControlPlane(SQLiteStore(tmp_path / "review.sqlite3"))
    cp.register_actor(Actor("member", "tenant", ActorType.USER, CAPS, external_ref_hash=SUBJECT))
    cp.register_actor(Actor("admin", "tenant", ActorType.USER, CAPS, roles=frozenset({"admin"}), external_ref_hash="admin-subject"))
    cp.register_actor(Actor("assistant", "tenant", ActorType.PERSONAL_ASSISTANT, CAPS))
    cp.register_actor(Actor("agent", "tenant", ActorType.BUSINESS_AGENT, CAPS))
    cp.register_agent_version(AgentVersion("agent", 1, CAPS))
    cp.bind_assistant(AssistantBinding("binding", "tenant", "member", "assistant"))
    identity = IdentityContext("tenant", "member", "user_oauth", SUBJECT)
    orchestrator = AgentOrchestrator(cp, FakeRuntime())
    execution = orchestrator.execute_meeting_request(
        requester_identity=identity, member_actor_id="member", assistant_actor_id="assistant",
        agent_id="agent", agent_version=1, document_id_hash=DOCUMENT_HASH,
        revision_id="5", blocks=[{"index": 2, "block_type": 2, "text": "张三整理部署文档"}],
        record_id="record", dispatch_key="test-review-gateway",
    )
    reader = FakeRevisionReader(revision)
    gateway = FeishuMeetingReviewGateway(cp, orchestrator, reader)
    edits = {review_todo_ref("todo-2"): {"assignee_actor_id": "member", "due_date": "2026-10-05"}}
    return cp, gateway, reader, identity, execution.task_run_id, edits


def test_fresh_document_allows_submitter_to_create_pending_proposal(tmp_path):
    cp, gateway, reader, identity, task_id, edits = setup_review(tmp_path)
    result = gateway.submit(
        task_run_id=task_id, record_id="record", document_id=DOCUMENT_ID,
        reviewer_identity=identity, updates_by_todo_ref=edits,
    )
    assert reader.calls == [DOCUMENT_ID]
    assert len(result.approval_ids) == 1
    assert cp._task(task_id).status == TaskRunStatus.WAITING_APPROVAL
    assert cp.store.list_tool_calls(task_id) == []


def test_stale_document_blocks_review_without_changing_drafts(tmp_path):
    cp, gateway, reader, identity, task_id, edits = setup_review(tmp_path, revision="6")
    before = cp.store.list_meeting_todos("record")
    with pytest.raises(ValueError, match="source document changed"):
        gateway.submit(
            task_run_id=task_id, record_id="record", document_id=DOCUMENT_ID,
            reviewer_identity=identity, updates_by_todo_ref=edits,
        )
    assert reader.calls == [DOCUMENT_ID]
    assert cp.store.list_meeting_todos("record") == before
    assert cp.store.list_approvals(task_id) == []
    assert cp._task(task_id).status == TaskRunStatus.WAITING_REVIEW
    assert cp.store.list_audits()[-1]["outcome"] == "STALE"


def test_document_read_failure_blocks_review_and_is_audited(tmp_path):
    cp, gateway, reader, identity, task_id, edits = setup_review(tmp_path)

    def fail(_document_id):
        raise RuntimeError("FEISHU_DOCUMENT_READ_403")

    reader.get_revision = fail
    before = cp.store.list_meeting_todos("record")
    with pytest.raises(RuntimeError, match="FEISHU_DOCUMENT_READ_403"):
        gateway.submit(
            task_run_id=task_id, record_id="record", document_id=DOCUMENT_ID,
            reviewer_identity=identity, updates_by_todo_ref=edits,
        )
    assert cp.store.list_meeting_todos("record") == before
    assert cp.store.list_approvals(task_id) == []
    assert cp.store.list_audits()[-1]["outcome"] == "ERROR"


@pytest.mark.parametrize(
    ("document_id", "identity"),
    [
        ("other-docx", IdentityContext("tenant", "member", "user_oauth", SUBJECT)),
        (DOCUMENT_ID, IdentityContext("tenant", "member", "application", SUBJECT)),
        (DOCUMENT_ID, IdentityContext("tenant", "admin", "user_oauth", "admin-subject")),
    ],
)
def test_unbound_document_or_reviewer_is_rejected_before_feishu_read(tmp_path, document_id, identity):
    cp, gateway, reader, _, task_id, edits = setup_review(tmp_path)
    with pytest.raises(PermissionError):
        gateway.submit(
            task_run_id=task_id, record_id="record", document_id=document_id,
            reviewer_identity=identity, updates_by_todo_ref=edits,
        )
    assert reader.calls == []
    assert cp.store.list_approvals(task_id) == []
