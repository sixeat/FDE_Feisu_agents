"""The local browser demo uses real governance APIs and synthetic runtime only."""

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from scripts.local_lifecycle_demo import create_app
from test_agent_lifecycle import _draft


def _role(client, role):
    response = client.post(f"/local-demo/login/{role}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/local-demo/h5#agents"
    csrf = client.get("/api/meetings/agent-drafts").json()["csrf_token"]
    return {"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1:8212"}


def test_browser_demo_runs_governance_without_feishu(tmp_path):
    app = create_app(tmp_path / "demo.sqlite3")
    client = TestClient(app)
    assert client.get("/health").json()["mode"] == "synthetic-local-demo"
    assert client.get("/h5", follow_redirects=False).headers["location"] == "/local-demo"
    assert "/local-demo/h5#workflow-discovery" in client.get("/local-demo").text
    member_headers = _role(client, "member")
    assert client.get("/h5", follow_redirects=False).headers["location"] == "/local-demo/h5#agents"
    workspace = client.get("/local-demo/h5")
    assert workspace.status_code == 200
    assert '/assets/local_demo_ui.js' in workspace.text
    assert '/assets/local_demo.css' in workspace.text
    assert '/assets/workflow_discovery.js' in workspace.text
    assert workspace.text.index('/assets/local_demo_ui.js') < workspace.text.index('/assets/workflow_discovery.js')
    assert 'id="workflow-demo-banner"' in workspace.text
    assert client.post("/api/meetings/start", json={}, headers=member_headers).status_code == 404
    draft = _draft(client, member_headers)
    reviewer_headers = _role(client, "reviewer")
    dataset = {"dataset_id": "sample-regression-v1", "cases": [
        {"case_id": case, "expected_hash": hashlib.sha256(value.encode()).hexdigest(),
         "evidence_ref": evidence} for case, value, evidence in (
             ("case-read", "READ", "fixture-read"),
             ("case-human", "ASK_HUMAN", "fixture-human"),
         )
    ]}
    assert client.post("/api/meetings/agent-evaluation-datasets", json=dataset,
                       headers=reviewer_headers).status_code == 200
    stored = app.state.control_plane.store.get_agent_config_draft(draft, "local-demo")
    input_hash = hashlib.sha256(json.dumps(json.loads(stored["data_json"]),
                                         sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    evaluation = {"input_hash": input_hash, "dataset_id": dataset["dataset_id"],
                  "cases": [{**case, "observed_hash": case["expected_hash"]}
                            for case in dataset["cases"]]}
    assert client.post(f"/api/meetings/agent-drafts/{draft}/evaluate", json=evaluation,
                       headers=reviewer_headers).status_code == 200
    review = client.post(f"/api/meetings/agent-drafts/{draft}/precheck",
                         headers=reviewer_headers).json()
    assert review["status"] == "READY_FOR_ADMIN"
    admin_headers = _role(client, "admin")
    assert client.post(f"/api/meetings/agent-drafts/{draft}/confirm", json={
        "review_id": review["review_id"], "input_hash": review["input_hash"],
    }, headers=admin_headers).status_code == 200
    member_headers = _role(client, "member")
    run = client.post("/api/meetings/agents/sample-lifecycle-agent/sample-runs",
                      json={"case_id": "read_only"}, headers=member_headers)
    assert run.status_code == 200 and run.json()["status"] == "WAITING_REVIEW"
    assert run.json()["response"].startswith("v1")
    assert run.json()["allowed_tools"] == []
    assert app.state.control_plane.store.get_agent_sample_result(run.json()["task_run_id"]) is not None
    failure_url = "/local-demo/agents/sample-lifecycle-agent/sample-runs"
    assert client.post(failure_url, json={"case_id": "read_only", "outcome": "FAILED"}).status_code == 403
    failures = []
    for outcome, status in (("FAILED", "FAILED"), ("UNKNOWN", "RECONCILING")):
        result = client.post(failure_url, json={"case_id": "read_only", "outcome": outcome},
                             headers=member_headers)
        assert result.status_code == 200 and result.json()["status"] == status
        task_run_id = result.json()["task_run_id"]
        assert app.state.control_plane.store.get_agent_sample_result(task_run_id) is None
        failures.append(task_run_id)
    admin_headers = _role(client, "admin")
    issues = client.get("/api/meetings/agents/sample-lifecycle-agent/diagnostics").json()["issues"]
    assert {issue["task_run_id"] for issue in issues} == set(failures)
    assert {issue["error_code"] for issue in issues} == {"SAMPLE_DEMO_FAILURE", "SAMPLE_DEMO_UNKNOWN"}
    assert all(issue["is_sample"] for issue in issues)
    member_headers = _role(client, "member")
    repeat = client.post("/api/meetings/agents/sample-lifecycle-agent/sample-runs",
                         json={"case_id": "read_only"}, headers=member_headers)
    assert repeat.status_code == 409 and failures[1] in repeat.json()["detail"]
    disposition_url = ("/api/meetings/agents/sample-lifecycle-agent/sample-runs/"
                       + failures[1] + "/disposition")
    decision = {"expected_version": 1, "evidence_ref": "local:checked-v1"}
    assert client.post(disposition_url, json=decision, headers=member_headers).status_code == 403
    admin_headers = _role(client, "admin")
    assert client.post(disposition_url, json={**decision, "expected_version": 2},
                       headers=admin_headers).status_code == 409
    assert client.post(disposition_url.replace("sample-lifecycle-agent", "local-assistant"),
                       json=decision, headers=admin_headers).status_code == 409
    assert client.post(disposition_url, json={**decision, "evidence_ref": "private text here"},
                       headers=admin_headers).status_code == 422
    saved = client.post(disposition_url, json=decision, headers=admin_headers)
    assert saved.status_code == 200 and saved.json()["decision"] == "DISCARD_UNVERIFIED"
    assert client.post(disposition_url, json=decision, headers=admin_headers).status_code == 409
    assert app.state.control_plane.store.get_task_run(failures[1])["status"] == "RECONCILING"
    assert app.state.control_plane.store.get_agent_sample_result(failures[1]) is None
    resolved_issues = client.get("/api/meetings/agents/sample-lifecycle-agent/diagnostics").json()["issues"]
    disposition = next(issue for issue in resolved_issues if issue["task_run_id"] == failures[1])["disposition"]
    assert disposition["evidence_ref"] == decision["evidence_ref"]
    assert disposition["audit_id"] in {event["audit_id"] for event in app.state.control_plane.store.list_audits()}
    with sqlite3.connect(tmp_path / "demo.sqlite3") as connection:
        try:
            connection.execute("DELETE FROM agent_sample_dispositions WHERE task_run_id = ?", (failures[1],))
        except sqlite3.IntegrityError as exc:
            assert "immutable sample disposition" in str(exc)
        else:
            raise AssertionError("sample disposition must be immutable")
    member_headers = _role(client, "member")
    retried = client.post("/api/meetings/agents/sample-lifecycle-agent/sample-runs",
                          json={"case_id": "read_only"}, headers=member_headers)
    assert retried.status_code == 200 and retried.json()["status"] == "WAITING_REVIEW"
    draft2 = _draft(client, member_headers, base_version=1, description="只读建议与更严格的人工核对")
    reviewer_headers = _role(client, "reviewer")
    stored2 = app.state.control_plane.store.get_agent_config_draft(draft2, "local-demo")
    evaluation["input_hash"] = hashlib.sha256(json.dumps(json.loads(stored2["data_json"]),
                                                       sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    assert client.post(f"/api/meetings/agent-drafts/{draft2}/evaluate", json=evaluation,
                       headers=reviewer_headers).status_code == 200
    review2 = client.post(f"/api/meetings/agent-drafts/{draft2}/precheck",
                          headers=reviewer_headers).json()
    admin_headers = _role(client, "admin")
    assert client.post(f"/api/meetings/agent-drafts/{draft2}/confirm", json={
        "review_id": review2["review_id"], "input_hash": review2["input_hash"],
    }, headers=admin_headers).status_code == 200
    route = "/api/meetings/agents/sample-lifecycle-agent"
    member_headers = _role(client, "member")
    assert client.post(route + "/volunteer", json={"candidate_version": 2, "enroll": True},
                       headers=member_headers).status_code == 200
    admin_headers = _role(client, "admin")
    route_request = {"expected_version": 1, "target_version": 2,
                     "volunteer_ids": ["member"], "reason": "local sample canary"}
    assert client.post(route + "/canary", json=route_request,
                       headers=admin_headers).status_code == 200
    member_headers = _role(client, "member")
    unknown = client.post("/local-demo/agents/sample-lifecycle-agent/sample-runs",
                          json={"case_id": "read_only", "outcome": "UNKNOWN"},
                          headers=member_headers)
    assert unknown.status_code == 200 and unknown.json()["status"] == "RECONCILING"
    unknown_id = unknown.json()["task_run_id"]
    reviewer_headers = _role(client, "reviewer")
    canary_runs = client.get(route + "/canary-runs").json()["runs"]
    assert next(run for run in canary_runs if run["task_run_id"] == unknown_id)["error_code"] == "SAMPLE_DEMO_UNKNOWN"
    assert client.post(route + f"/canary-runs/{unknown_id}/review", json={
        "expected_version": 2, "outcome": "PASS", "evidence_ref": "local:unknown",
    }, headers=reviewer_headers).status_code == 409
    admin_headers = _role(client, "admin")
    readiness = client.get(route + "/promotion-readiness").json()
    assert {"RUN_FAILURE", "RUN_NOT_WAITING_REVIEW"} <= set(readiness["blockers"])
    assert client.post(route + "/promote", json={**route_request, "volunteer_ids": []},
                       headers=admin_headers).status_code == 409
    disposition_url = route + f"/sample-runs/{unknown_id}/disposition"
    assert client.post(disposition_url, json={"expected_version": 2,
        "evidence_ref": "local:checked-canary"}, headers=admin_headers).status_code == 200
    assert "RUN_FAILURE" in client.get(route + "/promotion-readiness").json()["blockers"]
    assert client.post(route + "/rollback", json={
        "expected_version": 1, "target_version": 1, "reason": "核对未知样例后关闭灰度窗口",
    }, headers=admin_headers).status_code == 200
    member_headers = _role(client, "member")
    assert client.post(route + "/volunteer", json={"candidate_version": 2, "enroll": True},
                       headers=member_headers).status_code == 200
    admin_headers = _role(client, "admin")
    assert client.post(route + "/canary", json=route_request,
                       headers=admin_headers).status_code == 200
    assert unknown_id not in {run["task_run_id"] for run in client.get(route + "/canary-runs").json()["runs"]}
    member_headers = _role(client, "member")
    sample_runs = []
    for case_id in ("read_only", "write_boundary"):
        response = client.post(route + "/sample-runs", json={"case_id": case_id},
                               headers=member_headers)
        assert response.status_code == 200 and response.json()["agent_version"] == 2
        sample_runs.append(response.json()["task_run_id"])
    reviewer_headers = _role(client, "reviewer")
    for task_run_id in sample_runs:
        assert client.post(route + f"/canary-runs/{task_run_id}/review", json={
            "expected_version": 2, "outcome": "PASS", "evidence_ref": "local:reviewed",
        }, headers=reviewer_headers).status_code == 200
    app.state.control_plane.store.connection.execute(
        "UPDATE agent_routes SET updated_at = datetime('now', '-2 minutes') WHERE agent_id = ?",
        ("sample-lifecycle-agent",),
    )
    admin_headers = _role(client, "admin")
    assert client.get(route + "/promotion-readiness").json()["can_promote"] is True
    assert client.post(route + "/promote", json={**route_request, "volunteer_ids": []},
                       headers=admin_headers).status_code == 200
    assert client.get(route + "/compare?old_version=1&new_version=2").json()[
        "sample_comparison"
    ]["read_only"]["old"]["task_run_id"] == retried.json()["task_run_id"]
    assert client.post(route + "/rollback", json={
        "expected_version": 2, "target_version": 1, "reason": "local sample rollback",
    }, headers=admin_headers).status_code == 200
    assert client.post(route + "/archive", json={
        "expected_version": 1, "reason": "local sample archived",
    }, headers=admin_headers).status_code == 200
    assert app.state.control_plane.store.select_agent_version(
        "local-demo", "sample-lifecycle-agent", "member"
    ) is None
    app.state.control_plane.store.close()
    restarted = create_app(tmp_path / "demo.sqlite3")
    stored_dispositions = restarted.state.control_plane.store.agent_sample_dispositions(
        "local-demo", "sample-lifecycle-agent",
    )
    assert set(stored_dispositions) == {failures[1], unknown_id}
    assert restarted.state.control_plane.store.get_task_run(unknown_id)["status"] == "RECONCILING"


def test_browser_demo_restricts_login_origin_and_remote_clients(tmp_path):
    app = create_app(tmp_path / "demo.sqlite3")
    client = TestClient(app)
    assert client.post("/local-demo/login/admin", headers={
        "Origin": "https://another.example",
    }).status_code == 403
    assert client.post("/local-demo/login/unknown").status_code == 404
    remote = TestClient(app, client=("203.0.113.9", 40000))
    assert remote.get("/local-demo").status_code == 403


def test_local_member_can_subscribe_scan_review_create_draft_and_revoke(tmp_path):
    app = create_app(tmp_path / "workflow-demo.sqlite3")
    client = TestClient(app)
    headers = _role(client, "member")
    subscribed = client.post("/api/workflow-opportunities/subscriptions", headers=headers,
                             json={"chat_id": "oc_workflow123", "chat_type": "GROUP",
                                   "display_label": "本地课程群", "consent_confirmed": True,
                                   "consent_version": "workflow-source-v1"})
    assert subscribed.status_code == 200
    subscription_id = subscribed.json()["subscription_id"]

    scanned = client.post(f"/api/workflow-opportunities/subscriptions/{subscription_id}/scan",
                          headers=headers)
    assert scanned.status_code == 200
    assert scanned.json()["messages_scanned"] == 2
    opportunity_id = scanned.json()["opportunity_ids"][0]
    opportunity_url = f"/api/workflow-opportunities/{opportunity_id}"
    assert client.get("/api/workflow-opportunities").json()["opportunities"][0]["status"] == "DISCOVERED"

    decision = client.post(opportunity_url + "/decision", headers=headers,
                           json={"decision": "ACCEPTED", "reason": "每周都会重复整理课程资料"})
    assert decision.status_code == 200
    draft = client.post(opportunity_url + "/agent-draft", headers=headers,
                        json={"name": "课程资料 Agent", "description": "整理重复流程并输出待确认建议"})
    assert draft.status_code == 200
    assert draft.json()["status"] == "PENDING_REVIEW"
    assert draft.json()["remote_write"] is False

    revoked = client.delete(f"/api/workflow-opportunities/subscriptions/{subscription_id}",
                            headers=headers)
    assert revoked.status_code == 200
    assert client.post(f"/api/workflow-opportunities/subscriptions/{subscription_id}/scan",
                       headers=headers).status_code == 403
    opportunity = client.get("/api/workflow-opportunities").json()["opportunities"][0]
    assert opportunity["status"] == "AGENT_DRAFT_CREATED"
    app.state.control_plane.store.close()


def test_revoked_workflow_source_blocks_precheck_and_publication(tmp_path):
    app = create_app(tmp_path / "revoked-workflow-demo.sqlite3")
    client = TestClient(app)
    member_headers = _role(client, "member")
    subscribed = client.post("/api/workflow-opportunities/subscriptions", headers=member_headers,
                             json={"chat_id": "oc_revoked123", "chat_type": "GROUP",
                                   "display_label": "撤权测试群", "consent_confirmed": True,
                                   "consent_version": "workflow-source-v1"})
    assert subscribed.status_code == 200
    subscription_id = subscribed.json()["subscription_id"]
    scanned = client.post(f"/api/workflow-opportunities/subscriptions/{subscription_id}/scan",
                          headers=member_headers)
    opportunity_id = scanned.json()["opportunity_ids"][0]
    assert client.post(f"/api/workflow-opportunities/{opportunity_id}/decision",
                       headers=member_headers,
                       json={"decision": "ACCEPTED", "reason": "每周都要重复整理资料"}).status_code == 200
    created = client.post(f"/api/workflow-opportunities/{opportunity_id}/agent-draft",
                          headers=member_headers,
                          json={"name": "撤权来源 Agent", "description": "只做人工待确认建议"})
    assert created.status_code == 200
    draft_id = created.json()["draft_id"]
    stored = app.state.control_plane.store.get_agent_config_draft(draft_id, "local-demo")
    input_hash = hashlib.sha256(json.dumps(json.loads(stored["data_json"]),
                                         sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    reviewer_headers = _role(client, "reviewer")
    digest = hashlib.sha256(b"READ_ONLY").hexdigest()
    dataset = {"dataset_id": "revoked-source-eval", "cases": [
        {"case_id": name, "expected_hash": digest, "evidence_ref": name}
        for name in ("read-only-1", "read-only-2")
    ]}
    assert client.post("/api/meetings/agent-evaluation-datasets", json=dataset,
                       headers=reviewer_headers).status_code == 200
    assert client.post(f"/api/meetings/agent-drafts/{draft_id}/evaluate", json={
        "input_hash": input_hash, "dataset_id": dataset["dataset_id"],
        "cases": [case | {"observed_hash": digest} for case in dataset["cases"]],
    }, headers=reviewer_headers).status_code == 200
    ready = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck",
                        headers=reviewer_headers).json()
    assert ready["status"] == "READY_FOR_ADMIN"

    app.state.control_plane.store.connection.execute(
        "UPDATE workflow_source_subscriptions SET consent_version = 'workflow-source-v0' "
        "WHERE subscription_id = ?", (subscription_id,),
    )
    admin_headers = _role(client, "admin")
    outdated = client.post(f"/api/meetings/agent-drafts/{draft_id}/confirm", json={
        "review_id": ready["review_id"], "input_hash": ready["input_hash"],
    }, headers=admin_headers)
    assert outdated.status_code == 409
    reviewer_headers = _role(client, "reviewer")
    outdated_review = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck",
                                  headers=reviewer_headers).json()
    assert "WORKFLOW_SOURCE_UNAVAILABLE" in outdated_review["blockers"]
    member_headers = _role(client, "member")
    renewed = client.post(f"/api/workflow-opportunities/subscriptions/{subscription_id}/consent",
                          headers=member_headers,
                          json={"consent_confirmed": True, "consent_version": "workflow-source-v1"})
    assert renewed.status_code == 200
    assert next(item for item in client.get("/api/meetings/agent-drafts").json()["drafts"]
                if item["draft_id"] == draft_id)["workflow_source_available"] is True
    reviewer_headers = _role(client, "reviewer")
    ready = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck",
                        headers=reviewer_headers).json()
    assert ready["status"] == "READY_FOR_ADMIN" and ready["duplicate"] is False

    member_headers = _role(client, "member")
    assert client.delete(f"/api/workflow-opportunities/subscriptions/{subscription_id}",
                         headers=member_headers).status_code == 200
    assert not any(event["event_type"] == "WORKFLOW_AGENT_SOURCE_REVOKED"
                   for event in app.state.control_plane.store.list_tenant_audits("local-demo"))
    admin_headers = _role(client, "admin")
    listed = next(item for item in client.get("/api/meetings/agent-drafts").json()["drafts"]
                  if item["draft_id"] == draft_id)
    assert listed["workflow_source_available"] is False
    assert listed["review_status"] == "READY_FOR_ADMIN"
    denied = client.post(f"/api/meetings/agent-drafts/{draft_id}/confirm", json={
        "review_id": ready["review_id"], "input_hash": ready["input_hash"],
    }, headers=admin_headers)
    assert denied.status_code == 409
    assert "来源已撤销或失效" in denied.json()["detail"]
    reviewer_headers = _role(client, "reviewer")
    blocked = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck",
                          headers=reviewer_headers).json()
    assert blocked["status"] == "BLOCKED"
    assert "WORKFLOW_SOURCE_UNAVAILABLE" in blocked["blockers"]
    assert blocked["duplicate"] is False
    repeated = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck",
                           headers=reviewer_headers).json()
    assert repeated["review_id"] == blocked["review_id"] and repeated["duplicate"] is True
    assert app.state.control_plane.store.get_agent_config_approval(draft_id, "local-demo", input_hash) is None
    assert app.state.control_plane.store.get_agent_version("撤权来源 Agent", 1) is None


def test_revoking_published_workflow_source_creates_reviewer_issue_without_pausing(tmp_path):
    app = create_app(tmp_path / "published-source-revoke.sqlite3")
    client = TestClient(app)
    member_headers = _role(client, "member")
    subscribed = client.post("/api/workflow-opportunities/subscriptions", headers=member_headers,
                             json={"chat_id": "oc_published123", "chat_type": "GROUP",
                                   "display_label": "已发布来源群", "consent_confirmed": True,
                                   "consent_version": "workflow-source-v1"})
    assert subscribed.status_code == 200
    subscription_id = subscribed.json()["subscription_id"]
    scanned = client.post(f"/api/workflow-opportunities/subscriptions/{subscription_id}/scan",
                          headers=member_headers)
    opportunity_id = scanned.json()["opportunity_ids"][0]
    assert client.post(f"/api/workflow-opportunities/{opportunity_id}/decision",
                       headers=member_headers,
                       json={"decision": "ACCEPTED", "reason": "每周重复整理课程资料"}).status_code == 200
    created = client.post(f"/api/workflow-opportunities/{opportunity_id}/agent-draft",
                          headers=member_headers,
                          json={"name": "来源运维 Agent", "description": "输出待人工核对的建议"})
    draft_id = created.json()["draft_id"]
    stored = app.state.control_plane.store.get_agent_config_draft(draft_id, "local-demo")
    input_hash = hashlib.sha256(json.dumps(json.loads(stored["data_json"]),
                                         sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    reviewer_headers = _role(client, "reviewer")
    digest = hashlib.sha256(b"READ_ONLY").hexdigest()
    cases = [{"case_id": name, "expected_hash": digest, "evidence_ref": name}
             for name in ("source-impact-1", "source-impact-2")]
    assert client.post("/api/meetings/agent-evaluation-datasets", json={
        "dataset_id": "source-impact-eval", "cases": cases,
    }, headers=reviewer_headers).status_code == 200
    assert client.post(f"/api/meetings/agent-drafts/{draft_id}/evaluate", json={
        "input_hash": input_hash, "dataset_id": "source-impact-eval",
        "cases": [case | {"observed_hash": digest} for case in cases],
    }, headers=reviewer_headers).status_code == 200
    review = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck",
                         headers=reviewer_headers).json()
    assert review["status"] == "READY_FOR_ADMIN"
    admin_headers = _role(client, "admin")
    published = client.post(f"/api/meetings/agent-drafts/{draft_id}/confirm", json={
        "review_id": review["review_id"], "input_hash": review["input_hash"],
    }, headers=admin_headers)
    assert published.status_code == 200 and published.json()["version"] == 1

    member_headers = _role(client, "member")
    app.state.control_plane.store.connection.execute(
        "CREATE TRIGGER fail_source_impact BEFORE INSERT ON workflow_source_agent_impacts "
        "BEGIN SELECT RAISE(ABORT, 'synthetic impact failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic impact failure"):
        client.delete(f"/api/workflow-opportunities/subscriptions/{subscription_id}",
                      headers=member_headers)
    assert app.state.control_plane.store.get_workflow_subscription(
        subscription_id, "local-demo", "member",
    )["status"] == "ACTIVE"
    assert not any(event["event_type"] == "WORKFLOW_SOURCE_REVOKED"
                   for event in app.state.control_plane.store.list_tenant_audits("local-demo"))
    app.state.control_plane.store.connection.execute("DROP TRIGGER fail_source_impact")
    assert client.delete(f"/api/workflow-opportunities/subscriptions/{subscription_id}",
                         headers=member_headers).status_code == 200
    assert client.get("/api/meetings/audit").json()["can_view"] is False
    reviewer_headers = _role(client, "reviewer")
    queue = client.get("/api/meetings/audit").json()
    impacts = [item for item in queue["events"]
               if item["event_type"] == "WORKFLOW_AGENT_SOURCE_REVOKED"]
    assert len(impacts) == 1
    impact = impacts[0]
    assert impact["affected_agents"] == [{"agent_id": "来源运维 Agent", "version": 1}]
    assert impact["severity"] == "WARN" and impact["recommended_action"] == "REVIEW_AGENT_SOURCE"
    assert queue["summary"]["incidents"]["OPEN"] >= 1
    assert "oc_published123" not in json.dumps(queue, ensure_ascii=False)
    assert "请每周帮我" not in json.dumps(queue, ensure_ascii=False)
    assert client.post(f'/api/meetings/audit/{impact["audit_ref"]}/triage', json={
        "expected_status": "OPEN", "status": "ACKNOWLEDGED",
    }, headers=reviewer_headers).status_code == 200
    assert next(agent for agent in client.get("/api/meetings/agents").json()["agents"]
                if agent["agent_id"] == "来源运维 Agent")["status"] == "ACTIVE"
    admin_headers = _role(client, "admin")
    admin_events = client.get("/api/meetings/audit").json()["events"]
    assert next(item for item in admin_events if item["event_type"] ==
                "WORKFLOW_AGENT_SOURCE_REVOKED")["affected_agents"] == impact["affected_agents"]
    member_headers = _role(client, "member")
    assert client.delete(f"/api/workflow-opportunities/subscriptions/{subscription_id}",
                         headers=member_headers).status_code == 409
    assert sum(event["event_type"] == "WORKFLOW_AGENT_SOURCE_REVOKED"
               for event in app.state.control_plane.store.list_tenant_audits("local-demo")) == 1


def test_browser_demo_recovers_session_and_draft_after_restart(tmp_path):
    path = tmp_path / "demo.sqlite3"
    first = create_app(path)
    client = TestClient(first)
    headers = _role(client, "member")
    draft_id = _draft(client, headers)
    cookie = client.cookies.get("fde_auth_session")
    first.state.control_plane.store.close()

    restarted = create_app(path)
    recovered = TestClient(restarted)
    recovered.cookies.set("fde_auth_session", cookie)
    assert recovered.get("/local-demo/h5").status_code == 200
    drafts = recovered.get("/api/meetings/agent-drafts").json()
    assert any(item["draft_id"] == draft_id and item["created_by_current_member"]
               for item in drafts["drafts"])
    assert recovered.post("/api/meetings/start", json={}, headers=headers).status_code == 404
    reviewer_headers = _role(recovered, "reviewer")
    review = recovered.post(f"/api/meetings/agent-drafts/{draft_id}/precheck",
                            headers=reviewer_headers)
    assert review.status_code == 200
    assert "AGENT_EVALUATION_REQUIRED" in review.json()["blockers"]
