import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from fde_control_plane import (
    FollowUpStatus,
    build_daily_follow_up_digest,
    build_remote_task_observation,
    OwnerDecision,
    OwnerDecisionEvent,
    apply_owner_decision_event,
    SQLiteStore,
    build_owner_decision_report,
    persist_owner_decision_report,
    persist_remote_task_observation,
    extract_remote_task_snapshot,
    map_remote_task_status,
)
from fde_control_plane.models import (
    Actor, ActorType, AgentVersion, AssistantBinding, IdentityContext, RiskLevel, ToolDefinition,
)
from fde_control_plane import ControlPlane


@pytest.fixture
def owner_action(tmp_path):
    """Use a genuinely approved/succeeded local operation, no remote APIs."""
    with SQLiteStore(tmp_path / "owner.sqlite3") as store:
        cp = ControlPlane(store)
        caps = frozenset({"task.write"})
        for actor_id, kind in [("organizer", ActorType.USER), ("owner", ActorType.USER),
                               ("assistant", ActorType.PERSONAL_ASSISTANT), ("agent", ActorType.BUSINESS_AGENT)]:
            cp.register_actor(Actor(actor_id, "tenant-1", kind, caps, external_ref_hash=actor_id + "-hash"))
        cp.register_agent_version(AgentVersion("agent", 1, caps))
        cp.bind_assistant(AssistantBinding("binding", "tenant-1", "organizer", "assistant"))
        run = cp.create_task_run("organizer", "assistant", "agent", 1)
        proposal = cp.create_operation_proposal(
            run.task_run_id, "step", "task.create", "tasks", {"assignee_actor_id": "owner"},
            ToolDefinition("task.create", caps, RiskLevel.HIGH, True),
            eligible_approver_ids=frozenset({"organizer"}),
        )
        approval = cp.get_approval_for_operation(proposal.operation_id)
        result = cp.handle_approval_callback("approve", approval.approval_id, "organizer", True, 1, proposal.proposal_hash)
        cp.dispatch_tool_call(result["tool_call_id"])
        cp.complete_tool_call(result["tool_call_id"], True, remote_ref="remote-task")
        event = OwnerDecisionEvent("event-1", "tenant-1", run.task_run_id, "remote-task", "owner",
                                   OwnerDecision.ACCEPTED, proposal.proposal_hash)
        identity = IdentityContext("tenant-1", "owner", "user_oauth", "owner-hash")
        yield store, event, identity


def test_owner_event_and_action_replay_survive_restart(owner_action):
    store, event, identity = owner_action
    first = apply_owner_decision_event(store, event, identity=identity)
    assert first["accepted"] and first["status"] == "IN_PROGRESS"
    assert first["remote_write"] is False and first["notification_sent"] is False
    with SQLiteStore(store.path) as reopened:
        assert apply_owner_decision_event(reopened, event, identity=identity)["duplicate"]
        assert apply_owner_decision_event(reopened, replace(event, event_id="new-delivery"), identity=identity)["duplicate"]
        rows = reopened.list_follow_up_observations(event.task_run_id)
        assert len(rows) == 1 and rows[0]["remote_task_id"] == event.remote_task_id
        assert rows[0]["tenant_id"] == event.tenant_id and rows[0]["observed_at"]
        assert len([a for a in reopened.list_audits() if a["event_type"] == "OWNER_DECISION_RECORDED"]) == 1


@pytest.mark.parametrize("change,identity_change,expected", [
    ({}, {"actor_id": "organizer", "subject_ref_hash": "organizer-hash"}, "OWNER_IDENTITY_MISMATCH"),
    ({"owner_actor_id": "organizer"}, {"actor_id": "organizer", "subject_ref_hash": "organizer-hash"}, "OWNER_NOT_ASSIGNED"),
    ({}, {"subject_ref_hash": "wrong-subject"}, "OWNER_IDENTITY_INVALID"),
    ({"tenant_id": "tenant-2"}, {}, "OWNER_IDENTITY_MISMATCH"),
    ({"remote_task_id": "unrelated-task"}, {}, "OWNER_TASK_BINDING_INVALID"),
    ({"source_revision": "old-proposal-hash"}, {}, "OWNER_ACTION_STALE"),
    ({"decision": "RETURNED", "reason": "  "}, {}, "RETURN_REASON_REQUIRED"),
])
def test_owner_event_rejects_unauthorized_or_stale_input(owner_action, change, identity_change, expected):
    store, event, identity = owner_action
    result = apply_owner_decision_event(store, replace(event, **change), identity=replace(identity, **identity_change))
    assert result["reason"] == expected and not result["accepted"]
    assert store.list_follow_up_observations(event.task_run_id) == []
    # Rejected input cannot poison the valid owner's subsequent event.
    assert apply_owner_decision_event(store, event, identity=identity)["accepted"]


