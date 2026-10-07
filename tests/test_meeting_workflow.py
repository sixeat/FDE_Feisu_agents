from __future__ import annotations

import json
import sqlite3
import hashlib
from datetime import datetime, timezone

import pytest

from fde_control_plane import (
    Actor,
    ActorType,
    AgentVersion,
    AssistantBinding,
    ControlPlane,
    FakeFeishuTaskGateway,
    FeishuTaskGateway,
    MeetingOutboxWorker,
    MeetingWorkflow,
    normalize_document_blocks,
    RiskLevel,
    SQLiteStore,
)


CAPS = frozenset({"task.write", "doc.read"})


def test_task_v2_all_day_due_uses_milliseconds_and_rejects_invalid_date():
    import lark_oapi as lark

    gateway = FeishuTaskGateway(client=object())
    task = gateway._build_task_builder(lark, {"title": "test", "due_date": "2026-10-09"}, "idempotency").build()
    expected = int(datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp() * 1000)
    assert task.due.timestamp == str(expected)
    assert task.due.is_all_day is True
    with pytest.raises(ValueError, match="DUE_DATE_INVALID"):
        gateway._build_task_builder(lark, {"title": "test", "due_date": "not-a-date"}, "idempotency")


def test_task_v2_due_patch_updates_only_all_day_due():
    from types import SimpleNamespace

    class TaskApi:
        request = None

        def patch(self, request):
            self.request = request
            return SimpleNamespace(code=0)

    task_api = TaskApi()
    client = SimpleNamespace(task=SimpleNamespace(v2=SimpleNamespace(task=task_api)))
    gateway = FeishuTaskGateway(client=client)
    gateway.patch_task_due("remote-guid", "2026-10-09")
    request = task_api.request
    assert request.task_guid == "remote-guid"
    assert request.body.update_fields == ["due"]
    assert request.body.task.due.timestamp == str(int(datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp() * 1000))
    assert request.body.task.due.is_all_day is True
    with pytest.raises(ValueError, match="DUE_DATE_INVALID"):
        gateway.patch_task_due("remote-guid", "tomorrow")


def approved_call(tmp_path):
    cp, workflow, run = setup_workflow(tmp_path)
    workflow.ingest(record_id="recovery", tenant_id="tenant-1", submitted_by="user-1", payload=payload(),
                    document_id_hash="doc-hash", revision_id="7", valid_block_ids={8, 9})
    workflow.build_drafts(record_id="recovery", payload=payload(), assignee_actor_by_todo={"todo-1": "user-1"})
    workflow.revise_draft("recovery", "todo-2", discard=True)
    proposal, approval = workflow.create_task_proposals("recovery", run.task_run_id)[0]
    result = cp.handle_approval_callback("recovery-event", approval.approval_id, "user-1", True, 1, proposal.proposal_hash)
    return cp, result["tool_call_id"]


@pytest.mark.parametrize("result_kind", ["missing", "network", "candidate"])
def test_reconciliation_failure_stays_blocked_and_redacts_details(tmp_path, result_kind):
    from fde_control_plane.meeting import TaskLookupUncertain
    cp, call_id = approved_call(tmp_path)
    cp.dispatch_tool_call(call_id)
    cp.mark_timeout(call_id)
    class LookupGateway:
        def query_task(self, *args, **kwargs):
            if result_kind == "network":
                raise ConnectionError("secret-token-must-not-leak")
            if result_kind == "candidate":
                raise TaskLookupUncertain("REMOTE_CANDIDATE_REQUIRES_REVIEW")
            return None
    worker = MeetingOutboxWorker(cp, LookupGateway())
    result = worker.reconcile(call_id)
    assert result.status == "RECONCILING"
    assert worker.execute(call_id).warning == "RECONCILIATION_REQUIRED"
    assert cp.store.get_outbox_for_tool_call(call_id)["status"] == "RECONCILING"
    assert "secret-token" not in str(result) + str(cp.store.list_audits())


