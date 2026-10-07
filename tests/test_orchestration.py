from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import SimpleNamespace

import pytest

from fde_control_plane import (
    Actor,
    ActorType,
    AgentOrchestrator,
    AgentVersion,
    AssistantBinding,
    ControlPlane,
    FakeFeishuTaskGateway,
    FeishuApprovalGateway,
    IdentityContext,
    MeetingOutboxWorker,
    RunEvent,
    RuntimeResult,
    review_todo_ref,
    SQLiteStore,
    TaskRunStatus,
    build_meeting_approval_card,
    build_meeting_decided_card,
    handle_meeting_approval_action,
)


CAPS = frozenset({"doc.read", "task.write"})


def make_plane(tmp_path):
    cp = ControlPlane(SQLiteStore(tmp_path / "orchestration.sqlite3"))
    cp.register_actor(Actor("member-1", "tenant-1", ActorType.USER, CAPS, external_ref_hash="member-subject"))
    cp.register_actor(Actor("assistant-1", "tenant-1", ActorType.PERSONAL_ASSISTANT, CAPS))
    cp.register_actor(Actor("meeting-agent", "tenant-1", ActorType.BUSINESS_AGENT, CAPS))
    cp.register_actor(Actor("admin-1", "tenant-1", ActorType.USER, roles=frozenset({"admin"}), external_ref_hash="admin-subject"))
    cp.register_actor(Actor("other-1", "tenant-1", ActorType.USER, CAPS, external_ref_hash="other-subject"))
    cp.register_agent_version(AgentVersion("meeting-agent", 1, CAPS))
    cp.bind_assistant(AssistantBinding("binding-1", "tenant-1", "member-1", "assistant-1"))
    return cp


BLOCKS = [{"index": 8, "block_type": 2, "text": "张三负责整理部署文档，截止时间待确认。"}]


def reviewer(actor_id="member-1", subject="member-subject"):
    return IdentityContext("tenant-1", actor_id, "user_oauth", subject)


def payload():
    return {
        "schema_version": "meeting-agent.v1",
        "source": {"document_id_hash": "doc-hash", "revision_id": "7", "source_block_ids": [8]},
        "summary": {"text": "会议摘要", "evidence_block_ids": [8]},
        "todos": [{
            "todo_id": "todo-8", "title": "整理部署文档", "evidence_block_ids": [8],
            "assignee_candidate": {"display_name": "张三"},
            "due_date_candidate": {"raw_text": "待确认", "normalized_date": None, "status": "needs_context"},
            "needs_confirmation": True, "confirmation_reasons": ["date_missing"],
        }],
        "relations": [],
    }


@dataclass
class FakeRuntime:
    result: RuntimeResult
    request = None
    call_count: int = 0

    def run_once(self, request):
        self.request = request
        self.call_count += 1
        events = tuple(
            RunEvent(request.task_run_id, request.trace_id, event.sequence, event.event_type, event.occurred_at, event.summary)
            for event in self.result.events
        )
        return RuntimeResult(
            self.result.status, self.result.payload, self.result.external_run_ref,
            events, self.result.error_code, self.result.error_detail,
        )

    def health(self):
        return {"runtime": "fake"}


def test_personal_assistant_delegates_to_meeting_agent_and_persists_runtime(tmp_path):
    event = RunEvent("placeholder", "trace-1", 1, "accepted", "2026-09-27T00:00:00Z", "accepted")
    runtime = FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None, (event,)))
    cp = make_plane(tmp_path)
    result = AgentOrchestrator(cp, runtime).execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1",
        agent_id="meeting-agent", agent_version=1, document_id_hash="doc-hash",
        revision_id="7", blocks=BLOCKS, record_id="meeting-orchestrated", dispatch_key="feishu:event:1",
    )

    assert result.runtime_result.status == "SUCCEEDED"
    assert result.meeting_result is not None
    assert result.meeting_result.validation.status == "NEEDS_HUMAN_REVIEW"
    assert cp._task(result.task_run_id).status == TaskRunStatus.WAITING_REVIEW
    drafts = cp.store.list_meeting_todos("meeting-orchestrated")
    assert len(drafts) == 1 and drafts[0]["status"] == "NEEDS_CONFIRMATION"
    assert cp.store.list_approvals(result.task_run_id) == []
    assert cp.store.list_delegations(result.task_run_id)
    assert len(cp.store.list_runtime_events(result.task_run_id)) == 1
    assert runtime.request.input_ref["document_id_hash"] == "doc-hash"
    assert runtime.request.tool_policy_snapshot["allowed_tools"] == []