def test_owner_replay_revalidates_identity_and_binds_payload(owner_action):
    store, event, identity = owner_action
    apply_owner_decision_event(store, event, identity=identity)
    changed = replace(event, decision="RETURNED", reason="need a different date")
    assert apply_owner_decision_event(store, changed, identity=identity)["reason"] == "OWNER_EVENT_PAYLOAD_MISMATCH"
    assert apply_owner_decision_event(store, replace(changed, event_id="second"), identity=identity)["reason"] == "OWNER_DECISION_ALREADY_RECORDED"
    store.connection.execute("UPDATE actors SET active = 0 WHERE actor_id = 'owner'")
    assert apply_owner_decision_event(store, event, identity=identity)["reason"] == "OWNER_IDENTITY_INVALID"


def test_concurrent_owner_clicks_have_one_winner(owner_action):
    store, event, identity = owner_action
    def submit(index):
        with SQLiteStore(store.path) as other:
            choice = replace(event, event_id=f"click-{index}", decision="ACCEPTED" if index % 2 else "RETURNED", reason="date")
            return apply_owner_decision_event(other, choice, identity=identity)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(8)))
    assert sum(r["accepted"] and not r.get("duplicate") for r in results) == 1
    assert len(store.list_follow_up_observations(event.task_run_id)) == 1


