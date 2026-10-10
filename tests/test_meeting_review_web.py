from fastapi import FastAPI
from fastapi.testclient import TestClient
from datetime import date, datetime, timezone
import pytest

from fde_control_plane import (
    Actor,
    ActorType,
    IdentityContext,
    SQLiteOAuthSessionIssuer,
    build_remote_task_observation,
    extract_remote_task_snapshot,
    persist_remote_task_observation,
    RiskLevel, ToolDefinition,
)
from fde_control_plane.meeting_review_web import MeetingReviewWebService, build_meeting_review_router
from test_feishu_review import setup_review, DOCUMENT_ID, SUBJECT


def web_setup(tmp_path, revision="5", identity=None):
    cp, gateway, reader, owner, task_id, edits = setup_review(tmp_path, revision)
    cp.store.save_meeting_source("record", {
        "document_id": DOCUMENT_ID, "title": "会议纪要",
        "revision_id": "5", "evidence": [{"block_id": 2, "text": "张三整理部署文档"}],
    })
    sessions = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant")
    service = MeetingReviewWebService(gateway)
    app = FastAPI()
    app.include_router(build_meeting_review_router(service, sessions, public_origin="https://example.test"))
    client = TestClient(app)
    client.cookies.set("fde_auth_session", sessions(identity or owner))
    index = client.get("/api/meetings").json()
    detail = client.get("/api/meetings/record").json()
    headers = {"X-CSRF-Token": index["csrf_token"], "Origin": "https://example.test"}
    return cp, reader, client, headers, detail, edits, task_id


def test_owner_can_read_evidence_and_save_then_submit_without_remote_write(tmp_path):
    cp, reader, client, headers, detail, edits, task_id = web_setup(tmp_path)
    assert detail["todos"][0]["evidence"][0]["text"] == "张三整理部署文档"
    assert "document_id" not in detail
    saved = client.post("/api/meetings/record/save", json={"snapshot": detail["snapshot"], "updates": edits}, headers=headers)
    assert saved.status_code == 200
    assert saved.json()["todos"][0]["status"] == "READY_FOR_APPROVAL"
    assert reader.calls == []
    submitted = client.post("/api/meetings/record/submit", json={"snapshot": saved.json()["snapshot"], "updates": {}}, headers=headers)
    assert submitted.status_code == 200
    assert submitted.json()["task_status"] == "WAITING_APPROVAL"
    assert len(submitted.json()["approvals"]) == 1
    assert reader.calls == [DOCUMENT_ID]
    assert cp.store.list_tool_calls(task_id) == []
    replay = client.post("/api/meetings/record/submit", json={"snapshot": saved.json()["snapshot"], "updates": {}}, headers=headers)
    assert replay.status_code == 409
    assert len(cp.store.list_approvals(task_id)) == 1