def test_failed_and_unknown_runtime_results_do_not_create_meeting_records(tmp_path):
    for status, expected in (("FAILED", TaskRunStatus.FAILED), ("UNKNOWN", TaskRunStatus.RECONCILING)):
        cp = make_plane(tmp_path / status.lower())
        runtime = FakeRuntime(RuntimeResult(status, None, None, (), error_code="TEST"))
        result = AgentOrchestrator(cp, runtime).execute_meeting_request(
            requester_identity=reviewer(),
            member_actor_id="member-1", assistant_actor_id="assistant-1",
            agent_id="meeting-agent", agent_version=1, document_id_hash="doc-hash",
            revision_id="7", blocks=BLOCKS, dispatch_key=f"feishu:event:{status.lower()}",
        )
        assert cp._task(result.task_run_id).status == expected
        assert result.meeting_result is None
        assert cp.store.get_meeting_record(result.task_run_id) is None


def test_dispatch_key_replay_returns_existing_task_without_running_runtime_again(tmp_path):
    event = RunEvent("placeholder", "trace-1", 1, "accepted", "2026-09-27T00:00:00Z", "accepted")
    runtime = FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None, (event,)))
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, runtime)
    first = orchestrator.execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="meeting-replay", dispatch_key="feishu:event:replay",
    )
    second = orchestrator.execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="meeting-replay", dispatch_key="feishu:event:replay",
    )
    assert second.deduplicated is True
    assert second.task_run_id == first.task_run_id
    assert runtime.call_count == 1
    assert len(cp.store.list_delegations(first.task_run_id)) == 1


def test_dispatch_key_conflict_is_rejected(tmp_path):
    runtime = FakeRuntime(RuntimeResult("FAILED", None, None, (), error_code="TEST"))
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, runtime)
    orchestrator.execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS, dispatch_key="feishu:event:conflict",
    )
    try:
        orchestrator.execute_meeting_request(
            requester_identity=reviewer(),
            member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
            document_id_hash="other-doc", revision_id="7", blocks=BLOCKS, dispatch_key="feishu:event:conflict",
        )
    except PermissionError as exc:
        assert "dispatch key" in str(exc)
    else:
        raise AssertionError("same dispatch key with different input must be rejected")


def test_runtime_event_for_another_task_fails_closed(tmp_path):
    class ForeignEventRuntime(FakeRuntime):
        def run_once(self, request):
            self.request = request
            self.call_count += 1
            return self.result

    foreign = RunEvent("other-task", "other-trace", 1, "accepted", "2026-09-27T00:00:00Z", "foreign")
    runtime = ForeignEventRuntime(RuntimeResult("SUCCEEDED", payload(), None, (foreign,)))
    cp = make_plane(tmp_path)
    result = AgentOrchestrator(cp, runtime).execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS, dispatch_key="feishu:event:foreign",
    )
    assert result.runtime_result.error_code == "RUNTIME_EVENT_INVALID"
    assert cp._task(result.task_run_id).status == TaskRunStatus.FAILED
    assert cp.store.list_runtime_events(result.task_run_id) == []


def test_review_to_approval_to_fake_feishu_task(tmp_path):
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="meeting-review", dispatch_key="feishu:event:review",
    )
    with pytest.raises(PermissionError):
        orchestrator.submit_meeting_review(
            task_run_id=execution.task_run_id, record_id="meeting-review", reviewer_identity=reviewer("other-1", "other-subject"),
            updates_by_todo={"todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"}},
        )
    with pytest.raises(ValueError, match="all todos need human confirmation"):
        orchestrator.submit_meeting_review(
            task_run_id=execution.task_run_id, record_id="meeting-review", reviewer_identity=reviewer(),
            updates_by_todo={"todo-8": {"assignee_actor_id": "member-1", "due_date": "next Friday"}},
        )
    submission = orchestrator.submit_meeting_review(
        task_run_id=execution.task_run_id, record_id="meeting-review", reviewer_identity=reviewer(),
        updates_by_todo={"todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"}},
    )
    assert cp._task(execution.task_run_id).status == TaskRunStatus.WAITING_APPROVAL
    assert len(submission.approval_ids) == 1
    assert cp.store.list_tool_calls(execution.task_run_id) == []
    approval = cp._approval_from_row(cp.store.get_approval(submission.approval_ids[0]))
    callback = cp.handle_approval_callback(
        "callback-review-1", approval.approval_id, "member-1", True,
        approval.proposal.action_version, approval.proposal.proposal_hash,
    )
    assert callback["accepted"] is True
    gateway = FakeFeishuTaskGateway()
    result = MeetingOutboxWorker(cp, gateway).execute(callback["tool_call_id"])
    assert result.status == "SUCCEEDED"
    assert len(gateway.tasks) == 1