def test_reconcile_skips_prepared_terminal_and_live_claims(tmp_path):
    cp, call_id = approved_call(tmp_path)
    worker = MeetingOutboxWorker(cp, object())  # any query would fail the test
    assert worker.reconcile(call_id).warning == "RECONCILIATION_NOT_REQUIRED"
    cp.store.claim_outbox_for_tool_call(call_id, "live-worker", lease_seconds=60)
    cp.dispatch_tool_call(call_id)
    assert worker.reconcile(call_id).warning == "WORKER_BUSY"


def test_real_gateway_preflight_checks_source_and_assignee_before_create(tmp_path):
    cp, workflow, task = setup_workflow(tmp_path)
    document_id = "doc-real-preflight"
    document_hash = hashlib.sha256(document_id.encode()).hexdigest()[:12]
    data = payload()
    data["source"]["document_id_hash"] = document_hash
    workflow.ingest(record_id="strict", tenant_id="tenant-1", submitted_by="user-1", payload=data,
                    document_id_hash=document_hash, revision_id="7", valid_block_ids={8, 9})
    cp.store.save_meeting_source("strict", {"document_id": document_id, "revision_id": "7"})
    workflow.build_drafts(record_id="strict", payload=data, assignee_actor_by_todo={"todo-1": "user-1", "todo-2": "user-2"})
    workflow.revise_draft("strict", "todo-2", due_date="2026-10-03")
    proposal, approval = workflow.create_task_proposals("strict", task.task_run_id)[0]
    decision = cp.handle_approval_callback("strict-approval", approval.approval_id, "user-1", True, 1, proposal.proposal_hash)

    class StrictGateway(FakeFeishuTaskGateway):
        requires_source_preflight = True
        def __init__(self):
            super().__init__()
            self.preflight_calls = []
        def verify_assignee_binding(self, actor_id, expected_external_hash):
            self.preflight_calls.append(("assignee", actor_id))
        def verify_source_revision(self, document_id, expected_revision):
            self.preflight_calls.append(("source", document_id, expected_revision))

    gateway = StrictGateway()
    result = MeetingOutboxWorker(cp, gateway).execute(decision["tool_call_id"])
    assert result.status == "SUCCEEDED"
    assert gateway.preflight_calls == [("assignee", "user-1"), ("source", document_id, "7")]


def test_real_gateway_preflight_blocks_stale_source_without_dispatch(tmp_path):
    cp, workflow, task = setup_workflow(tmp_path)
    document_id = "doc-stale-preflight"
    document_hash = hashlib.sha256(document_id.encode()).hexdigest()[:12]
    data = payload()
    data["source"]["document_id_hash"] = document_hash
    workflow.ingest(record_id="stale", tenant_id="tenant-1", submitted_by="user-1", payload=data,
                    document_id_hash=document_hash, revision_id="7", valid_block_ids={8, 9})
    cp.store.save_meeting_source("stale", {"document_id": document_id, "revision_id": "6"})
    workflow.build_drafts(record_id="stale", payload=data, assignee_actor_by_todo={"todo-1": "user-1", "todo-2": "user-2"})
    workflow.revise_draft("stale", "todo-2", due_date="2026-10-03")
    proposal, approval = workflow.create_task_proposals("stale", task.task_run_id)[0]
    decision = cp.handle_approval_callback("stale-approval", approval.approval_id, "user-1", True, 1, proposal.proposal_hash)
    class StrictGateway(FakeFeishuTaskGateway):
        requires_source_preflight = True
    result = MeetingOutboxWorker(cp, StrictGateway()).execute(decision["tool_call_id"])
    assert result.status == "FAILED" and result.warning == "SOURCE_SNAPSHOT_MISSING"
    assert cp.store.get_tool_call(decision["tool_call_id"])["attempts"] == 0