def test_csrf_origin_unknown_fields_and_old_snapshot_cannot_change_drafts(tmp_path):
    cp, _, client, headers, detail, edits, _ = web_setup(tmp_path)
    body = {"snapshot": detail["snapshot"], "updates": edits}
    before = cp.store.list_meeting_todos("record")
    assert client.post("/api/meetings/record/save", json=body).status_code == 403
    assert client.post("/api/meetings/record/save", json=body, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/meetings/record/save", json={**body, "document_id": "evil"}, headers=headers).status_code == 422
    assert cp.store.list_meeting_todos("record") == before
    assert client.post("/api/meetings/record/save", json=body, headers=headers).status_code == 200
    assert client.post("/api/meetings/record/save", json=body, headers=headers).status_code == 409


def test_other_bound_member_and_logged_out_user_cannot_read_meeting(tmp_path):
    cp, gateway, _, owner, _, _ = setup_review(tmp_path)
    sessions = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant")
    app = FastAPI()
    app.include_router(build_meeting_review_router(MeetingReviewWebService(gateway), sessions, public_origin="https://example.test"))
    client = TestClient(app)
    assert client.get("/api/meetings").status_code == 401
    client.cookies.set("fde_auth_session", sessions(IdentityContext("tenant", "admin", "user_oauth", "admin-subject")))
    assert client.get("/api/meetings").json()["meetings"] == []
    assert client.get("/api/meetings/record").status_code == 404


def test_member_access_requires_admin_and_preserves_meeting_isolation(tmp_path):
    cp, _, client, headers, _, _, _ = web_setup(tmp_path)
    subject_hash = "b" * 64
    request = cp.store.request_member_access("tenant", subject_hash, "member-request-test2")
    assert request["status"] == "PENDING"
    assert client.get("/api/meetings/member-access-requests").status_code == 403
    url = "/api/meetings/member-access-requests/member-request-test2/approve"
    assert client.post(url, headers=headers).status_code == 403
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    listed = client.get("/api/meetings/member-access-requests")
    assert listed.status_code == 200
    assert listed.json()["requests"][0]["request_id"] == "member-request-test2"
    assert subject_hash not in listed.text
    assert client.post(url).status_code == 403
    approved = client.post(url, headers=admin_headers)
    assert approved.status_code == 200 and approved.json()["duplicate"] is False
    actor_id = approved.json()["actor_id"]
    assert cp._actor(actor_id).roles == frozenset()
    assert client.post(url, headers=admin_headers).json()["duplicate"] is True
    assert client.get("/api/meetings/member-access-requests").json()["requests"] == []
    second_headers = switch_user(cp, client, actor_id, subject_hash)
    assert client.get("/api/meetings").json()["meetings"] == []
    assert client.get("/api/meetings/record").status_code == 404
    assert client.get("/api/meetings/member-access-requests").status_code == 403
    assert client.post(url, headers=second_headers).status_code == 403


def test_daily_digest_preview_is_authenticated_redacted_and_unsent(tmp_path):
    cp, _, client, _, _, _, task_id = web_setup(tmp_path)
    empty = client.get("/api/meetings/digest?business_date=2026-10-05").json()
    assert empty["business_date"] == "2026-10-05"
    assert empty["counts"] == {} and empty["attention_count"] == 0
    assert empty["attention_task_hashes"] == []
    assert len(empty["notification_key_hash"]) == 12
    assert empty["notification_sent"] is False and empty["remote_write"] is False
    observation = build_remote_task_observation(
        tenant_id="tenant", task_run_id=task_id,
        snapshot=extract_remote_task_snapshot({"guid": "remote-secret", "status": "TODO", "due": {"timestamp": 1791062400000}}, now=date(2026, 10, 5)),
        observed_at="2026-10-05T12:00:00Z",
    )
    persist_remote_task_observation(cp.store, observation)
    response = client.get("/api/meetings/digest?business_date=2026-10-05")
    assert response.status_code == 200
    result = response.json()
    assert result["counts"] == {"OVERDUE": 1}
    assert result["attention_count"] == 1
    assert result["notification_sent"] is False and result["remote_write"] is False
    assert "remote-secret" not in response.text and task_id not in response.text
    assert client.get("/api/meetings/digest?business_date=bad-date").status_code == 422


def test_agent_directory_is_tenant_scoped_and_redacts_external_identity(tmp_path):
    cp, _, client, _, _, _, _ = web_setup(tmp_path)
    response = client.get("/api/meetings/agents")
    assert response.status_code == 200
    agents = response.json()["agents"]
    assert {item["agent_id"] for item in agents} == {"assistant", "agent"}
    business = next(item for item in agents if item["agent_id"] == "agent")
    assert business["actor_type"] == "BUSINESS_AGENT"
    assert business["status"] == "ACTIVE"
    assert business["current_version"] == 1
    assert business["version_count"] == 1
    assert "doc.read" in business["capabilities"]
    assistant = next(item for item in agents if item["agent_id"] == "assistant")
    assert assistant["status"] == "UNVERSIONED"
    assert assistant["current_version"] is None
    assert "external_ref_hash" not in response.text
    assert "admin-subject" not in response.text
    assert client.get("/api/meetings").status_code == 200


def test_member_can_submit_agent_config_draft_without_changing_runtime(tmp_path):
    cp, _, client, headers, _, _, _ = web_setup(tmp_path)
    body = {
        "name": "任务跟进 Agent",
        "actor_type": "BUSINESS_AGENT",
        "description": "汇总任务进度并生成每日跟进预览。",
        "capabilities": ["task.read", "task.read", "message.send"],
        "skill_version": "follow-up.v1",
    }
    created = client.post("/api/meetings/agent-drafts", json=body, headers=headers)
    assert created.status_code == 200
    assert created.json()["status"] == "PENDING_REVIEW"
    drafts = client.get("/api/meetings/agent-drafts")
    assert drafts.status_code == 200
    item = drafts.json()["drafts"][0]
    assert item["name"] == body["name"]
    assert item["status"] == "PENDING_REVIEW"
    assert item["capabilities"] == ["message.send", "task.read"]
    assert all(key not in drafts.text for key in ("external_ref_hash", "app_secret", "access_token"))
    assert not any(agent["agent_id"] == body["name"] for agent in client.get("/api/meetings/agents").json()["agents"])
    assert any(a["event_type"] == "AGENT_CONFIG_DRAFT_CREATED" for a in cp.store.list_audits())


def test_agent_config_precheck_is_role_gated_deterministic_and_idempotent(tmp_path):
    cp, _, client, headers, _, _, _ = web_setup(tmp_path)
    created = client.post("/api/meetings/agent-drafts", json={
        "name": "审核测试 Agent", "actor_type": "BUSINESS_AGENT",
        "description": "读取任务并发送跟进提醒。", "capabilities": ["task.write", "task.write"],
    }, headers=headers)
    assert created.status_code == 200
    draft_id = created.json()["draft_id"]
    assert client.get("/api/meetings/agent-drafts").json()["can_precheck"] is False
    assert client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck", headers=headers).status_code == 403

    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    listing = client.get("/api/meetings/agent-drafts").json()
    assert listing["can_precheck"] is True
    first = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck", headers=admin_headers)
    assert first.status_code == 200
    assert first.json()["status"] == "READY_FOR_ADMIN"
    assert first.json()["warnings"] == ["HIGH_RISK_CAPABILITY:task.write"]
    second = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck", headers=admin_headers)
    assert second.status_code == 200 and second.json()["duplicate"] is True
    item = next(item for item in client.get("/api/meetings/agent-drafts").json()["drafts"] if item["draft_id"] == draft_id)
    assert item["review_status"] == "READY_FOR_ADMIN"
    assert cp.store.get_latest_agent_config_review(draft_id, "tenant")["status"] == "READY_FOR_ADMIN"
    assert not any(agent["agent_id"] == "审核测试 Agent" for agent in client.get("/api/meetings/agents").json()["agents"])


def test_agent_config_precheck_blocks_unknown_capability(tmp_path):
    cp, _, client, headers, _, _, _ = web_setup(tmp_path)
    created = client.post("/api/meetings/agent-drafts", json={
        "name": "未知能力 Agent", "actor_type": "BUSINESS_AGENT",
        "description": "测试未知工具。", "capabilities": ["unknown.tool"],
    }, headers=headers)
    draft_id = created.json()["draft_id"]
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    result = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck", headers=admin_headers)
    assert result.status_code == 200
    assert result.json()["status"] == "BLOCKED"
    assert result.json()["blockers"] == ["UNKNOWN_CAPABILITY:unknown.tool"]


def test_admin_confirmation_requires_current_precheck_and_publishes_immutable_v1(tmp_path):
    cp, _, client, headers, _, _, _ = web_setup(tmp_path)
    created = client.post("/api/meetings/agent-drafts", json={
        "name": "确认发布 Agent", "actor_type": "BUSINESS_AGENT",
        "description": "读取任务并生成跟进建议。", "capabilities": ["task.read"],
        "skill_version": "follow-up.v1",
    }, headers=headers)
    draft_id = created.json()["draft_id"]
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    assert client.get("/api/meetings/agent-drafts").json()["can_confirm"] is True
    assert client.post(f"/api/meetings/agent-drafts/{draft_id}/confirm", json={
        "review_id": "missing", "input_hash": "0" * 64,
    }, headers=admin_headers).status_code == 409
    review = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck", headers=admin_headers).json()
    assert review["status"] == "READY_FOR_ADMIN"
    result = client.post(f"/api/meetings/agent-drafts/{draft_id}/confirm", json={
        "review_id": review["review_id"], "input_hash": review["input_hash"],
    }, headers=admin_headers)
    assert result.status_code == 200
    assert result.json()["status"] == "PUBLISHED"
    assert result.json()["version"] == 1 and result.json()["duplicate"] is False
    replay = client.post(f"/api/meetings/agent-drafts/{draft_id}/confirm", json={
        "review_id": review["review_id"], "input_hash": review["input_hash"],
    }, headers=admin_headers)
    assert replay.status_code == 200 and replay.json()["duplicate"] is True
    draft = client.get("/api/meetings/agent-drafts").json()["drafts"][0]
    assert draft["status"] == "PUBLISHED"
    agent = next(item for item in client.get("/api/meetings/agents").json()["agents"] if item["agent_id"] == "确认发布 Agent")
    assert agent["current_version"] == 1 and agent["status"] == "ACTIVE"
    assert cp.store.connection.execute("SELECT COUNT(*) FROM agent_config_approvals").fetchone()[0] == 1
    assert any(a["event_type"] == "AGENT_CONFIG_CONFIRMED" for a in cp.store.list_audits())


def test_agent_config_confirmation_rejects_blocked_review_and_non_admin(tmp_path):
    cp, _, client, headers, _, _, _ = web_setup(tmp_path)
    created = client.post("/api/meetings/agent-drafts", json={
        "name": "阻断发布 Agent", "actor_type": "BUSINESS_AGENT",
        "description": "测试阻断发布。", "capabilities": ["unknown.tool"],
    }, headers=headers)
    draft_id = created.json()["draft_id"]
    assert client.post(f"/api/meetings/agent-drafts/{draft_id}/confirm", json={
        "review_id": "missing", "input_hash": "0" * 64,
    }, headers=headers).status_code == 403
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    review = client.post(f"/api/meetings/agent-drafts/{draft_id}/precheck", headers=admin_headers).json()
    assert review["status"] == "BLOCKED"
    result = client.post(f"/api/meetings/agent-drafts/{draft_id}/confirm", json={
        "review_id": review["review_id"], "input_hash": review["input_hash"],
    }, headers=admin_headers)
    assert result.status_code == 409
    assert not any(item["agent_id"] == "阻断发布 Agent" for item in client.get("/api/meetings/agents").json()["agents"])


def test_agent_config_draft_requires_csrf_and_rejects_unknown_fields(tmp_path):
    _, _, client, headers, _, _, _ = web_setup(tmp_path)
    body = {"name": "Valid X", "actor_type": "BUSINESS_AGENT", "description": "测试"}
    assert client.post("/api/meetings/agent-drafts", json=body).status_code == 403
    assert client.post("/api/meetings/agent-drafts", json={**body, "unexpected": True}, headers=headers).status_code == 422


def test_stale_document_cannot_submit_or_modify_stored_draft(tmp_path):
    cp, reader, client, headers, detail, edits, task_id = web_setup(tmp_path, revision="6")
    before = cp.store.list_meeting_todos("record")
    response = client.post("/api/meetings/record/submit", json={"snapshot": detail["snapshot"], "updates": edits}, headers=headers)
    assert response.status_code == 409
    assert "源文档已更新" in response.json()["detail"]
    assert cp.store.list_meeting_todos("record") == before
    assert cp.store.list_approvals(task_id) == []


def test_all_discard_is_cancelled_and_unbound_assignee_is_rejected(tmp_path):
    cp, _, client, headers, detail, edits, task_id = web_setup(tmp_path)
    cp.register_actor(Actor("unbound", "tenant", ActorType.USER))
    ref = next(iter(edits))
    invalid = {ref: {"assignee_actor_id": "unbound", "due_date": "2026-10-05"}}
    assert client.post("/api/meetings/record/save", json={"snapshot": detail["snapshot"], "updates": invalid}, headers=headers).status_code == 422
    response = client.post("/api/meetings/record/submit", json={"snapshot": detail["snapshot"], "updates": {ref: {"discard": True}}}, headers=headers)
    assert response.status_code == 200
    assert response.json()["task_status"] == "CANCELLED"
    assert cp.store.list_tool_calls(task_id) == []


def test_invalid_batch_leaves_all_drafts_untouched(tmp_path):
    cp, _, client, headers, detail, edits, _ = web_setup(tmp_path)
    before = cp.store.list_meeting_todos("record")
    updates = {**edits, "000000000000": {"discard": True}}
    assert client.post("/api/meetings/record/save", json={"snapshot": detail["snapshot"], "updates": updates}, headers=headers).status_code == 422
    assert cp.store.list_meeting_todos("record") == before


def test_revoked_agent_permissions_block_submission_before_saving_edits(tmp_path):
    cp, _, client, headers, detail, edits, task_id = web_setup(tmp_path)
    cp.register_actor(Actor("agent", "tenant", ActorType.BUSINESS_AGENT, active=False))
    before = cp.store.list_meeting_todos("record")
    result = client.post("/api/meetings/record/submit", json={"snapshot": detail["snapshot"], "updates": edits}, headers=headers)
    assert result.status_code == 403
    assert cp.store.list_meeting_todos("record") == before
    assert cp.store.list_approvals(task_id) == []


def test_card_delivery_state_is_visible_without_exposing_message_or_recipient_ids(tmp_path):
    cp, _, client, headers, detail, edits, _ = web_setup(tmp_path)
    submitted = client.post("/api/meetings/record/submit", json={"snapshot": detail["snapshot"], "updates": edits}, headers=headers).json()
    approval = submitted["approvals"][0]
    assert approval["card_status"] == "NOT_SENT"
    approval_id = approval["approval_id"]
    proposal_hash = cp.store.get_approval(approval_id)["proposal_hash"]
    cp.store.claim_approval_delivery(approval_id, "member", proposal_hash, "test-card-send")
    uncertain = client.get("/api/meetings/record").json()["approvals"][0]
    assert uncertain["card_status"] == "UNKNOWN"
    cp.store.save_approval_delivery(approval_id, "om_private", "member", proposal_hash)
    delivered = client.get("/api/meetings/record").json()["approvals"][0]
    assert delivered["card_status"] == "SENT"
    assert set(delivered) == {"approval_id", "status", "card_status"}


def test_execution_summary_is_redacted_and_shows_failure_category(tmp_path):
    cp, _, client, headers, detail, edits, task_id = web_setup(tmp_path)
    submitted = client.post(
        "/api/meetings/record/submit",
        json={"snapshot": detail["snapshot"], "updates": edits},
        headers=headers,
    ).json()
    approval = cp.store.get_approval(submitted["approvals"][0]["approval_id"])
    decision = cp.handle_approval_callback(
        "execution-failure-event", approval["approval_id"], "member", True,
        approval["action_version"], approval["proposal_hash"],
    )
    cp.complete_tool_call(decision["tool_call_id"], False, error_code="SOURCE_REVISION_STALE")

    execution = client.get("/api/meetings/record").json()["execution"]
    assert execution["counts"] == {"FAILED": 1}
    assert execution["items"] == [{
        "status": "FAILED",
        "error_code": "SOURCE_REVISION_STALE",
        "remote_ref_present": False,
        "outbox_status": "READY",
    }]
    assert "tool_call_id" not in execution["items"][0]
    assert "write_idempotency_key" not in execution["items"][0]


def ready_owner_tasks(tmp_path):
    cp, _, client, headers, detail, edits, task_id = web_setup(tmp_path)
    edits = {ref: {**values, "due_date": "2027-01-01"} for ref, values in edits.items()}
    response = client.post("/api/meetings/record/submit",
                           json={"snapshot": detail["snapshot"], "updates": edits}, headers=headers)
    approval = cp.store.get_approval(response.json()["approvals"][0]["approval_id"])
    first = cp.handle_approval_callback("first", approval["approval_id"], "member", True,
                                       approval["action_version"], approval["proposal_hash"])
    cp.dispatch_tool_call(first["tool_call_id"])
    cp.complete_tool_call(first["tool_call_id"], True, remote_ref="remote-owner-secret")
    # Same meeting run, separate task assigned to a different member.
    proposal = cp.create_operation_proposal(
        task_id, "second", "task.create", "task", {
            "title": "仅第二负责人可见的事项", "due_date": "2027-01-01", "assignee_actor_id": "admin",
        }, ToolDefinition("task.create", frozenset({"task.write"}), RiskLevel.HIGH, True),
        eligible_approver_ids=frozenset({"member"}),
    )
    other = cp.get_approval_for_operation(proposal.operation_id)
    second = cp.handle_approval_callback("second", other.approval_id, "member", True, 1, proposal.proposal_hash)
    cp.dispatch_tool_call(second["tool_call_id"])
    cp.complete_tool_call(second["tool_call_id"], True, remote_ref="remote-other-secret")
    for remote in ["remote-owner-secret", "remote-other-secret"]:
        persist_remote_task_observation(cp.store, build_remote_task_observation(
            tenant_id="tenant", task_run_id=task_id,
            snapshot=extract_remote_task_snapshot({
                "guid": remote, "status": "todo",
                "due": {"timestamp": int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)},
            }),
        ))
    return cp, client, headers, task_id


