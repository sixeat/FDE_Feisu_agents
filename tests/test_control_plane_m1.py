from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from fde_control_plane import (
    Actor,
    ActorType,
    AgentVersion,
    AssistantBinding,
    ControlPlane,
    Delegation,
    IdentityContext,
    RiskLevel,
    SQLiteStore,
    TaskStep,
    TaskRunStatus,
    ToolCallStatus,
    ToolDefinition,
)


CAPS = frozenset({"doc.read", "task.write"})


def make_plane(tmp_path):
    store = SQLiteStore(tmp_path / "m1.sqlite3")
    cp = ControlPlane(store)
    cp.register_actor(Actor("user-1", "tenant-1", ActorType.USER, CAPS))
    cp.register_actor(Actor("assistant-1", "tenant-1", ActorType.PERSONAL_ASSISTANT, CAPS))
    cp.register_actor(Actor("meeting-agent", "tenant-1", ActorType.BUSINESS_AGENT, CAPS))
    cp.register_actor(Actor("admin-1", "tenant-1", ActorType.USER, roles=frozenset({"admin"})))
    cp.register_agent_version(AgentVersion("meeting-agent", 1, CAPS))
    cp.bind_assistant(AssistantBinding("binding-1", "tenant-1", "user-1", "assistant-1"))
    task = cp.create_task_run("user-1", "assistant-1", "meeting-agent", 1)
    return cp, task


HIGH_TOOL = ToolDefinition("feishu.task.create", frozenset({"task.write"}), RiskLevel.HIGH, side_effect=True)


def make_proposal(cp, task, **kwargs):
    return cp.create_operation_proposal(
        task.task_run_id,
        "step-1",
        kwargs.get("operation_type", "create_task"),
        kwargs.get("target_ref", "task://demo"),
        kwargs.get("arguments", {"summary": "demo"}),
        HIGH_TOOL,
        operation_id=kwargs.get("operation_id"),
    )


def test_approval_outbox_and_execution(tmp_path):
    cp, task = make_plane(tmp_path)
    proposal = make_proposal(cp, task)
    approval = cp.get_approval_for_operation(proposal.operation_id)
    assert approval is not None
    assert cp.authorize_operation(task.task_run_id, HIGH_TOOL).requires_approval

    result = cp.handle_approval_callback(
        "event-1", approval.approval_id, "admin-1", True,
        proposal.action_version, proposal.proposal_hash,
    )
    assert result["accepted"] is True
    call = cp.dispatch_tool_call(result["tool_call_id"])
    assert call.status == ToolCallStatus.DISPATCHED
    assert cp.complete_tool_call(call.tool_call_id, True, remote_ref="remote-1").status == ToolCallStatus.SUCCEEDED
    assert cp._task(task.task_run_id).status.value == "SUCCEEDED"


def test_permission_intersection_denies_missing_capability(tmp_path):
    cp, task = make_plane(tmp_path)
    tool = ToolDefinition("knowledge.write", frozenset({"knowledge.write"}), RiskLevel.LOW, side_effect=True)
    decision = cp.authorize_operation(task.task_run_id, tool)
    assert decision.allowed is False
    with pytest.raises(PermissionError):
        cp.create_operation_proposal(task.task_run_id, "step", "write", "knowledge://1", {}, tool)


def test_duplicate_callback_and_tampered_proposal_are_blocked(tmp_path):
    cp, task = make_plane(tmp_path)
    proposal = make_proposal(cp, task)
    approval = cp.get_approval_for_operation(proposal.operation_id)
    first = cp.handle_approval_callback("event-1", approval.approval_id, "admin-1", True, 1, proposal.proposal_hash)
    duplicate = cp.handle_approval_callback("event-1", approval.approval_id, "admin-1", True, 1, proposal.proposal_hash)
    assert first["accepted"] and duplicate["duplicate"]
    second_click = cp.handle_approval_callback("event-2", approval.approval_id, "admin-1", True, 1, proposal.proposal_hash)
    assert second_click["accepted"] is True and second_click["duplicate"]

    cp2, task2 = make_plane(tmp_path / "tamper")
    proposal2 = make_proposal(cp2, task2)
    approval2 = cp2.get_approval_for_operation(proposal2.operation_id)
    tampered = cp2.handle_approval_callback("event-tamper", approval2.approval_id, "admin-1", True, 1, "wrong-hash")
    assert tampered["accepted"] is False and tampered["status"] == "INVALIDATED"


