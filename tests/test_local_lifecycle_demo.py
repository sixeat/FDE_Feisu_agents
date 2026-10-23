"""The local browser demo uses real governance APIs and synthetic runtime only."""

import hashlib
import json

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
    member_headers = _role(client, "member")
    assert client.get("/h5", follow_redirects=False).headers["location"] == "/local-demo/h5#agents"
    workspace = client.get("/local-demo/h5")
    assert workspace.status_code == 200
    assert '/assets/local_demo_ui.js' in workspace.text
    assert '/assets/local_demo.css' in workspace.text
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
    member_headers = _role(client, "member")
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
    ]["read_only"]["old"]["task_run_id"] == run.json()["task_run_id"]
    assert client.post(route + "/rollback", json={
        "expected_version": 2, "target_version": 1, "reason": "local sample rollback",
    }, headers=admin_headers).status_code == 200
    assert client.post(route + "/archive", json={
        "expected_version": 1, "reason": "local sample archived",
    }, headers=admin_headers).status_code == 200
    assert app.state.control_plane.store.select_agent_version(
        "local-demo", "sample-lifecycle-agent", "member"
    ) is None


def test_browser_demo_restricts_login_origin_and_remote_clients(tmp_path):
    app = create_app(tmp_path / "demo.sqlite3")
    client = TestClient(app)
    assert client.post("/local-demo/login/admin", headers={
        "Origin": "https://another.example",
    }).status_code == 403
    assert client.post("/local-demo/login/unknown").status_code == 404
    remote = TestClient(app, client=("203.0.113.9", 40000))
    assert remote.get("/local-demo").status_code == 403


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
