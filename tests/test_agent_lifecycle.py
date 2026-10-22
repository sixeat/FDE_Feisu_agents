"""Human-led, tenant-scoped governance of a generic sample agent."""

import hashlib
import json
import sqlite3

import pytest

from fde_control_plane import Actor, ActorType, AgentVersion, RiskLevel, SQLiteStore, TaskRunStatus, ToolDefinition
from fde_control_plane.runtime import (HermesAgentSampleAdapter, RunEvent, RunRequest,
                                       RuntimeExecutionError, RuntimeResult)
from test_meeting_review_web import switch_user, web_setup


def _evaluation(cp, draft_id, *, failed=False):
    draft = cp.store.get_agent_config_draft(draft_id, "tenant")
    input_hash = hashlib.sha256(json.dumps(json.loads(draft["data_json"]),
                                         sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    return {"input_hash": input_hash, "dataset_id": "sample-regression-v1", "cases": [
        {"case_id": "case-read", "expected_hash": digest("READ"),
         "observed_hash": digest("DENY" if failed else "READ"), "evidence_ref": "fixture-read"},
        {"case_id": "case-human", "expected_hash": digest("ASK_HUMAN"),
         "observed_hash": digest("ASK_HUMAN"), "evidence_ref": "fixture-human"},
    ]}


def _draft(client, headers, *, base_version=None, description="只读建议与人工核对"):
    result = client.post("/api/meetings/agent-drafts", json={
        "name": "sample-lifecycle-agent", "actor_type": "BUSINESS_AGENT",
        "description": description, "capabilities": ["doc.read"],
        "base_version": base_version,
    }, headers=headers)
    assert result.status_code == 200, result.text
    return result.json()["draft_id"]


def test_existing_agent_route_database_adds_canary_epoch_without_losing_route(tmp_path):
    path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE agent_routes (agent_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "stable_version INTEGER NOT NULL, canary_version INTEGER, "
            "volunteer_ids_json TEXT NOT NULL, status TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO agent_routes(agent_id, tenant_id, stable_version, canary_version, "
            "volunteer_ids_json, status) VALUES ('sample', 'tenant', 1, NULL, '[]', 'ACTIVE')"
        )
    with SQLiteStore(path) as store:
        route = store.get_agent_route("tenant", "sample")
        assert route["stable_version"] == 1 and route["canary_epoch"] == 0


def test_sample_agent_human_lifecycle_and_read_only_canary(tmp_path):
    class SampleRuntime:
        def run_once(self, request):
            assert request.tool_policy_snapshot == {"allowed_tools": []}
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "agent-sample.v1",
                "case_id": request.input_ref["case_id"],
                "response": f"v{request.input_ref['version']} 仅给出建议，等待人工确认。",
            }, None, events=(RunEvent(request.task_run_id, request.trace_id, 1,
                                      "completed", "2026-10-22T00:00:00Z", "sample completed"),))

    cp, _, client, member_headers, _, _, _ = web_setup(tmp_path, sample_runtime=SampleRuntime())
    cp.register_actor(Actor("reviewer", "tenant", ActorType.USER, frozenset(),
                            external_ref_hash="reviewer-subject"))
    reviewer_headers = switch_user(cp, client, "reviewer", "reviewer-subject")
    dataset = {"dataset_id": "sample-regression-v1", "cases": [
        {"case_id": "case-read", "expected_hash": hashlib.sha256(b"READ").hexdigest(),
         "evidence_ref": "fixture-read"},
        {"case_id": "case-human", "expected_hash": hashlib.sha256(b"ASK_HUMAN").hexdigest(),
         "evidence_ref": "fixture-human"},
    ]}
    assert client.post("/api/meetings/agent-evaluation-datasets", json=dataset,
                       headers=reviewer_headers).status_code == 403
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    grant = client.post("/api/meetings/member-directory/reviewer/reviewer-role", json={
        "expected_reviewer": False, "reviewer": True, "reason": "授权独立审核样例 Agent",
    }, headers=admin_headers)
    assert grant.status_code == 200 and grant.json()["reviewer"] is True
    reviewer_headers = switch_user(cp, client, "reviewer", "reviewer-subject")
    assert client.post("/api/meetings/agent-evaluation-datasets", json=dataset,
                       headers=reviewer_headers).status_code == 200
    assert client.post("/api/meetings/agent-evaluation-datasets", json=dataset,
                       headers=reviewer_headers).status_code == 409
    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    draft1 = _draft(client, member_headers)
    assert client.post(f"/api/meetings/agent-drafts/{draft1}/evaluate",
                       json=_evaluation(cp, draft1), headers=member_headers).status_code == 403
    reviewer_headers = switch_user(cp, client, "reviewer", "reviewer-subject")
    evaluation = client.post(f"/api/meetings/agent-drafts/{draft1}/evaluate",
                             json=_evaluation(cp, draft1), headers=reviewer_headers)
    assert evaluation.status_code == 200 and evaluation.json()["status"] == "PASSED"
    review1 = client.post(f"/api/meetings/agent-drafts/{draft1}/precheck",
                          headers=reviewer_headers).json()
    assert review1["status"] == "READY_FOR_ADMIN"
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    publish1 = client.post(f"/api/meetings/agent-drafts/{draft1}/confirm", json={
        "review_id": review1["review_id"], "input_hash": review1["input_hash"],
    }, headers=admin_headers)
    assert publish1.status_code == 200 and publish1.json()["version"] == 1
    snapshot1 = dict(cp.store.get_agent_snapshot("tenant", "sample-lifecycle-agent", 1))
    with pytest.raises(ValueError, match="immutable"):
        cp.store.save_agent_version(AgentVersion("sample-lifecycle-agent", 1,
                                                frozenset({"doc.write"})))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        cp.store.connection.execute(
            "UPDATE agent_version_snapshots SET config_hash = ? WHERE agent_id = ? AND version = 1",
            ("0" * 64, "sample-lifecycle-agent"),
        )
    assert cp.store.select_agent_version("tenant", "sample-lifecycle-agent", "member") == 1
    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    stable_sample = client.post(
        "/api/meetings/agents/sample-lifecycle-agent/sample-runs",
        json={"case_id": "read_only"}, headers=member_headers,
    )
    assert stable_sample.status_code == 200
    assert stable_sample.json()["agent_version"] == 1
    run1 = cp.create_task_run("member", "assistant", "sample-lifecycle-agent", 1)
    run1.status = TaskRunStatus.FAILED
    run1.failure_reason = "sample failure"
    cp.store.save_task_run(run1)

    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    draft2 = _draft(client, member_headers, base_version=1, description="只读建议与更严格的人工核对")
    reviewer_headers = switch_user(cp, client, "reviewer", "reviewer-subject")
    blocked = client.post(f"/api/meetings/agent-drafts/{draft2}/precheck",
                          headers=reviewer_headers).json()
    assert "AGENT_EVALUATION_REQUIRED" in blocked["blockers"]
    failed = client.post(f"/api/meetings/agent-drafts/{draft2}/evaluate",
                         json=_evaluation(cp, draft2, failed=True), headers=reviewer_headers)
    assert failed.json()["status"] == "FAILED"
    passed = client.post(f"/api/meetings/agent-drafts/{draft2}/evaluate",
                         json=_evaluation(cp, draft2), headers=reviewer_headers)
    assert passed.json()["status"] == "PASSED"
    review2 = client.post(f"/api/meetings/agent-drafts/{draft2}/precheck",
                          headers=reviewer_headers).json()
    assert review2["status"] == "READY_FOR_ADMIN" and not review2["duplicate"]
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    publish2 = client.post(f"/api/meetings/agent-drafts/{draft2}/confirm", json={
        "review_id": review2["review_id"], "input_hash": review2["input_hash"],
    }, headers=admin_headers)
    assert publish2.status_code == 200 and publish2.json()["version"] == 2
    assert dict(cp.store.get_agent_snapshot("tenant", "sample-lifecycle-agent", 1)) == snapshot1
    assert not cp.store.get_agent_version("sample-lifecycle-agent", 2)["active"]
    assert cp.store.select_agent_version("tenant", "sample-lifecycle-agent", "member") == 1
    route = "/api/meetings/agents/sample-lifecycle-agent"
    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    consent = {"candidate_version": 2, "enroll": True}
    assert client.post(route + "/volunteer", json=consent, headers=member_headers).status_code == 200
    assert client.post(route + "/volunteer", json=consent, headers=member_headers).status_code == 409
    assert client.post(route + "/volunteer", json={**consent, "enroll": False},
                       headers=member_headers).status_code == 200
    assert client.post(route + "/volunteer", json=consent, headers=member_headers).status_code == 200
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    payload = {"expected_version": 1, "target_version": 2,
               "volunteer_ids": ["member"], "reason": "志愿者验证只读建议"}
    cp.register_actor(Actor("outsider", "other-tenant", ActorType.USER))
    assert client.post(route + "/canary", json={**payload, "volunteer_ids": ["outsider"]},
                       headers=admin_headers).status_code == 409
    assert client.post(route + "/canary", json=payload, headers=admin_headers).status_code == 200
    assert client.post(route + "/canary", json=payload, headers=admin_headers).status_code == 409
    assert cp.store.select_agent_version("tenant", "sample-lifecycle-agent", "member") == 2
    assert cp.store.select_agent_version("tenant", "sample-lifecycle-agent", "admin") == 1
    readiness_url = route + "/promotion-readiness"
    initial_readiness = client.get(readiness_url).json()
    assert not initial_readiness["can_promote"]
    assert {"OBSERVATION_PERIOD", "SAMPLE_COUNT"} <= set(initial_readiness["blockers"])
    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    assert client.post(route + "/volunteer", json={**consent, "enroll": False},
                       headers=member_headers).status_code == 200
    assert cp.store.select_agent_version("tenant", "sample-lifecycle-agent", "member") == 1
    assert client.post(route + "/volunteer", json=consent, headers=member_headers).status_code == 200
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    assert client.post(route + "/canary", json=payload, headers=admin_headers).status_code == 200
    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    sample_url = route + "/sample-runs"
    assert client.post(sample_url, json={"case_id": "read_only"}).status_code == 403
    sample2 = client.post(sample_url, json={"case_id": "read_only"}, headers=member_headers)
    assert sample2.status_code == 200 and sample2.json()["status"] == "WAITING_REVIEW"
    run2 = cp._task(sample2.json()["task_run_id"])
    sample_result = cp.store.get_agent_sample_result(run2.task_run_id)
    assert sample_result["response_hash"] == hashlib.sha256(sample2.json()["response"].encode()).hexdigest()
    with pytest.raises(sqlite3.IntegrityError, match="immutable agent sample result"):
        cp.store.connection.execute(
            "UPDATE agent_sample_results SET response_text = ? WHERE task_run_id = ?",
            ("changed", run2.task_run_id),
        )
    decision = cp.authorize_operation(run2.task_run_id, ToolDefinition(
        "read", frozenset({"doc.read"}), RiskLevel.LOW))
    assert not decision.allowed and "suggestion-only" in decision.reason
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    assert client.get(readiness_url).json()["sample_count"] == 1
    assert client.post(route + "/promote", json={**payload, "volunteer_ids": []},
                       headers=admin_headers).status_code == 409
    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    sample3 = client.post(sample_url, json={"case_id": "write_boundary"}, headers=member_headers)
    assert sample3.status_code == 200 and sample3.json()["status"] == "WAITING_REVIEW"
    run3 = cp._task(sample3.json()["task_run_id"])
    cp.store.connection.execute(
        "UPDATE agent_routes SET updated_at = datetime('now', '-2 minutes') WHERE agent_id = ?",
        ("sample-lifecycle-agent",),
    )
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    pending_readiness = client.get(readiness_url).json()
    assert pending_readiness["blockers"] == ["REVIEW_MISSING"]
    assert pending_readiness["sample_count"] == 2 and pending_readiness["unreviewed_count"] == 2
    assert client.post(route + "/promote", json={**payload, "volunteer_ids": []},
                       headers=admin_headers).status_code == 409
    fabricated = cp.create_task_run("member", "assistant", "sample-lifecycle-agent", 2)
    fabricated.status = TaskRunStatus.WAITING_REVIEW
    cp.store.save_task_run(fabricated)
    assert "RUN_EVIDENCE_MISSING" in client.get(readiness_url).json()["blockers"]
    reviewer_headers = switch_user(cp, client, "reviewer", "reviewer-subject")
    assert client.get(readiness_url).status_code == 403
    assert client.get(route + "/promotion-history").status_code == 403
    assert client.get(route + "/compare?old_version=1&new_version=2").status_code == 403
    candidate_runs = client.get(route + "/canary-runs").json()["runs"]
    assert {item["task_run_id"] for item in candidate_runs} == {
        run2.task_run_id, run3.task_run_id, fabricated.task_run_id,
    }
    assert next(item for item in candidate_runs if item["task_run_id"] == run2.task_run_id)[
        "sample_response"
    ] == sample2.json()["response"]
    failed_review = {"expected_version": 2, "outcome": "FAIL", "evidence_ref": "fixture:badcase"}
    assert client.post(route + f"/canary-runs/{fabricated.task_run_id}/review",
                       json=failed_review, headers=reviewer_headers).status_code == 409
    assert client.post(route + f"/canary-runs/{run2.task_run_id}/review", json=failed_review,
                       headers=reviewer_headers).status_code == 200
    assert client.post(route + f"/canary-runs/{run2.task_run_id}/review", json=failed_review,
                       headers=reviewer_headers).status_code == 409
    passed_review = {"expected_version": 2, "outcome": "PASS", "evidence_ref": "fixture:reviewed"}
    assert client.post(route + f"/canary-runs/{run3.task_run_id}/review", json=passed_review,
                       headers=reviewer_headers).status_code == 200
    with pytest.raises(sqlite3.IntegrityError, match="immutable canary review"):
        cp.store.connection.execute(
            "UPDATE agent_canary_reviews SET outcome = 'PASS' WHERE task_run_id = ?",
            (run2.task_run_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable canary review"):
        cp.store.connection.execute(
            "DELETE FROM agent_canary_reviews WHERE task_run_id = ?", (run2.task_run_id,),
        )
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    reviewed_runs = client.get(route + "/canary-runs").json()["runs"]
    assert {run["reviewer_id"] for run in reviewed_runs if run["reviewer_id"]} == {"reviewer"}
    assert {run["evidence_ref"] for run in reviewed_runs if run["evidence_ref"]} == {
        "fixture:badcase", "fixture:reviewed",
    }
    failed_readiness = client.get(readiness_url).json()
    assert {"RUN_EVIDENCE_MISSING", "REVIEW_MISSING", "REVIEW_FAILED"} <= set(
        failed_readiness["blockers"]
    )
    assert failed_readiness["failed_review_count"] == 1
    run2.status = TaskRunStatus.FAILED
    cp.store.save_task_run(run2)
    assert {"RUN_NOT_WAITING_REVIEW", "RUN_FAILURE", "REVIEW_FAILED"} <= set(
        client.get(readiness_url).json()["blockers"]
    )
    assert client.post(route + "/promote", json={**payload, "volunteer_ids": []},
                       headers=admin_headers).status_code == 409
    first_epoch = run2.canary_epoch
    assert client.post(route + "/rollback", json={"expected_version": 1, "target_version": 1,
        "reason": "灰度失败后回到稳定版"}, headers=admin_headers).status_code == 200
    assert client.get(readiness_url).status_code == 409
    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    assert client.post(route + "/volunteer", json=consent, headers=member_headers).status_code == 200
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    assert client.post(route + "/canary", json=payload, headers=admin_headers).status_code == 200
    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    fresh_runs = []
    for case_id in ("read_only", "write_boundary"):
        response = client.post(sample_url, json={"case_id": case_id}, headers=member_headers)
        assert response.status_code == 200 and response.json()["status"] == "WAITING_REVIEW"
        fresh_runs.append(cp._task(response.json()["task_run_id"]))
    assert all(run.canary_epoch != first_epoch for run in fresh_runs)
    cp.store.connection.execute(
        "UPDATE agent_routes SET updated_at = datetime('now', '-2 minutes') WHERE agent_id = ?",
        ("sample-lifecycle-agent",),
    )
    reviewer_headers = switch_user(cp, client, "reviewer", "reviewer-subject")
    assert {item["task_run_id"] for item in client.get(route + "/canary-runs").json()["runs"]} == {
        run.task_run_id for run in fresh_runs
    }
    for run in fresh_runs:
        assert client.post(route + f"/canary-runs/{run.task_run_id}/review", json=passed_review,
                           headers=reviewer_headers).status_code == 200
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    assert client.get(route + "/compare?old_version=1&new_version=99").status_code == 404
    diagnostics = client.get(route + "/diagnostics").json()
    assert diagnostics["total"] == 7 and diagnostics["versions"]["1"]["failures"] == 1
    comparison = client.get(route + "/compare?old_version=1&new_version=2").json()
    assert comparison["changes"]["description"]["old"] != comparison["changes"]["description"]["new"]
    read_answers = comparison["sample_comparison"]["read_only"]
    assert read_answers["old"]["task_run_id"] == stable_sample.json()["task_run_id"]
    assert read_answers["old"]["response"] == stable_sample.json()["response"]
    assert read_answers["new"]["task_run_id"] == fresh_runs[0].task_run_id
    assert read_answers["new"]["response"] != read_answers["old"]["response"]
    assert comparison["sample_comparison"]["write_boundary"]["old"] is None
    assert comparison["sample_comparison"]["write_boundary"]["new"]["task_run_id"] == fresh_runs[1].task_run_id
    ready = client.get(readiness_url).json()
    assert ready["can_promote"] and ready["blockers"] == []
    assert ready["sample_count"] == 2 and ready["unreviewed_count"] == 0
    assert "SELF_REVIEW_PROMOTION" in cp.store.agent_promotion_readiness(
        "tenant", "sample-lifecycle-agent", "reviewer",
    )["blockers"]
    assert client.post(route + "/promote", json={**payload, "volunteer_ids": []},
                       headers=admin_headers).status_code == 200
    promotion_history = client.get(route + "/promotion-history").json()["promotions"]
    assert len(promotion_history) == 1
    promoted = promotion_history[0]
    assert promoted["stable_version"] == 1 and promoted["candidate_version"] == 2
    assert promoted["integrity_ok"] is True
    assert promoted["canary_epoch"] == fresh_runs[0].canary_epoch
    assert {run["task_run_id"] for run in promoted["runs"]} == {run.task_run_id for run in fresh_runs}
    assert {run["outcome"] for run in promoted["runs"]} == {"PASS"}
    evidence_snapshot = {key: promoted[key] for key in (
        "audit_id", "stable_version", "candidate_version", "canary_epoch",
        "approver_id", "observation_seconds", "runs",
    )}
    evidence_snapshot["agent_id"] = "sample-lifecycle-agent"
    assert promoted["evidence_hash"] == hashlib.sha256(json.dumps(
        evidence_snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert promoted["audit_id"] in {event["audit_id"] for event in cp.store.list_audits()}
    with pytest.raises(sqlite3.IntegrityError, match="immutable promotion evidence"):
        cp.store.connection.execute(
            "UPDATE agent_promotion_evidence SET evidence_hash = ? WHERE audit_id = ?",
            ("0" * 64, promoted["audit_id"]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable promotion evidence"):
        cp.store.connection.execute(
            "DELETE FROM agent_promotion_evidence WHERE audit_id = ?", (promoted["audit_id"],),
        )
    fresh_runs[0].status = TaskRunStatus.FAILED
    cp.store.save_task_run(fresh_runs[0])
    assert client.get(route + "/promotion-history").json()["promotions"][0]["runs"] == promoted["runs"]
    assert cp.store.select_agent_version("tenant", "sample-lifecycle-agent", "admin") == 2
    assert not cp.authorize_operation(run2.task_run_id, ToolDefinition(
        "read", frozenset({"doc.read"}), RiskLevel.LOW)).allowed
    assert client.post(route + "/rollback", json={"expected_version": 2, "target_version": 1,
        "reason": "人工核验后回退旧版"}, headers=admin_headers).status_code == 200
    assert cp.store.select_agent_version("tenant", "sample-lifecycle-agent", "member") == 1
    assert client.post(route + "/archive", json={"expected_version": 1,
        "reason": "样例完成下架归档"}, headers=admin_headers).status_code == 200
    assert cp.store.select_agent_version("tenant", "sample-lifecycle-agent", "member") is None
    assert next(agent for agent in client.get("/api/meetings/agents").json()["agents"]
                if agent["agent_id"] == "sample-lifecycle-agent")["status"] == "ARCHIVED"
    assert dict(cp.store.get_agent_snapshot("tenant", "sample-lifecycle-agent", 1)) == snapshot1
    assert len(client.get(route + "/promotion-history").json()["promotions"]) == 1
    assert client.get(route + "/compare?old_version=1&new_version=2").json()[
        "sample_comparison"
    ] == comparison["sample_comparison"]
    events = [event["event_type"] for event in cp.store.list_audits()]
    assert all(name in events for name in ("AGENT_EVALUATED", "AGENT_CANARY", "AGENT_CANARY_REVIEWED", "AGENT_PROMOTE",
                                      "AGENT_ROLLBACK", "AGENT_ARCHIVE"))


def test_lifecycle_rejects_non_admin_and_cross_tenant_volunteer(tmp_path):
    cp, _, client, headers, _, _, _ = web_setup(tmp_path)
    route = "/api/meetings/agents/agent"
    assert client.get(route + "/diagnostics").status_code == 403
    assert client.post(route + "/archive", json={"expected_version": 1,
        "reason": "普通成员尝试下架"}, headers=headers).status_code == 403
    assert client.post(route + "/sample-runs", json={"case_id": "read_only"},
                       headers=headers).status_code == 404


@pytest.mark.parametrize("failure,expected", [
    ("invalid", "FAILED"), ("unknown", "RECONCILING"), ("revoked", "FAILED"),
    ("rebound", "FAILED"),
])
def test_sample_run_fails_closed_before_human_review(tmp_path, failure, expected):
    holder = {}

    class SampleRuntime:
        def run_once(self, request):
            if failure == "revoked":
                holder["cp"].store.connection.execute(
                    "UPDATE actors SET active = 0 WHERE actor_id = 'member'"
                )
                holder["cp"].store.connection.commit()
            if failure == "rebound":
                holder["cp"].store.connection.execute(
                    "UPDATE actors SET external_ref_hash = 'another-subject' WHERE actor_id = 'member'"
                )
                holder["cp"].store.connection.commit()
            if failure == "unknown":
                return RuntimeResult("UNKNOWN", None, None, error_code="RUNTIME_TIMEOUT")
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "agent-sample.v1", "case_id": request.input_ref["case_id"],
                "response": "" if failure == "invalid" else "需要人工确认。",
            }, None, events=(RunEvent(request.task_run_id, request.trace_id, 1,
                                      "completed", "2026-10-22T00:00:00Z", "completed"),))

    cp, _, client, member_headers, _, _, _ = web_setup(tmp_path, sample_runtime=SampleRuntime())
    holder["cp"] = cp
    cp.register_actor(Actor("reviewer", "tenant", ActorType.USER, frozenset(),
                            external_ref_hash="reviewer-subject"))
    draft = _draft(client, member_headers)
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    assert client.post("/api/meetings/member-directory/reviewer/reviewer-role", json={
        "expected_reviewer": False, "reviewer": True, "reason": "independent sample review",
    }, headers=admin_headers).status_code == 200
    reviewer_headers = switch_user(cp, client, "reviewer", "reviewer-subject")
    dataset = {"dataset_id": "sample-regression-v1", "cases": [
        {"case_id": case, "expected_hash": hashlib.sha256(value.encode()).hexdigest(),
         "evidence_ref": evidence} for case, value, evidence in (
             ("case-read", "READ", "fixture-read"),
             ("case-human", "ASK_HUMAN", "fixture-human"),
         )
    ]}
    assert client.post("/api/meetings/agent-evaluation-datasets", json=dataset,
                       headers=reviewer_headers).status_code == 200
    evaluation = _evaluation(cp, draft)
    assert client.post(f"/api/meetings/agent-drafts/{draft}/evaluate", json=evaluation,
                       headers=reviewer_headers).status_code == 200
    review = client.post(f"/api/meetings/agent-drafts/{draft}/precheck",
                         headers=reviewer_headers).json()
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    assert client.post(f"/api/meetings/agent-drafts/{draft}/confirm", json={
        "review_id": review["review_id"], "input_hash": review["input_hash"],
    }, headers=admin_headers).status_code == 200
    member_headers = switch_user(cp, client, "member", hashlib.sha256(b"ou_submitter").hexdigest())
    result = client.post("/api/meetings/agents/sample-lifecycle-agent/sample-runs",
                         json={"case_id": "read_only"}, headers=member_headers)
    assert result.status_code == 200 and result.json()["status"] == expected
    assert cp.store.get_agent_sample_result(result.json()["task_run_id"]) is None
    assert not any(event["event_type"] == "AGENT_SAMPLE_COMPLETED"
                   for event in cp.store.list_audits())


def test_hermes_agent_sample_uses_no_tools_and_requires_fixed_input():
    import subprocess

    captured = {}

    def runner(command, **kwargs):
        captured.update(command=command, prompt=kwargs["input"])
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "schema_version": "agent-sample.v1", "case_id": "read_only",
            "response": "请人工核对。",
        }), "")

    adapter = HermesAgentSampleAdapter(runner=runner)
    request = RunRequest("run", "trace", "member", "sample@1", {
        "agent_id": "sample", "version": 1, "description": "只读建议",
        "case_id": "read_only", "question": "给出建议",
    }, {"allowed_tools": []}, "offline")
    result = adapter.run_once(request)
    assert result.status == "SUCCEEDED"
    assert result.events[-1].summary == "Hermes returned a agent-sample.v1 contract"
    assert captured["command"][captured["command"].index("--toolsets") + 1] == "context_engine"
    assert "Do not use tools" in captured["prompt"]
    with pytest.raises(RuntimeExecutionError, match="INVALID_AGENT_SAMPLE"):
        adapter.run_once(RunRequest("run", "trace", "member", "sample@1",
                                    request.input_ref, {"allowed_tools": ["task.create"]}, "offline"))