def test_approver_role_and_version_are_checked(tmp_path):
    cp, task = make_plane(tmp_path)
    proposal = make_proposal(cp, task)
    approval = cp.get_approval_for_operation(proposal.operation_id)
    denied = cp.handle_approval_callback("event-no-role", approval.approval_id, "user-1", True, 1, proposal.proposal_hash)
    assert denied["accepted"] is False

    cp2, task2 = make_plane(tmp_path / "version")
    proposal2 = make_proposal(cp2, task2)
    approval2 = cp2.get_approval_for_operation(proposal2.operation_id)
    cp2.register_agent_version(AgentVersion("meeting-agent", 1, CAPS, active=False))
    stale = cp2.handle_approval_callback("event-version", approval2.approval_id, "admin-1", True, 1, proposal2.proposal_hash)
    assert stale["accepted"] is False

    cp3, task3 = make_plane(tmp_path / "new-version")
    proposal3 = make_proposal(cp3, task3)
    approval3 = cp3.get_approval_for_operation(proposal3.operation_id)
    cp3.register_agent_version(AgentVersion("meeting-agent", 2, CAPS))
    old_approval = cp3.handle_approval_callback("event-old-version", approval3.approval_id, "admin-1", True, 1, proposal3.proposal_hash)
    assert old_approval["accepted"] is False


def test_sqlite_restart_preserves_approval_and_outbox(tmp_path):
    cp, task = make_plane(tmp_path)
    proposal = make_proposal(cp, task)
    approval = cp.get_approval_for_operation(proposal.operation_id)
    cp.close()

    reopened = ControlPlane(SQLiteStore(tmp_path / "m1.sqlite3"))
    restored = reopened.get_approval_for_operation(proposal.operation_id)
    assert restored is not None and restored.status.value == "PENDING"
    result = reopened.handle_approval_callback("event-restart", restored.approval_id, "admin-1", True, 1, proposal.proposal_hash)
    assert result["accepted"] is True
    assert reopened.store.get_tool_call(result["tool_call_id"]) is not None


def test_wake_key_is_atomic_and_result_survives_restart(tmp_path):
    db_path = tmp_path / "wake.sqlite3"
    first = SQLiteStore(db_path)
    second = SQLiteStore(db_path)
    assert first.claim_wake(
        "tenant-1:run-1:due:rev-3",
        tenant_id="tenant-1", task_run_id="run-1", trigger_type="due",
        source_revision="rev-3", claim_token="wake-a", data={"owner": "user-1"},
    )
    assert not second.claim_wake(
        "tenant-1:run-1:due:rev-3",
        tenant_id="tenant-1", task_run_id="run-1", trigger_type="due",
        source_revision="rev-3", claim_token="wake-b",
    )
    assert not second.complete_wake("tenant-1:run-1:due:rev-3", "wake-b", "COMPLETED")
    assert first.complete_wake(
        "tenant-1:run-1:due:rev-3", "wake-a", "COMPLETED", {"notification_key": "n-1"}
    )
    first.close()
    second.close()

    with SQLiteStore(db_path) as reopened:
        row = reopened.get_wake("tenant-1:run-1:due:rev-3")
        assert row is not None and row["status"] == "COMPLETED"
        assert json.loads(row["result_json"]) == {"notification_key": "n-1"}
        assert reopened.claim_wake(
            "tenant-1:run-1:due:rev-4",
            tenant_id="tenant-1", task_run_id="run-1", trigger_type="due",
            source_revision="rev-4", claim_token="wake-c",
        )