def test_only_meeting_submitter_can_review_and_can_discard_candidate(tmp_path):
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(), member_actor_id="member-1", assistant_actor_id="assistant-1",
        agent_id="meeting-agent", agent_version=1, document_id_hash="doc-hash", revision_id="7",
        blocks=BLOCKS, record_id="meeting-discard", dispatch_key="feishu:event:discard",
    )
    with pytest.raises(PermissionError):
        orchestrator.submit_meeting_review(
            task_run_id=execution.task_run_id, record_id="meeting-discard",
            reviewer_identity=reviewer("admin-1", "admin-subject"),
            updates_by_todo={"todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"}},
        )
    submission = orchestrator.submit_meeting_review(
        task_run_id=execution.task_run_id, record_id="meeting-discard", reviewer_identity=reviewer(),
        updates_by_todo={"todo-8": {"discard": True}},
    )
    assert submission.approval_ids == ()
    assert cp._task(execution.task_run_id).status == TaskRunStatus.CANCELLED
    assert cp.store.get_meeting_todo("meeting-discard", "todo-8")["status"] == "DISCARDED"


def test_review_cannot_forge_evidence_or_mix_discard_with_edits(tmp_path):
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(), member_actor_id="member-1", assistant_actor_id="assistant-1",
        agent_id="meeting-agent", agent_version=1, document_id_hash="doc-hash", revision_id="7",
        blocks=BLOCKS, record_id="meeting-review-boundary", dispatch_key="feishu:event:review-boundary",
    )
    with pytest.raises(ValueError, match="unsupported draft fields"):
        orchestrator.submit_meeting_review(
            task_run_id=execution.task_run_id, record_id="meeting-review-boundary", reviewer_identity=reviewer(),
            updates_by_todo={"todo-8": {"evidence_block_ids": [999]}},
        )
    with pytest.raises(ValueError, match="discard cannot be combined"):
        orchestrator.submit_meeting_review(
            task_run_id=execution.task_run_id, record_id="meeting-review-boundary", reviewer_identity=reviewer(),
            updates_by_todo={"todo-8": {"discard": True, "due_date": "2026-10-02"}},
        )


def test_failed_batch_review_keeps_every_draft_unchanged(tmp_path):
    cp = make_plane(tmp_path)
    two_todos = payload()
    two_todos["source"]["source_block_ids"] = [8, 9]
    second = dict(two_todos["todos"][0])
    second.update(todo_id="todo-9", title="跟进接口联调", evidence_block_ids=[9])
    two_todos["todos"].append(second)
    blocks = BLOCKS + [{"index": 9, "block_type": 2, "text": "李四跟进接口联调。"}]
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", two_todos, None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(), member_actor_id="member-1", assistant_actor_id="assistant-1",
        agent_id="meeting-agent", agent_version=1, document_id_hash="doc-hash", revision_id="7",
        blocks=blocks, record_id="meeting-atomic-review", dispatch_key="feishu:event:atomic-review",
    )
    before = cp.store.list_meeting_todos("meeting-atomic-review")
    with pytest.raises(ValueError, match="unsupported draft fields"):
        orchestrator.submit_meeting_review(
            task_run_id=execution.task_run_id, record_id="meeting-atomic-review", reviewer_identity=reviewer(),
            updates_by_todo={
                "todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"},
                "todo-9": {"evidence_block_ids": [999]},
            },
        )
    assert cp.store.list_meeting_todos("meeting-atomic-review") == before
    assert cp.store.list_approvals(execution.task_run_id) == []
    assert cp._task(execution.task_run_id).status == TaskRunStatus.WAITING_REVIEW