def test_late_lookup_failure_cannot_undo_success_from_another_connection(tmp_path):
    cp, call_id = approved_call(tmp_path)
    cp.dispatch_tool_call(call_id)
    cp.mark_timeout(call_id)
    class RacingLookup:
        def query_task(self, *args, **kwargs):
            with SQLiteStore(cp.store.path) as other:
                ControlPlane(other).reconcile_tool_call(call_id, "verified-id", "SUCCEEDED")
            return None
    result = MeetingOutboxWorker(cp, RacingLookup()).reconcile(call_id)
    assert result.status == "SUCCEEDED" and result.remote_task_id == "verified-id"
    assert cp.store.get_outbox_for_tool_call(call_id)["status"] == "SUCCEEDED"


def test_call_and_outbox_reconciliation_roll_back_together(tmp_path):
    cp, call_id = approved_call(tmp_path)
    cp.dispatch_tool_call(call_id)
    cp.mark_timeout(call_id)
    cp.store.connection.execute("CREATE TRIGGER fail_outbox BEFORE UPDATE ON outbox BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        cp.reconcile_tool_call(call_id, "verified-id", "SUCCEEDED")
    assert cp.store.get_tool_call(call_id)["status"] == "UNKNOWN"
    assert cp.store.get_outbox_for_tool_call(call_id)["status"] == "READY"
    cp.store.connection.execute("DROP TRIGGER fail_outbox")
    cp.store.close()
    with SQLiteStore(tmp_path / "stage2.sqlite3") as reopened:
        ControlPlane(reopened).reconcile_tool_call(call_id, "verified-id", "SUCCEEDED")
        assert reopened.get_tool_call(call_id)["status"] == "SUCCEEDED"
        assert reopened.get_outbox_for_tool_call(call_id)["status"] == "SUCCEEDED"


def setup_workflow(tmp_path):
    cp = ControlPlane(SQLiteStore(tmp_path / "stage2.sqlite3"))
    cp.register_actor(Actor("user-1", "tenant-1", ActorType.USER, CAPS))
    cp.register_actor(Actor("user-2", "tenant-1", ActorType.USER, CAPS))
    cp.register_actor(Actor("assistant-1", "tenant-1", ActorType.PERSONAL_ASSISTANT, CAPS))
    cp.register_actor(Actor("meeting-agent", "tenant-1", ActorType.BUSINESS_AGENT, CAPS))
    cp.register_actor(Actor("admin-1", "tenant-1", ActorType.USER, roles=frozenset({"admin"})))
    cp.register_agent_version(AgentVersion("meeting-agent", 1, CAPS))
    cp.bind_assistant(AssistantBinding("binding-1", "tenant-1", "user-1", "assistant-1"))
    task = cp.create_task_run("user-1", "assistant-1", "meeting-agent", 1)
    return cp, MeetingWorkflow(cp), task


def payload():
    return {
        "schema_version": "meeting-agent.v1",
        "source": {"document_id_hash": "doc-hash", "revision_id": "7", "source_block_ids": [8, 9]},
        "summary": {"text": "会议摘要", "evidence_block_ids": [8]},
        "todos": [
            {
                "todo_id": "todo-1", "title": "整理部署文档", "evidence_block_ids": [8],
                "assignee_candidate": {"display_name": "张三"},
                "due_date_candidate": {"raw_text": "2026-10-02", "normalized_date": "2026-10-02", "status": "normalized"},
                "needs_confirmation": False,
            },
            {
                "todo_id": "todo-2", "title": "跟进接口联调", "evidence_block_ids": [9],
                "assignee_candidate": {"display_name": None},
                "due_date_candidate": {"raw_text": "待确认", "normalized_date": None, "status": "needs_context"},
                "needs_confirmation": True, "confirmation_reasons": ["assignee_missing", "date_missing"],
            },
        ],
        "relations": [],
    }


def test_meeting_record_draft_revision_and_approval_proposals(tmp_path):
    cp, workflow, task = setup_workflow(tmp_path)
    result = workflow.ingest(
        record_id="meeting-1", tenant_id="tenant-1", submitted_by="user-1", payload=payload(),
        document_id_hash="doc-hash", revision_id="7", valid_block_ids={8, 9},
    )
    assert result.record.revision_id == "7"
    assert result.validation.status == "NEEDS_HUMAN_REVIEW"
    drafts = workflow.build_drafts(record_id="meeting-1", payload=payload(), assignee_actor_by_todo={"todo-1": "user-1"})
    assert {draft.todo_id for draft in drafts} == {"todo-1", "todo-2"}
    with pytest.raises(ValueError):
        workflow.create_task_proposals("meeting-1", task.task_run_id)

    revised = workflow.revise_draft("meeting-1", "todo-2", assignee_actor_id="user-2", due_date="2026-10-03")
    assert revised.status == "READY_FOR_APPROVAL"
    proposals = workflow.create_task_proposals("meeting-1", task.task_run_id)
    assert len(proposals) == 2
    repeated_proposals = workflow.create_task_proposals("meeting-1", task.task_run_id)
    assert [item[1].approval_id for item in repeated_proposals] == [item[1].approval_id for item in proposals]
    for proposal, approval in proposals:
        decision = cp.handle_approval_callback(
            f"callback-{proposal.operation_id}", approval.approval_id, "user-1", True,
            proposal.action_version, proposal.proposal_hash,
        )
        assert decision["accepted"] is True


def test_meeting_cannot_assign_approval_to_another_submitter(tmp_path):
    cp, workflow, task = setup_workflow(tmp_path)
    workflow.ingest(
        record_id="forged-submitter", tenant_id="tenant-1", submitted_by="user-2",
        payload=payload(), document_id_hash="doc-hash", revision_id="7", valid_block_ids={8, 9},
    )
    workflow.build_drafts(
        record_id="forged-submitter", payload=payload(),
        assignee_actor_by_todo={"todo-1": "user-1", "todo-2": "user-2"},
    )
    workflow.revise_draft("forged-submitter", "todo-2", due_date="2026-10-03")
    with pytest.raises(PermissionError, match="submitter must match"):
        workflow.create_task_proposals("forged-submitter", task.task_run_id)
    assert cp.store.list_approvals(task.task_run_id) == []


def test_outbox_worker_idempotence_notification_failure_and_reconcile(tmp_path):
    cp, workflow, task = setup_workflow(tmp_path)
    workflow.ingest(
        record_id="meeting-2", tenant_id="tenant-1", submitted_by="user-1", payload=payload(),
        document_id_hash="doc-hash", revision_id="7", valid_block_ids={8, 9},
    )
    workflow.build_drafts(record_id="meeting-2", payload=payload(), assignee_actor_by_todo={"todo-1": "user-1", "todo-2": "user-2"})
    workflow.revise_draft("meeting-2", "todo-2", due_date="2026-10-03")
    proposals = workflow.create_task_proposals("meeting-2", task.task_run_id)
    proposal, approval = proposals[0]
    decision = cp.handle_approval_callback("callback-worker", approval.approval_id, "user-1", True, 1, proposal.proposal_hash)
    proposal2, approval2 = proposals[1]
    decision2 = cp.handle_approval_callback("callback-timeout", approval2.approval_id, "user-1", True, 1, proposal2.proposal_hash)
    gateway = FakeFeishuTaskGateway()
    gateway.fail_notifications = True
    worker = MeetingOutboxWorker(cp, gateway)
    result = worker.execute(decision["tool_call_id"])
    assert result.status == "SUCCEEDED" and result.notification_status == "FAILED"
    assert cp._task(task.task_run_id).status.value == "RUNNING"
    repeated = worker.execute(decision["tool_call_id"])
    assert repeated.notification_status == "ALREADY_SENT"
    assert len(gateway.tasks) == 1

    gateway.timeout_after_create = True
    unknown = worker.execute(decision2["tool_call_id"])
    assert unknown.status == "UNKNOWN"
    gateway.timeout_after_create = False
    reconciled = worker.reconcile(decision2["tool_call_id"])
    assert reconciled.status == "SUCCEEDED"
    assert len(gateway.tasks) == 2


def test_outbox_claim_lease_and_dispatch_fence_survive_worker_crash(tmp_path):
    cp, workflow, task = setup_workflow(tmp_path)
    workflow.ingest(
        record_id="meeting-lease", tenant_id="tenant-1", submitted_by="user-1", payload=payload(),
        document_id_hash="doc-hash", revision_id="7", valid_block_ids={8, 9},
    )
    workflow.build_drafts(record_id="meeting-lease", payload=payload(), assignee_actor_by_todo={"todo-1": "user-1", "todo-2": "user-2"})
    workflow.revise_draft("meeting-lease", "todo-2", due_date="2026-10-03")
    proposal, approval = workflow.create_task_proposals("meeting-lease", task.task_run_id)[0]
    decision = cp.handle_approval_callback("callback-lease", approval.approval_id, "user-1", True, 1, proposal.proposal_hash)
    tool_call_id = decision["tool_call_id"]

    first = cp.store.claim_outbox_for_tool_call(tool_call_id, "worker-a", now=100.0, lease_seconds=10)
    assert first is not None and first["status"] == "CLAIMED" and first["attempt_count"] == 1
    assert cp.store.claim_outbox_for_tool_call(tool_call_id, "worker-b", now=105.0, lease_seconds=10) is None

    # The first worker crashed before crossing the local dispatch fence. An
    # expired lease can be recovered and then safely dispatched once.
    recovered = cp.store.claim_outbox_for_tool_call(tool_call_id, "worker-b", now=111.0, lease_seconds=10)
    assert recovered is not None and recovered["claim_token"] == "worker-b"
    dispatched = cp.dispatch_tool_call(tool_call_id)
    assert dispatched.status.value == "DISPATCHED" and dispatched.attempts == 1

    # Once dispatch intent is durable, lease expiry cannot authorize a second
    # remote write and the service rejects direct redispatch.
    assert cp.store.claim_outbox_for_tool_call(tool_call_id, "worker-c", now=200.0, lease_seconds=10) is None
    with pytest.raises(RuntimeError, match="DISPATCHED"):
        cp.dispatch_tool_call(tool_call_id)
    reconciling = cp.reconcile_tool_call(tool_call_id, remote_status="UNKNOWN")
    assert reconciling.status.value == "RECONCILING"
    with pytest.raises(RuntimeError, match="reconciled"):
        cp.dispatch_tool_call(tool_call_id)


def test_worker_classifies_connection_loss_as_unknown_and_preserves_outbox_data(tmp_path):
    cp, workflow, task = setup_workflow(tmp_path)
    workflow.ingest(
        record_id="meeting-connection", tenant_id="tenant-1", submitted_by="user-1", payload=payload(),
        document_id_hash="doc-hash", revision_id="7", valid_block_ids={8, 9},
    )
    workflow.build_drafts(record_id="meeting-connection", payload=payload(), assignee_actor_by_todo={"todo-1": "user-1", "todo-2": "user-2"})
    workflow.revise_draft("meeting-connection", "todo-2", due_date="2026-10-03")
    proposal, approval = workflow.create_task_proposals("meeting-connection", task.task_run_id)[0]
    decision = cp.handle_approval_callback("callback-connection", approval.approval_id, "user-1", True, 1, proposal.proposal_hash)

    class ConnectionLossGateway(FakeFeishuTaskGateway):
        def create_task(self, payload, idempotency_key):
            raise ConnectionError("connection dropped after request")

    result = MeetingOutboxWorker(cp, ConnectionLossGateway()).execute(decision["tool_call_id"])
    assert result.status == "UNKNOWN"
    call = cp.store.get_tool_call(decision["tool_call_id"])
    outbox = cp.store.get_outbox_for_tool_call(decision["tool_call_id"])
    assert call["status"] == "UNKNOWN"
    assert outbox["status"] == "UNKNOWN"
    assert json.loads(outbox["data_json"])["operation_id"] == proposal.operation_id


def test_worker_run_once_consumes_recoverable_outbox_batch(tmp_path):
    cp, workflow, task = setup_workflow(tmp_path)
    workflow.ingest(
        record_id="meeting-run-once", tenant_id="tenant-1", submitted_by="user-1", payload=payload(),
        document_id_hash="doc-hash", revision_id="7", valid_block_ids={8, 9},
    )
    workflow.build_drafts(record_id="meeting-run-once", payload=payload(), assignee_actor_by_todo={"todo-1": "user-1", "todo-2": "user-2"})
    workflow.revise_draft("meeting-run-once", "todo-2", due_date="2026-10-03")
    proposal, approval = workflow.create_task_proposals("meeting-run-once", task.task_run_id)[0]
    decision = cp.handle_approval_callback("callback-run-once", approval.approval_id, "user-1", True, 1, proposal.proposal_hash)

    gateway = FakeFeishuTaskGateway()
    worker = MeetingOutboxWorker(cp, gateway)
    results = worker.run_once(limit=1)
    assert len(results) == 1 and results[0].status == "SUCCEEDED"
    outbox = cp.store.get_outbox_for_tool_call(decision["tool_call_id"])
    assert outbox["status"] == "SUCCEEDED" and outbox["attempt_count"] == 1
    assert worker.run_once(limit=1) == []


def test_multiple_meeting_tasks_report_partial_success(tmp_path):
    cp, workflow, task = setup_workflow(tmp_path)
    workflow.ingest(record_id="meeting-partial", tenant_id="tenant-1", submitted_by="user-1", payload=payload(),
                    document_id_hash="doc-hash", revision_id="7", valid_block_ids={8, 9})
    workflow.build_drafts(record_id="meeting-partial", payload=payload(), assignee_actor_by_todo={"todo-1": "user-1", "todo-2": "user-2"})
    workflow.revise_draft("meeting-partial", "todo-2", due_date="2026-10-03")
    proposals = workflow.create_task_proposals("meeting-partial", task.task_run_id)
    gateway = FakeFeishuTaskGateway()
    worker = MeetingOutboxWorker(cp, gateway)
    first, second = proposals
    cb1 = cp.handle_approval_callback("partial-1", first[1].approval_id, "user-1", True, 1, first[0].proposal_hash)
    cb2 = cp.handle_approval_callback("partial-2", second[1].approval_id, "user-1", True, 1, second[0].proposal_hash)
    worker.execute(cb1["tool_call_id"])
    gateway.fail_create = True
    failed = worker.execute(cb2["tool_call_id"])
    assert failed.status == "FAILED"
    assert cp._task(task.task_run_id).status.value == "PARTIAL_SUCCESS"


def test_document_blocks_keep_source_evidence_and_do_not_invent_dates(tmp_path):
    blocks = [
        {"index": 8, "block_type": 13, "text": "张三负责整理部署文档，截止日期为下周三。"},
        {"index": 9, "block_type": 2, "text": "李四跟进接口联调，截止时间待确认。"},
        {"index": 10, "block_type": 2, "text": "王五负责后续事项。"},
    ]
    payload = normalize_document_blocks(document_id_hash="doc-hash", revision_id=8, blocks=blocks)
    assert payload["source"]["source_block_ids"] == [8, 9, 10]
    assert payload["todos"][0]["due_date_candidate"]["normalized_date"] is None
    assert "relative_date_not_normalized" in payload["todos"][0]["confirmation_reasons"]
    cp, workflow, _ = setup_workflow(tmp_path)
    ingested = workflow.ingest_document_blocks(
        record_id="meeting-blocks", tenant_id="tenant-1", submitted_by="user-1",
        document_id_hash="doc-hash", revision_id=8, blocks=blocks,
    )
    assert ingested.record.source_block_ids == (8, 9, 10)
    drafts = workflow.build_drafts(record_id="meeting-blocks", payload=payload, assignee_actor_by_todo={})
    assert all(d.status == "NEEDS_CONFIRMATION" for d in drafts)