def test_existing_outbox_schema_is_migrated_without_replacing_rows(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE outbox (outbox_id TEXT PRIMARY KEY, write_idempotency_key TEXT NOT NULL UNIQUE, "
        "status TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "INSERT INTO outbox(outbox_id, write_idempotency_key, status, data_json) VALUES (?, ?, ?, ?)",
        ("legacy-outbox", "legacy-key", "READY", '{"tool_call_id":"legacy-call","operation_id":"legacy-op"}'),
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(db_path)
    columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(outbox)").fetchall()}
    row = store.get_outbox_by_key("legacy-key")
    assert {"claim_token", "lease_until", "attempt_count", "last_error"} <= columns
    assert row is not None and row["outbox_id"] == "legacy-outbox" and row["status"] == "READY"
    assert row["attempt_count"] == 0


def test_write_idempotency_and_unknown_recovery(tmp_path):
    cp, task = make_plane(tmp_path)
    proposal = make_proposal(cp, task, operation_id="fixed-operation")
    approval = cp.get_approval_for_operation(proposal.operation_id)
    result = cp.handle_approval_callback("event-fixed", approval.approval_id, "admin-1", True, 1, proposal.proposal_hash)
    again = cp.execute_approved(proposal.operation_id)
    assert again.tool_call_id == result["tool_call_id"]
    unknown = cp.mark_timeout(again.tool_call_id)
    assert unknown.status == ToolCallStatus.UNKNOWN
    with pytest.raises(RuntimeError):
        cp.dispatch_tool_call(again.tool_call_id)
    reconciling = cp.reconcile_tool_call(again.tool_call_id, remote_status="UNKNOWN")
    assert reconciling.status == ToolCallStatus.RECONCILING
    recovered = cp.reconcile_tool_call(again.tool_call_id, found_remote_ref="remote-2", remote_status="SUCCEEDED")
    assert recovered.status == ToolCallStatus.SUCCEEDED


def test_credentials_are_redacted_from_proposal_and_audit(tmp_path):
    cp, task = make_plane(tmp_path)
    proposal = make_proposal(cp, task, arguments={"summary": "x", "access_token": "do-not-store", "url": "https://x?a=1&ticket=secret"})
    text = json.dumps(proposal.arguments, ensure_ascii=True)
    assert "do-not-store" not in text and "secret" not in text
    audit_text = json.dumps(cp.store.list_audits(), ensure_ascii=True)
    assert "do-not-store" not in audit_text and "secret" not in audit_text


def test_concurrent_callbacks_share_one_approval_action_and_tool_call(tmp_path):
    cp, task = make_plane(tmp_path)
    proposal = make_proposal(cp, task)
    approval = cp.get_approval_for_operation(proposal.operation_id)
    # Separate connections model two callback workers.
    cp_a = ControlPlane(SQLiteStore(tmp_path / "m1.sqlite3"))
    cp_b = ControlPlane(SQLiteStore(tmp_path / "m1.sqlite3"))

    def approve(cp_instance):
        return cp_instance.handle_approval_callback(
            "event-worker-" + str(id(cp_instance)), approval.approval_id, "admin-1", True, 1, proposal.proposal_hash
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, (cp_a, cp_b)))
    assert sorted(result.get("duplicate", False) for result in results) == [False, True]
    count = cp.store.connection.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"]
    actions = cp.store.connection.execute("SELECT COUNT(*) AS n FROM approval_actions").fetchone()["n"]
    assert count == 1 and actions == 1


def test_identity_binding_delegation_and_task_state_guards(tmp_path):
    cp, task = make_plane(tmp_path)
    cp.register_actor(Actor("bound-user", "tenant-1", ActorType.USER, CAPS, external_ref_hash="subject-a"))
    cp.bind_assistant(AssistantBinding("binding-bound", "tenant-1", "bound-user", "assistant-1"))
    context = IdentityContext("tenant-1", "bound-user", "user_oauth", "subject-a", frozenset({"doc.read"}))
    assert cp.verify_identity(context, "doc.read").actor_id == "bound-user"
    with pytest.raises(PermissionError):
        cp.verify_identity(IdentityContext("tenant-1", "bound-user", "user_oauth", "subject-b"))
    with pytest.raises(PermissionError):
        cp.create_task_run("user-1", "assistant-1", "meeting-agent", 1, identity=context)

    cp.create_task_step(TaskStep("step-domain", task.task_run_id, "parse meeting", sequence=1))
    delegation = cp.delegate(Delegation("delegation-1", task.task_run_id, "assistant-1", "meeting-agent", CAPS))
    assert delegation.status == "ACTIVE"
    with pytest.raises(PermissionError):
        cp.delegate(Delegation("delegation-user-source", task.task_run_id, "user-1", "meeting-agent", CAPS))
    with pytest.raises(ValueError):
        cp.transition_task(task.task_run_id, TaskRunStatus.SUCCEEDED)
    cp.transition_task(task.task_run_id, TaskRunStatus.PLANNING)
    cp.transition_task(task.task_run_id, TaskRunStatus.RUNNING)
    assert cp._task(task.task_run_id).status == TaskRunStatus.RUNNING


def test_revision_expiry_and_meeting_contract_are_safe_gates(tmp_path):
    cp, task = make_plane(tmp_path)
    proposal = make_proposal(cp, task)
    approval = cp.get_approval_for_operation(proposal.operation_id)
    revised = cp.revise_operation_proposal(approval.approval_id, {"arguments": {"summary": "changed"}})
    assert revised.proposal.action_version == 2
    old = cp.handle_approval_callback("event-revised-old", approval.approval_id, "admin-1", True, 1, proposal.proposal_hash)
    assert old["accepted"] is False
    expired = cp.expire_approval(revised.approval_id)
    assert expired.status.value == "EXPIRED"
    expired_callback = cp.handle_approval_callback("event-expired", revised.approval_id, "admin-1", True, 2, revised.proposal.proposal_hash)
    assert expired_callback["accepted"] is False

    payload = {
        "schema_version": "meeting-agent.v1",
        "source": {"document_id_hash": "doc-hash", "revision_id": "6", "source_block_ids": [8]},
        "todos": [{"todo_id": "todo-1", "title": "整理部署文档", "evidence_block_ids": [8], "needs_confirmation": True}],
        "relations": [],
    }
    result = cp.validate_meeting_agent_result(payload, document_id_hash="doc-hash", revision_id="6", valid_block_ids={8})
    assert result.status == "NEEDS_HUMAN_REVIEW" and result.write_eligible_count == 0
    payload["source"]["revision_id"] = "5"
    stale = cp.validate_meeting_agent_result(payload, document_id_hash="doc-hash", revision_id="6", valid_block_ids={8})
    assert "STALE_REVISION" in stale.reasons