def test_review_api_accepts_opaque_todo_reference_only_within_record(tmp_path):
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(), member_actor_id="member-1", assistant_actor_id="assistant-1",
        agent_id="meeting-agent", agent_version=1, document_id_hash="doc-hash", revision_id="7",
        blocks=BLOCKS, record_id="meeting-opaque-ref", dispatch_key="feishu:event:opaque-ref",
    )
    result = orchestrator.submit_meeting_review_refs(
        task_run_id=execution.task_run_id, record_id="meeting-opaque-ref", reviewer_identity=reviewer(),
        updates_by_todo_ref={review_todo_ref("todo-8"): {"assignee_actor_id": "member-1", "due_date": "2026-10-02"}},
    )
    assert len(result.approval_ids) == 1
    with pytest.raises(KeyError):
        orchestrator.submit_meeting_review_refs(
            task_run_id=execution.task_run_id, record_id="meeting-opaque-ref", reviewer_identity=reviewer(),
            updates_by_todo_ref={"000000000000": {}},
        )
def test_invalid_relation_contract_cannot_enter_review(tmp_path):
    invalid_payload = payload()
    invalid_payload["relations"] = [{
        "todo_ids": ["todo-8", "invented"],
        "relation": "possible_duplicate_or_conflict",
        "evidence_block_ids": [8],
        "needs_human_confirmation": True,
    }]
    cp = make_plane(tmp_path)
    result = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", invalid_payload, None))).execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="invalid-meeting", dispatch_key="feishu:event:invalid",
    )
    assert result.runtime_result.error_code == "MEETING_INGEST_FAILED"
    assert cp._task(result.task_run_id).status == TaskRunStatus.FAILED
    assert cp.store.get_meeting_record("invalid-meeting") is None


def test_possible_duplicate_remains_blocked_after_field_edits(tmp_path):
    two_blocks = BLOCKS + [{"index": 9, "block_type": 2, "text": "补充：部署文档由张三负责。"}]
    ambiguous = payload()
    ambiguous["source"]["source_block_ids"] = [8, 9]
    ambiguous["todos"].append({
        "todo_id": "todo-9", "title": "整理部署文档", "evidence_block_ids": [9],
        "needs_confirmation": True, "confirmation_reasons": ["possible_duplicate"],
        "due_date_candidate": {"raw_text": None, "normalized_date": None, "status": "missing"},
    })
    ambiguous["relations"] = [{
        "todo_ids": ["todo-8", "todo-9"], "relation": "possible_duplicate_or_conflict",
        "evidence_block_ids": [8, 9], "needs_human_confirmation": True,
    }]
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", ambiguous, None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=two_blocks,
        record_id="meeting-conflict", dispatch_key="feishu:event:conflict-review",
    )
    with pytest.raises(ValueError, match="all todos need human confirmation"):
        orchestrator.submit_meeting_review(
            task_run_id=execution.task_run_id, record_id="meeting-conflict", reviewer_identity=reviewer(),
            updates_by_todo={
                "todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"},
                "todo-9": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"},
            },
        )
    assert cp.store.list_approvals(execution.task_run_id) == []


def test_empty_agent_result_does_not_create_review_record(tmp_path):
    empty = payload()
    empty["todos"] = []
    cp = make_plane(tmp_path)
    result = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", empty, None))).execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="meeting-empty", dispatch_key="feishu:event:empty",
    )
    assert result.runtime_result.error_code == "MEETING_INGEST_FAILED"
    assert cp._task(result.task_run_id).status == TaskRunStatus.FAILED
    assert cp.store.get_meeting_record("meeting-empty") is None


def test_permission_revoked_after_review_blocks_approval_callback(tmp_path):
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="meeting-revoked", dispatch_key="feishu:event:revoked",
    )
    review = orchestrator.submit_meeting_review(
        task_run_id=execution.task_run_id, record_id="meeting-revoked", reviewer_identity=reviewer(),
        updates_by_todo={"todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"}},
    )
    approval = cp._approval_from_row(cp.store.get_approval(review.approval_ids[0]))
    cp.register_actor(Actor("member-1", "tenant-1", ActorType.USER, frozenset({"doc.read"})))
    decision = cp.handle_approval_callback(
        "callback-revoked", approval.approval_id, "member-1", True,
        approval.proposal.action_version, approval.proposal.proposal_hash,
    )
    assert decision["accepted"] is False
    assert "permission revoked" in decision["reason"]
    assert cp.store.list_tool_calls(execution.task_run_id) == []