def test_owner_decision_rolls_back_if_audit_insert_fails(owner_action):
    store, event, identity = owner_action
    store.connection.execute("CREATE TRIGGER fail_owner_audit BEFORE INSERT ON audit_events "
                             "WHEN NEW.event_type = 'OWNER_DECISION_RECORDED' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        apply_owner_decision_event(store, event, identity=identity)
    assert store.list_follow_up_observations(event.task_run_id) == []
    store.connection.execute("DROP TRIGGER fail_owner_audit")
    # A persisted inbox from the failed transaction would falsely return duplicate.
    assert not apply_owner_decision_event(store, event, identity=identity).get("duplicate")


@pytest.mark.parametrize("remote_status", ["DONE", "new-unknown-status"])
def test_owner_cannot_accept_terminal_or_uncertain_remote_task(owner_action, remote_status):
    store, event, identity = owner_action
    persist_remote_task_observation(store, build_remote_task_observation(
        tenant_id=event.tenant_id, task_run_id=event.task_run_id,
        snapshot=extract_remote_task_snapshot({"guid": event.remote_task_id, "status": remote_status}),
    ))
    assert apply_owner_decision_event(store, event, identity=identity)["reason"] == "OWNER_TASK_REQUIRES_RECONCILIATION"


@pytest.mark.parametrize("decision,remote_status,expected", [
    ("ACCEPTED", "TODO", "IN_PROGRESS"), ("RETURNED", "TODO", "WAITING_HUMAN"),
    ("ACCEPTED", "DONE", "COMPLETED"), ("RETURNED", "DONE", "WAITING_HUMAN"),
    ("ACCEPTED", "unrecognized", "UNKNOWN"), ("RETURNED", "unrecognized", "UNKNOWN"),
])
def test_remote_poll_does_not_erase_owner_decision(owner_action, decision, remote_status, expected):
    store, event, identity = owner_action
    apply_owner_decision_event(store, replace(event, decision=decision, reason="date"), identity=identity)
    persist_remote_task_observation(store, build_remote_task_observation(
        tenant_id=event.tenant_id, task_run_id=event.task_run_id,
        snapshot=extract_remote_task_snapshot({"guid": event.remote_task_id, "status": remote_status}),
        observed_at="2099-01-01T00:00:00Z",
    ))
    digest = build_daily_follow_up_digest(
        tenant_id=event.tenant_id, business_date="2099-01-01", recipient_actor_id="organizer",
        observations=store.list_follow_up_observations_for_tenant(event.tenant_id),
    )
    assert digest.counts[expected] == 1 and sum(digest.counts.values()) == 1


def test_owner_acceptance_persists_in_progress_report_and_deduplicates_notification(tmp_path):
    report = build_owner_decision_report(
        tenant_id="tenant-1", task_run_id="run-1", owner_actor_id="owner-1",
        recipient_actor_id="member-1",
        decision=OwnerDecision.ACCEPTED, source_revision="remote-4",
    )
    assert report.status == FollowUpStatus.IN_PROGRESS
    with SQLiteStore(tmp_path / "follow-up.sqlite3") as store:
        persist_owner_decision_report(store, report, tenant_id="tenant-1", observation_id="obs-1")
        assert store.claim_notification(
        report.notification_key, tenant_id="tenant-1", recipient_actor_id="member-1",
            channel="assistant-report", claim_token="notify-1", data={"status": report.status.value},
        )
        assert not store.claim_notification(
            report.notification_key, tenant_id="tenant-1", recipient_actor_id="member-1",
            channel="assistant-report", claim_token="notify-2",
        )
        assert store.complete_notification(report.notification_key, "notify-1", "SENT")
        assert store.list_follow_up_observations("run-1")[0]["status"] == "IN_PROGRESS"


def test_owner_return_requires_reason_and_waits_for_human_without_reassignment(tmp_path):
    with pytest.raises(ValueError, match="reason"):
        build_owner_decision_report(
            tenant_id="tenant-1", task_run_id="run-2", owner_actor_id="owner-2",
            recipient_actor_id="member-2",
            decision="RETURNED", source_revision=5,
        )
    report = build_owner_decision_report(
        tenant_id="tenant-1", task_run_id="run-2", owner_actor_id="owner-2",
        recipient_actor_id="member-2",
        decision="RETURNED", source_revision=5, reason="截止日期不清楚",
    )
    assert report.status == FollowUpStatus.WAITING_HUMAN
    assert "改派" in report.next_action
    with SQLiteStore(tmp_path / "returned.sqlite3") as store:
        persist_owner_decision_report(store, report, tenant_id="tenant-1", observation_id="obs-2")
        assert store.get_notification(report.notification_key) is None
        data = store.list_follow_up_observations("run-2")[0]
        assert data["reason"] == "截止日期不清楚"
        assert json.dumps(data, ensure_ascii=False)


def test_remote_task_status_mapping_preserves_unknown_and_due_boundaries():
    assert map_remote_task_status("todo").status == FollowUpStatus.WAITING_OWNER
    assert map_remote_task_status("doing").status == FollowUpStatus.IN_PROGRESS
    assert map_remote_task_status("done").status == FollowUpStatus.COMPLETED
    assert map_remote_task_status("cancelled").status == FollowUpStatus.WAITING_HUMAN
    unknown = map_remote_task_status("new_status_from_feishu")
    assert unknown.status == FollowUpStatus.UNKNOWN
    assert unknown.reason == "REMOTE_STATUS_UNRECOGNIZED"

    now = date(2026, 10, 5)
    assert map_remote_task_status("todo", due_at="2026-10-04", now=now).status == FollowUpStatus.OVERDUE
    assert map_remote_task_status("in_progress", due_at="2026-10-06", now=now).status == FollowUpStatus.DUE_SOON
    invalid = map_remote_task_status("todo", due_at="not-a-date", now=now)
    assert invalid.status == FollowUpStatus.WAITING_OWNER and invalid.reason == "DUE_DATE_INVALID"


def test_remote_task_snapshot_accepts_sdk_like_objects_and_rejects_missing_guid():
    class Due:
        timestamp = 1791158400000

    class Task:
        guid = "remote-task-1"
        status = "DONE"
        due = Due()

    snapshot = extract_remote_task_snapshot(Task())
    assert snapshot.remote_task_id == "remote-task-1"
    assert snapshot.mapping.status == FollowUpStatus.COMPLETED
    assert snapshot.mapping.due_at is not None and snapshot.mapping.due_at.endswith("Z")

    seconds = extract_remote_task_snapshot({"guid": "remote-task", "status": "todo",
                                            "due": {"timestamp": 1791158400}})
    assert seconds.mapping.status == FollowUpStatus.UNKNOWN
    assert seconds.mapping.reason == "REMOTE_DUE_TIMESTAMP_UNIT_INVALID"

    missing = extract_remote_task_snapshot({"status": "DONE"})
    assert missing.remote_task_id is None
    assert missing.mapping.status == FollowUpStatus.UNKNOWN
    assert missing.mapping.reason == "REMOTE_TASK_ID_MISSING"


@pytest.mark.parametrize("unverified", ["IN_PROGRESS", "COMPLETED", "CANCELLED"])
def test_feishu_adapter_rejects_unverified_generic_status_aliases(unverified):
    snapshot = extract_remote_task_snapshot({"guid": "task", "status": unverified})
    assert snapshot.mapping.status == FollowUpStatus.UNKNOWN


def test_remote_observation_is_stable_and_persisted_without_notification(tmp_path):
    snapshot = extract_remote_task_snapshot({"guid": "remote-1", "status": "TODO"})
    observed_at = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    first = build_remote_task_observation(
        tenant_id="tenant-1", task_run_id="run-1", snapshot=snapshot, observed_at=observed_at,
    )
    second = build_remote_task_observation(
        tenant_id="tenant-1", task_run_id="run-1", snapshot=snapshot, observed_at=observed_at,
    )
    assert first.observation_id == second.observation_id
    with SQLiteStore(tmp_path / "observations.sqlite3") as store:
        persist_remote_task_observation(store, first)
        persist_remote_task_observation(store, second)
        rows = store.list_follow_up_observations_for_tenant("tenant-1")
        assert len(rows) == 1
        assert rows[0]["status"] == "WAITING_OWNER"
        assert store.get_notification(first.observation_id) is None


def test_daily_digest_uses_latest_state_and_one_business_day_key():
    observations = [
        {"tenant_id": "tenant-1", "task_run_id": "run-a", "status": "WAITING_OWNER", "observed_at": "2026-10-05T09:00:00Z"},
        {"tenant_id": "tenant-1", "task_run_id": "run-a", "status": "DUE_SOON", "observed_at": "2026-10-05T12:00:00Z"},
        {"tenant_id": "tenant-1", "task_run_id": "run-b", "status": "COMPLETED", "observed_at": "2026-10-05T11:00:00Z"},
        {"tenant_id": "tenant-2", "task_run_id": "run-c", "status": "OVERDUE", "observed_at": "2026-10-05T11:00:00Z"},
    ]
    digest = build_daily_follow_up_digest(
        tenant_id="tenant-1", business_date=date(2026, 10, 5),
        observations=observations, recipient_actor_id="member-1",
    )
    assert digest.counts["DUE_SOON"] == 1
    assert digest.counts["COMPLETED"] == 1
    assert digest.counts["WAITING_OWNER"] == 0
    assert digest.attention_task_run_ids == ("run-a",)
    same_day = build_daily_follow_up_digest(
        tenant_id="tenant-1", business_date="2026-10-05", observations=[], recipient_actor_id="member-1",
    )
    assert same_day.notification_key == digest.notification_key


def test_daily_digest_keeps_remote_tasks_separate_within_one_task_run():
    observations = [
        {
            "tenant_id": "tenant-1",
            "task_run_id": "run-meeting",
            "remote_task_id": "remote-a",
            "status": "WAITING_OWNER",
            "observed_at": "2026-10-06T09:00:00Z",
        },
        {
            "tenant_id": "tenant-1",
            "task_run_id": "run-meeting",
            "remote_task_id": "remote-b",
            "status": "OVERDUE",
            "observed_at": "2026-10-06T09:00:00Z",
        },
        {
            "tenant_id": "tenant-1",
            "task_run_id": "run-meeting",
            "remote_task_id": "remote-a",
            "status": "IN_PROGRESS",
            "observed_at": "2026-10-06T12:00:00Z",
        },
    ]
    digest = build_daily_follow_up_digest(
        tenant_id="tenant-1",
        business_date="2026-10-06",
        observations=observations,
        recipient_actor_id="member-1",
    )
    assert digest.counts["IN_PROGRESS"] == 1
    assert digest.counts["WAITING_OWNER"] == 0
    assert digest.counts["OVERDUE"] == 1
    assert digest.attention_task_ids == ("remote-b",)