def switch_user(cp, client, actor_id, subject):
    sessions = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant")
    client.cookies.set("fde_auth_session", sessions(IdentityContext("tenant", actor_id, "user_oauth", subject)))
    csrf = client.get("/api/meetings/owner-tasks").json()["csrf_token"]
    return {"X-CSRF-Token": csrf, "Origin": "https://example.test"}


@pytest.mark.parametrize("decision,reason,expected", [
    ("ACCEPTED", None, "IN_PROGRESS"), ("RETURNED", "截止日期需要协商", "WAITING_HUMAN"),
])
def test_owner_web_records_decision_and_refreshes_digest_without_remote_effects(tmp_path, decision, reason, expected):
    cp, client, headers, task_id = ready_owner_tasks(tmp_path)
    listing = client.get("/api/meetings/owner-tasks").json()
    assert len(listing["tasks"]) == 1
    item = listing["tasks"][0]
    assert item["can_decide"] and item["title"] == "整理部署文档"
    body = {"event_id": "owner-click", "source_revision": item["source_revision"], "decision": decision, "reason": reason}
    url = f'/api/meetings/owner-tasks/{item["task_ref"]}/decision'
    before_calls = cp.store.list_tool_calls(task_id)
    result = client.post(url, json=body, headers=headers)
    assert result.status_code == 200
    assert result.json() == {"accepted": True, "status": expected, "duplicate": False,
                             "notification_sent": False, "remote_write": False}
    assert client.post(url, json=body, headers=headers).json()["duplicate"] is True
    refreshed = client.get("/api/meetings/owner-tasks").json()["tasks"][0]
    assert refreshed["decision"] == decision and not refreshed["can_decide"]
    assert refreshed["reason"] == reason
    assert client.get("/api/meetings/digest").json()["counts"] == {expected: 1, "WAITING_OWNER": 1}
    assert cp.store.list_tool_calls(task_id) == before_calls
    assert cp.store.connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 0
    text = str(listing) + result.text
    assert "remote-owner-secret" not in text and "remote-other-secret" not in text and task_id not in text