def test_other_tenant_admin_cannot_approve_meeting_task(tmp_path):
    cp = make_plane(tmp_path)
    cp.register_actor(Actor("foreign-admin", "tenant-2", ActorType.USER, roles=frozenset({"admin"})))
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="meeting-foreign-admin", dispatch_key="feishu:event:foreign-admin",
    )
    review = orchestrator.submit_meeting_review(
        task_run_id=execution.task_run_id, record_id="meeting-foreign-admin", reviewer_identity=reviewer(),
        updates_by_todo={"todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"}},
    )
    approval = cp._approval_from_row(cp.store.get_approval(review.approval_ids[0]))
    decision = cp.handle_approval_callback(
        "callback-foreign-admin", approval.approval_id, "foreign-admin", True,
        approval.proposal.action_version, approval.proposal.proposal_hash,
    )
    assert decision["accepted"] is False
    assert cp.store.list_tool_calls(execution.task_run_id) == []


def test_approval_card_requires_verified_operator_and_is_idempotent(tmp_path):
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="meeting-card", dispatch_key="feishu:event:card",
    )
    review = orchestrator.submit_meeting_review(
        task_run_id=execution.task_run_id, record_id="meeting-card", reviewer_identity=reviewer(),
        updates_by_todo={"todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"}},
    )
    approval = cp._approval_from_row(cp.store.get_approval(review.approval_ids[0]))
    card = build_meeting_approval_card(approval, assignee_name="张三")
    actions = card["elements"][-1]["actions"]
    approve_value = actions[0]["value"]
    assert card["elements"][0]["text"]["content"] == "整理部署文档"
    assert approve_value["proposal_hash"] == approval.proposal.proposal_hash
    assert actions[1]["value"]["decision"] == "reject"
    with pytest.raises(PermissionError):
        handle_meeting_approval_action(
            cp, event_id="card-click-1",
            verified_operator=IdentityContext("tenant-1", "admin-1", "application", "wrong-subject"),
            value=approve_value,
        )
    assert cp.store.list_tool_calls(execution.task_run_id) == []
    operator = IdentityContext("tenant-1", "member-1", "application", "member-subject")
    first = handle_meeting_approval_action(cp, event_id="card-click-1", verified_operator=operator, value=approve_value)
    second = handle_meeting_approval_action(cp, event_id="card-click-1", verified_operator=operator, value=approve_value)
    assert first["accepted"] is True
    assert second["duplicate"] is True
    assert len(cp.store.list_tool_calls(execution.task_run_id)) == 1
    decided = cp._approval_from_row(cp.store.get_approval(approval.approval_id))
    final_card = build_meeting_decided_card(decided, assignee_name="张三")
    assert final_card["elements"][-1]["text"]["content"] == "已批准，等待任务执行"
    assert not any(element.get("tag") == "action" for element in final_card["elements"])