def test_owner_web_isolates_sibling_tasks_and_hides_all_from_unrelated_member(tmp_path):
    cp, client, _, _ = ready_owner_tasks(tmp_path)
    member_task = client.get("/api/meetings/owner-tasks").json()["tasks"][0]
    headers = switch_user(cp, client, "admin", "admin-subject")
    listing = client.get("/api/meetings/owner-tasks").json()
    assert len(listing["tasks"]) == 1 and listing["tasks"][0]["title"] == "仅第二负责人可见的事项"
    assert client.get("/api/meetings/digest").json()["counts"] == {"WAITING_OWNER": 1}
    assert client.get("/api/meetings").json()["meetings"] == []
    assert client.get("/api/meetings/record").status_code == 404
    body = {"event_id": "stolen-ref", "source_revision": member_task["source_revision"], "decision": "ACCEPTED"}
    assert client.post(f'/api/meetings/owner-tasks/{member_task["task_ref"]}/decision', json=body, headers=headers).status_code == 404
    cp.register_actor(Actor("stranger", "tenant", ActorType.USER, external_ref_hash="stranger-hash"))
    switch_user(cp, client, "stranger", "stranger-hash")
    assert client.get("/api/meetings/owner-tasks").json()["tasks"] == []
    assert client.get("/api/meetings/digest").json()["counts"] == {}


def test_owner_web_rejects_csrf_stale_revision_spoofed_identity_and_blank_return(tmp_path):
    cp, client, headers, task_id = ready_owner_tasks(tmp_path)
    item = client.get("/api/meetings/owner-tasks").json()["tasks"][0]
    url = f'/api/meetings/owner-tasks/{item["task_ref"]}/decision'
    body = {"event_id": "click", "source_revision": item["source_revision"], "decision": "ACCEPTED"}
    assert client.post(url, json=body).status_code == 403
    assert client.post(url, json=body, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    assert client.post(url, json={**body, "owner_actor_id": "admin"}, headers=headers).status_code == 422
    assert client.post(url, json={**body, "source_revision": "0" * 64}, headers=headers).status_code == 409
    assert client.post(url, json={**body, "decision": "RETURNED", "reason": "  "}, headers=headers).status_code == 422
    assert len(cp.store.list_follow_up_observations(task_id)) == 2  # original remote observations only
    client.cookies.clear()
    assert client.get("/api/meetings/owner-tasks").status_code == 401
    assert client.post(url, json=body, headers=headers).status_code == 401


def test_owner_web_revoked_session_and_terminal_remote_state_block_actions(tmp_path):
    cp, client, headers, task_id = ready_owner_tasks(tmp_path)
    item = client.get("/api/meetings/owner-tasks").json()["tasks"][0]
    persist_remote_task_observation(cp.store, build_remote_task_observation(
        tenant_id="tenant", task_run_id=task_id,
        snapshot=extract_remote_task_snapshot({"guid": "remote-owner-secret", "status": "done"}),
        observed_at="2099-01-01T00:00:00Z",
    ))
    assert not client.get("/api/meetings/owner-tasks").json()["tasks"][0]["can_decide"]
    body = {"event_id": "terminal", "source_revision": item["source_revision"], "decision": "ACCEPTED"}
    assert client.post(f'/api/meetings/owner-tasks/{item["task_ref"]}/decision', json=body, headers=headers).status_code == 409
    cp.store.connection.execute("UPDATE actors SET active=0 WHERE actor_id='member'")
    assert client.get("/api/meetings/owner-tasks").status_code == 401
    assert client.get("/api/meetings/digest").status_code == 401


def test_due_mismatch_blocks_owner_click_and_digest_keeps_it_unknown(tmp_path):
    cp, client, headers, task_id = ready_owner_tasks(tmp_path)
    item = client.get("/api/meetings/owner-tasks").json()["tasks"][0]
    persist_remote_task_observation(cp.store, build_remote_task_observation(
        tenant_id="tenant", task_run_id=task_id,
        snapshot=extract_remote_task_snapshot({
            "guid": "remote-owner-secret", "status": "todo",
            "due": {"timestamp": int(datetime(2027, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)},
        }), observed_at="2099-01-01T00:00:00Z",
    ))
    refreshed = client.get("/api/meetings/owner-tasks").json()["tasks"][0]
    assert refreshed["due_mismatch"] and not refreshed["can_decide"]
    assert refreshed["status"] == "UNKNOWN"
    assert client.get("/api/meetings/digest").json()["counts"] == {"UNKNOWN": 1, "WAITING_OWNER": 1}
    response = client.post(f'/api/meetings/owner-tasks/{item["task_ref"]}/decision', json={
        "event_id": "mismatched-due", "source_revision": item["source_revision"], "decision": "ACCEPTED",
    }, headers=headers)
    assert response.status_code == 409 and "截止日期" in response.json()["detail"]
    assert not any(row.get("decision") for row in cp.store.list_follow_up_observations(task_id))


def test_due_mismatch_after_decision_preserves_decision_and_pauses_digest(tmp_path):
    cp, client, headers, task_id = ready_owner_tasks(tmp_path)
    item = client.get("/api/meetings/owner-tasks").json()["tasks"][0]
    assert client.post(f'/api/meetings/owner-tasks/{item["task_ref"]}/decision', json={
        "event_id": "accepted-before-drift", "source_revision": item["source_revision"], "decision": "ACCEPTED",
    }, headers=headers).status_code == 200
    persist_remote_task_observation(cp.store, build_remote_task_observation(
        tenant_id="tenant", task_run_id=task_id,
        snapshot=extract_remote_task_snapshot({
            "guid": "remote-owner-secret", "status": "todo",
            "due": {"timestamp": int(datetime(2027, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)},
        }), observed_at="2099-01-01T00:00:00Z",
    ))
    refreshed = client.get("/api/meetings/owner-tasks").json()["tasks"][0]
    assert refreshed["decision"] == "ACCEPTED" and refreshed["due_mismatch"]
    assert refreshed["status"] == "UNKNOWN" and not refreshed["can_decide"]
    assert client.get("/api/meetings/digest").json()["counts"] == {"UNKNOWN": 1, "WAITING_OWNER": 1}


def test_organizer_due_correction_requires_two_explicit_steps_and_remote_verification(tmp_path):
    from fde_control_plane.task_due_correction import TaskDueCorrectionWorker

    cp, client, headers, task_id = ready_owner_tasks(tmp_path)
    persist_remote_task_observation(cp.store, build_remote_task_observation(
        tenant_id="tenant", task_run_id=task_id,
        snapshot=extract_remote_task_snapshot({
            "guid": "remote-owner-secret", "status": "todo",
            "due": {"timestamp": int(datetime(2027, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)},
        }),
    ))
    tasks = client.get("/api/meetings/organizer-tasks").json()["tasks"]
    item = next(task for task in tasks if task["due_mismatch"])
    assert item["approved_due_date"] == "2027-01-01" and item["remote_due_date"] == "2027-07-01"
    url = f'/api/meetings/organizer-tasks/{item["task_ref"]}/due-correction'
    body = {"source_revision": item["source_revision"], "observation_id": item["observation_id"]}
    assert client.post(url, json=body).status_code == 403
    assert client.post(url, json={**body, "remote_task_id": "other"}, headers=headers).status_code == 422
    pending = client.post(url, json=body, headers=headers).json()
    assert pending["status"] == "PENDING" and not pending["remote_write"]
    assert client.post(url, json=body, headers=headers).json()["correction_id"] == pending["correction_id"]
    approve_url = f'{url}/{pending["correction_id"]}/approve'
    assert client.post(approve_url, json={"proposal_hash": "0" * 64}, headers=headers).status_code == 409
    assert client.post(approve_url, json={"proposal_hash": pending["proposal_hash"]}, headers=headers).json() == {
        "status": "APPROVED", "remote_write": False,
    }
    assert client.post(approve_url, json={"proposal_hash": pending["proposal_hash"]}, headers=headers).status_code == 409

    class Gateway:
        task = {"guid": "remote-owner-secret", "status": "todo",
                "due": {"timestamp": int(datetime(2027, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)}}
        patches = 0

        def get_task(self, remote_id):
            assert remote_id == self.task["guid"]
            return self.task

        def patch_task_due(self, remote_id, due_date):
            assert remote_id == self.task["guid"] and due_date == "2027-01-01"
            self.patches += 1
            self.task = {**self.task, "due": {
                "timestamp": int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
                "is_all_day": True}}

    gateway = Gateway()
    result = TaskDueCorrectionWorker(cp, gateway).execute(pending["correction_id"])
    assert result["status"] == "SUCCEEDED" and gateway.patches == 1
    assert client.get("/api/meetings/organizer-tasks").json()["tasks"][0]["due_mismatch"] is False
    assert client.get("/api/meetings/owner-tasks").json()["tasks"][0]["can_decide"]
    assert len([a for a in cp.store.list_audits() if a["event_type"] == "TASK_DUE_CORRECTION_VERIFIED"]) == 1


def test_due_correction_unknown_never_replays_patch_and_other_member_cannot_approve(tmp_path):
    from fde_control_plane.task_due_correction import TaskDueCorrectionWorker

    cp, client, headers, task_id = ready_owner_tasks(tmp_path)
    persist_remote_task_observation(cp.store, build_remote_task_observation(
        tenant_id="tenant", task_run_id=task_id,
        snapshot=extract_remote_task_snapshot({"guid": "remote-owner-secret", "status": "todo",
            "due": {"timestamp": int(datetime(2027, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)}}),
    ))
    item = next(task for task in client.get("/api/meetings/organizer-tasks").json()["tasks"] if task["due_mismatch"])
    url = f'/api/meetings/organizer-tasks/{item["task_ref"]}/due-correction'
    pending = client.post(url, json={"source_revision": item["source_revision"],
                                     "observation_id": item["observation_id"]}, headers=headers).json()
    admin_headers = switch_user(cp, client, "admin", "admin-subject")
    assert client.post(f'{url}/{pending["correction_id"]}/approve',
                       json={"proposal_hash": pending["proposal_hash"]}, headers=admin_headers).status_code == 404
    headers = switch_user(cp, client, "member", SUBJECT)
    assert client.post(f'{url}/{pending["correction_id"]}/approve',
                       json={"proposal_hash": pending["proposal_hash"]}, headers=headers).status_code == 200

    class UncertainGateway:
        patches = 0

        def get_task(self, remote_id):
            return {"guid": remote_id, "status": "todo",
                    "due": {"timestamp": int(datetime(2027, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)}}

        def patch_task_due(self, *_):
            self.patches += 1
            raise TimeoutError("unknown remote result")

    gateway = UncertainGateway()
    worker = TaskDueCorrectionWorker(cp, gateway)
    assert worker.execute(pending["correction_id"])["status"] == "UNKNOWN"
    with pytest.raises(ValueError, match="CORRECTION_NOT_APPROVED"):
        worker.execute(pending["correction_id"])
    assert worker.reconcile(pending["correction_id"])["status"] == "UNKNOWN"
    assert gateway.patches == 1


def test_pending_due_correction_is_invalidated_when_remote_observation_changes(tmp_path):
    cp, client, headers, task_id = ready_owner_tasks(tmp_path)
    def observe(day):
        persist_remote_task_observation(cp.store, build_remote_task_observation(
            tenant_id="tenant", task_run_id=task_id,
            snapshot=extract_remote_task_snapshot({"guid": "remote-owner-secret", "status": "todo",
                "due": {"timestamp": int(datetime(2027, day, 1, tzinfo=timezone.utc).timestamp() * 1000)}}),
        ))

    observe(7)
    first_item = next(task for task in client.get("/api/meetings/organizer-tasks").json()["tasks"] if task["due_mismatch"])
    url = f'/api/meetings/organizer-tasks/{first_item["task_ref"]}/due-correction'
    old = client.post(url, json={"source_revision": first_item["source_revision"],
                                 "observation_id": first_item["observation_id"]}, headers=headers).json()
    observe(8)
    new_item = next(task for task in client.get("/api/meetings/organizer-tasks").json()["tasks"] if task["due_mismatch"])
    assert new_item["can_propose_correction"]
    assert client.post(f'{url}/{old["correction_id"]}/approve',
                       json={"proposal_hash": old["proposal_hash"]}, headers=headers).status_code == 409
    new = client.post(url, json={"source_revision": new_item["source_revision"],
                                 "observation_id": new_item["observation_id"]}, headers=headers).json()
    assert new["correction_id"] != old["correction_id"]
    assert cp.store.connection.execute("SELECT status FROM task_due_corrections WHERE correction_id = ?",
                                       (old["correction_id"],)).fetchone()[0] == "INVALIDATED"