def test_feishu_card_delivery_binds_message_recipient_and_callback(tmp_path):
    open_id = "ou_meeting_submitter"
    cp = make_plane(tmp_path)
    cp.register_actor(Actor(
        "member-1", "tenant-1", ActorType.USER, CAPS,
        external_ref_hash=hashlib.sha256(open_id.encode()).hexdigest(),
    ))
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=IdentityContext("tenant-1", "member-1", "application", hashlib.sha256(open_id.encode()).hexdigest()),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="meeting-live-card", dispatch_key="feishu:event:live-card",
    )
    review = orchestrator.submit_meeting_review(
        task_run_id=execution.task_run_id, record_id="meeting-live-card",
        reviewer_identity=IdentityContext("tenant-1", "member-1", "application", hashlib.sha256(open_id.encode()).hexdigest()),
        updates_by_todo={"todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"}},
    )
    approval_id = review.approval_ids[0]
    approval = cp._approval_from_row(cp.store.get_approval(approval_id))
    assert approval.eligible_approver_ids == frozenset({"member-1"})
    assert approval.eligible_approver_roles == frozenset()
    admin_decision = cp.handle_approval_callback(
        "admin-click", approval_id, "admin-1", True, 1, approval.proposal.proposal_hash,
    )
    assert admin_decision["accepted"] is False

    class FakeSender:
        calls = []
        updates = []

        def send(self, recipient, card, key):
            self.calls.append((recipient, card, key))
            return "om_approval_card"

        def update(self, message_id, card):
            self.updates.append((message_id, card))

    sender = FakeSender()
    gateway = FeishuApprovalGateway(cp, sender, app_id="cli_test")
    message_id = gateway.send(
        approval_id, recipient_actor_id="member-1", recipient_open_id=open_id, assignee_name="张三",
        approve_label="确认测试回调",
    )
    assert message_id == "om_approval_card"
    assert gateway.send(approval_id, recipient_actor_id="member-1", recipient_open_id=open_id, assignee_name="张三") == message_id
    assert len(sender.calls) == 1
    assert sender.calls[0][1]["elements"][-1]["actions"][0]["text"]["content"] == "确认测试回调"
    value = sender.calls[0][1]["elements"][-1]["actions"][0]["value"]

    def callback(*, app_id="cli_test", message_id="om_approval_card", operator=open_id, event_id="click-1"):
        return SimpleNamespace(
            header=SimpleNamespace(app_id=app_id, event_id=event_id),
            event=SimpleNamespace(
                context=SimpleNamespace(open_message_id=message_id),
                operator=SimpleNamespace(open_id=operator),
                action=SimpleNamespace(tag="button", value=value),
            ),
        )

    assert gateway.receive(callback(app_id="cli_other"))["accepted"] is False
    assert gateway.receive(callback(message_id="om_forged"))["accepted"] is False
    assert gateway.receive(callback(operator="ou_other"))["accepted"] is False
    assert cp.store.list_tool_calls(execution.task_run_id) == []
    first = gateway.receive(callback())
    repeated = gateway.receive(callback())
    assert first["accepted"] is True and repeated["duplicate"] is True
    assert len(cp.store.list_tool_calls(execution.task_run_id)) == 1
    gateway.update_decided_card(approval_id, assignee_name="张三", test_mode=True)
    assert sender.updates[0][0] == "om_approval_card"
    assert not any(element.get("tag") == "action" for element in sender.updates[0][1]["elements"])
    assert "已确认测试，未创建任务" in sender.updates[0][1]["header"]["title"]["content"]
    result = MeetingOutboxWorker(cp, FakeFeishuTaskGateway()).execute(first["tool_call_id"])
    assert result.status == "SUCCEEDED"


def test_forged_member_identity_cannot_start_runtime(tmp_path):
    cp = make_plane(tmp_path)
    runtime = FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None))
    with pytest.raises(PermissionError):
        AgentOrchestrator(cp, runtime).execute_meeting_request(
            requester_identity=reviewer(subject="wrong-subject"),
            member_actor_id="member-1", assistant_actor_id="assistant-1",
            agent_id="meeting-agent", agent_version=1,
            document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
            dispatch_key="feishu:event:forged-member",
        )
    assert runtime.call_count == 0
    assert cp.store.get_dispatch("feishu:event:forged-member") is None


def test_permission_revoked_after_approval_stops_outbox_write(tmp_path):
    cp = make_plane(tmp_path)
    orchestrator = AgentOrchestrator(cp, FakeRuntime(RuntimeResult("SUCCEEDED", payload(), None)))
    execution = orchestrator.execute_meeting_request(
        requester_identity=reviewer(),
        member_actor_id="member-1", assistant_actor_id="assistant-1", agent_id="meeting-agent", agent_version=1,
        document_id_hash="doc-hash", revision_id="7", blocks=BLOCKS,
        record_id="meeting-late-revocation", dispatch_key="feishu:event:late-revocation",
    )
    review = orchestrator.submit_meeting_review(
        task_run_id=execution.task_run_id, record_id="meeting-late-revocation", reviewer_identity=reviewer(),
        updates_by_todo={"todo-8": {"assignee_actor_id": "member-1", "due_date": "2026-10-02"}},
    )
    approval = cp._approval_from_row(cp.store.get_approval(review.approval_ids[0]))
    callback = cp.handle_approval_callback(
        "callback-late-revocation", approval.approval_id, "member-1", True,
        approval.proposal.action_version, approval.proposal.proposal_hash,
    )
    assert callback["accepted"] is True
    cp.register_actor(Actor("member-1", "tenant-1", ActorType.USER, frozenset({"doc.read"}), external_ref_hash="member-subject"))
    gateway = FakeFeishuTaskGateway()
    result = MeetingOutboxWorker(cp, gateway).execute(callback["tool_call_id"])
    assert result.status == "FAILED"
    assert result.warning == "PERMISSION_REVOKED"
    assert gateway.tasks == {}
