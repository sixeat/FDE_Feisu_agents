from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fde_control_plane import (
    Actor, ActorType, ControlPlane, IdentityContext, SQLiteOAuthSessionIssuer, SQLiteStore,
)
from fde_control_plane.meeting_review_web import csrf_token
from fde_control_plane.feishu_oauth import OAuthGrant
from fde_control_plane.user_document_access import UserDocumentAuthorizationRequired, UserTokenVault
from fde_control_plane.workflow_discovery import (
    CHAT_GROUP_READ_SCOPE, CHAT_P2P_READ_SCOPE, ChatMessage,
    FeishuUserChatHistoryReader, WORKFLOW_SOURCE_CONSENT_VERSION, WorkflowDiscoveryService,
)
from fde_control_plane.workflow_web import build_workflow_discovery_router


CHAT_TEXT = "请每周帮我把课程资料整理成待确认事项"
NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


class FakeChatReader:
    def __init__(self):
        self.calls = []
        self.messages = [
            ChatMessage("message-one", NOW - timedelta(hours=2), CHAT_TEXT),
            ChatMessage("message-two", NOW - timedelta(hours=1), CHAT_TEXT),
            ChatMessage("unrelated", NOW - timedelta(minutes=30), "今天下午两点开会"),
        ]

    def list_messages(self, identity, chat_id, chat_type, start_at, end_at):
        self.calls.append((identity.actor_id, chat_id, chat_type, start_at, end_at))
        return self.messages


def setup(tmp_path):
    store = SQLiteStore(tmp_path / "workflow.sqlite3")
    cp = ControlPlane(store)
    subjects = {
        name: hashlib.sha256(f"open-id-{name}".encode()).hexdigest()
        for name in ("member", "other")
    }
    for name, subject in subjects.items():
        cp.register_actor(Actor(name, "tenant", ActorType.USER, external_ref_hash=subject))
    identities = {
        "member": IdentityContext("tenant", "member", "user_oauth", subjects["member"],
                                   frozenset({CHAT_GROUP_READ_SCOPE})),
        "other": IdentityContext("tenant", "other", "user_oauth", subjects["other"],
                                  frozenset({CHAT_GROUP_READ_SCOPE})),
        "no_scope": IdentityContext("tenant", "member", "user_oauth", subjects["member"]),
        "p2p": IdentityContext("tenant", "member", "user_oauth", subjects["member"],
                               frozenset({CHAT_P2P_READ_SCOPE})),
    }
    reader = FakeChatReader()
    service = WorkflowDiscoveryService(cp, reader, clock=lambda: NOW)
    return cp, service, reader, identities


def test_subscription_consent_version_migration_preserves_existing_records(tmp_path):
    path = tmp_path / "legacy-workflow.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE workflow_source_subscriptions ("
        "subscription_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, member_id TEXT NOT NULL, "
        "chat_id TEXT NOT NULL, chat_type TEXT NOT NULL, display_label TEXT NOT NULL, "
        "fingerprint_salt TEXT NOT NULL, status TEXT NOT NULL, "
        "consented_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, revoked_at TEXT)"
    )
    connection.execute(
        "INSERT INTO workflow_source_subscriptions(subscription_id, tenant_id, member_id, chat_id, "
        "chat_type, display_label, fingerprint_salt, status) "
        "VALUES ('legacy', 'tenant', 'member', 'oc_123456789', 'GROUP', 'old source', 'salt', 'ACTIVE')"
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(path)
    row = store.get_workflow_subscription("legacy", "tenant", "member")
    columns = {column["name"] for column in store.connection.execute(
        "PRAGMA table_info(workflow_source_subscriptions)")}
    assert "consent_version" in columns
    assert row["consent_version"] is None
    assert row["status"] == "ACTIVE"


@pytest.mark.parametrize("shared_store", [False, True])
def test_concurrent_subscription_of_same_chat_creates_only_one_active_source(tmp_path, shared_store):
    path = tmp_path / "concurrent-subscription.sqlite3"
    first = SQLiteStore(path)
    second = first if shared_store else SQLiteStore(path)
    barrier = Barrier(2)

    def subscribe(store, subscription_id):
        barrier.wait()
        try:
            store.create_workflow_subscription(
                subscription_id=subscription_id, tenant_id="tenant", member_id="member",
                chat_id="oc_123456789", chat_type="GROUP", display_label="测试群",
                fingerprint_salt="ab" * 32, consent_version=WORKFLOW_SOURCE_CONSENT_VERSION,
            )
            return "created"
        except ValueError as exc:
            assert str(exc) == "chat source is already subscribed"
            return "duplicate"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(subscribe, (first, second), ("source-1", "source-2")))
        assert sorted(results) == ["created", "duplicate"]
        assert first.connection.execute(
            "SELECT COUNT(*) FROM workflow_source_subscriptions WHERE status = 'ACTIVE'"
        ).fetchone()[0] == 1
    finally:
        first.close()
        if not shared_store:
            second.close()


def test_member_authorized_discovery_is_idempotent_private_and_creates_linked_draft(tmp_path):
    cp, service, reader, identities = setup(tmp_path)
    with pytest.raises(PermissionError, match="missing chat-read"):
        service.subscribe(identities["no_scope"], "oc_123456789", "GROUP", "测试群", True,
                         WORKFLOW_SOURCE_CONSENT_VERSION)

    subscription = service.subscribe(identities["member"], "oc_123456789", "GROUP", "测试群", True,
                                     WORKFLOW_SOURCE_CONSENT_VERSION)
    scanned = service.scan_recent(identities["member"], subscription["subscription_id"])
    assert scanned["messages_scanned"] == 3
    assert scanned["raw_message_content_retained"] is False
    assert len(scanned["opportunity_ids"]) == 1
    opportunity_id = scanned["opportunity_ids"][0]

    service.scan_recent(identities["member"], subscription["subscription_id"])
    opportunity = service.list_opportunities(identities["member"])[0]
    assert opportunity["occurrence_count"] == 2
    assert opportunity["source_label"] == "测试群"
    assert opportunity["pattern_type"] == "EXACT_NORMALIZED_TEXT_REPEAT"
    assert service.list_subscriptions(identities["member"])[0]["consent_version"] == "workflow-source-v1"
    assert len(opportunity["evidence_refs"]) == 2
    assert service.list_opportunities(identities["other"]) == []
    assert reader.calls[0][2] == "GROUP"

    persisted = "\n".join(cp.store.connection.iterdump())
    assert CHAT_TEXT not in persisted
    assert "message-one" not in persisted
    assert "message-two" not in persisted

    with pytest.raises(ValueError, match="only an accepted"):
        service.create_agent_draft(identities["member"], opportunity_id,
                                   "课程资料 Agent", "整理课程材料并输出待确认事项")
    service.decide(identities["member"], opportunity_id, "ACCEPTED", "该流程每周都会重复")
    draft = service.create_agent_draft(identities["member"], opportunity_id,
                                       "课程资料 Agent", "整理课程材料并输出待确认事项")
    row = cp.store.get_agent_config_draft(draft["draft_id"], "tenant")
    config = json.loads(row["data_json"])
    assert row["status"] == "PENDING_REVIEW"
    assert config["workflow_opportunity_id"] == opportunity_id
    assert config["workflow_occurrence_count"] == 2
    assert config["requested_tool_policy"] == {"allowed_tools": []}
    assert "message-one" not in json.dumps(config)


def test_subscription_owner_revocation_blocks_scan_but_keeps_review_access(tmp_path):
    _, service, reader, identities = setup(tmp_path)
    subscription = service.subscribe(identities["member"], "oc_123456789", "GROUP", "测试群", True,
                                     WORKFLOW_SOURCE_CONSENT_VERSION)
    opportunity_id = service.scan_recent(identities["member"], subscription["subscription_id"])["opportunity_ids"][0]
    with pytest.raises(PermissionError):
        service.scan_recent(identities["other"], subscription["subscription_id"])
    service.revoke(identities["member"], subscription["subscription_id"])
    assert service.list_subscriptions(identities["member"])[0]["status"] == "REVOKED"
    opportunity = service.list_opportunities(identities["member"])[0]
    assert opportunity["opportunity_id"] == opportunity_id
    assert opportunity["status"] == "SOURCE_REVOKED"
    with pytest.raises(ValueError, match="only an accepted"):
        service.create_agent_draft(identities["member"], opportunity_id, "课程 Agent", "只做整理")
    with pytest.raises(PermissionError, match="active member chat subscription"):
        service.scan_recent(identities["member"], subscription["subscription_id"])
    assert len(reader.calls) == 1


def test_revocation_during_chat_read_prevents_persisting_scan_results(tmp_path):
    cp, service, reader, identities = setup(tmp_path)
    subscription = service.subscribe(identities["member"], "oc_123456789", "GROUP", "测试群", True,
                                     WORKFLOW_SOURCE_CONSENT_VERSION)
    original_reader = reader.list_messages

    def read_then_revoke(identity, chat_id, chat_type, start_at, end_at):
        messages = original_reader(identity, chat_id, chat_type, start_at, end_at)
        service.revoke(identity, subscription["subscription_id"])
        return messages

    reader.list_messages = read_then_revoke
    with pytest.raises(ValueError, match="no longer active"):
        service.scan_recent(identities["member"], subscription["subscription_id"])

    assert service.list_opportunities(identities["member"]) == []
    assert service.list_subscriptions(identities["member"])[0]["status"] == "REVOKED"
    audit_kinds = [row["event_type"] for row in cp.store.list_tenant_audits("tenant", limit=20)]
    assert "WORKFLOW_DISCOVERY_SCAN" not in audit_kinds


def test_scan_rolls_back_all_candidates_when_later_write_fails(tmp_path):
    cp, service, reader, identities = setup(tmp_path)
    subscription = service.subscribe(identities["member"], "oc_123456789", "GROUP", "测试群", True,
                                     WORKFLOW_SOURCE_CONSENT_VERSION)
    reader.messages = [
        ChatMessage("first-one", NOW - timedelta(hours=4), CHAT_TEXT),
        ChatMessage("first-two", NOW - timedelta(hours=3), CHAT_TEXT),
        ChatMessage("second-one", NOW - timedelta(hours=2), "请每周整理一次教学反馈并交给老师确认"),
        ChatMessage("second-two", NOW - timedelta(hours=1), "请每周整理一次教学反馈并交给老师确认"),
    ]
    cp.store.connection.execute(
        "CREATE TRIGGER fail_second_workflow_candidate BEFORE INSERT ON workflow_opportunities "
        "WHEN (SELECT COUNT(*) FROM workflow_opportunities) > 0 "
        "BEGIN SELECT RAISE(ABORT, 'synthetic second write failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic second write failure"):
        service.scan_recent(identities["member"], subscription["subscription_id"])

    assert cp.store.connection.execute("SELECT COUNT(*) FROM workflow_opportunities").fetchone()[0] == 0
    assert cp.store.connection.execute("SELECT COUNT(*) FROM workflow_opportunity_evidence").fetchone()[0] == 0


def test_scan_rechecks_consent_after_chat_read_even_when_no_candidates(tmp_path):
    cp, service, reader, identities = setup(tmp_path)
    subscription = service.subscribe(identities["member"], "oc_123456789", "GROUP", "测试群", True,
                                     WORKFLOW_SOURCE_CONSENT_VERSION)

    def read_then_invalidate_consent(*args):
        cp.store.connection.execute(
            "UPDATE workflow_source_subscriptions SET consent_version = 'workflow-source-v0' "
            "WHERE subscription_id = ?", (subscription["subscription_id"],),
        )
        return []

    reader.list_messages = read_then_invalidate_consent
    with pytest.raises(ValueError, match="no longer active"):
        service.scan_recent(identities["member"], subscription["subscription_id"])
    assert cp.store.connection.execute("SELECT COUNT(*) FROM workflow_opportunities").fetchone()[0] == 0
    assert not any(event["event_type"] == "WORKFLOW_DISCOVERY_SCAN"
                   for event in cp.store.list_tenant_audits("tenant", limit=5))


def test_failed_scan_is_audited_without_persisting_reader_error_text(tmp_path):
    cp, service, reader, identities = setup(tmp_path)
    subscription = service.subscribe(identities["member"], "oc_123456789", "GROUP", "测试群", True,
                                     WORKFLOW_SOURCE_CONSENT_VERSION)

    def fail_with_sensitive_detail(*args):
        raise RuntimeError("Bearer secret-token; response contained private text")

    reader.list_messages = fail_with_sensitive_detail
    with pytest.raises(RuntimeError, match="^FEISHU_CHAT_READ_FAILED$"):
        service.scan_recent(identities["member"], subscription["subscription_id"])

    audit = cp.store.list_tenant_audits("tenant", limit=1)[0]
    assert audit["event_type"] == "WORKFLOW_DISCOVERY_SCAN_FAILED"
    assert audit["outcome"] == "FAILED"
    persisted = "\n".join(cp.store.connection.iterdump())
    assert "secret-token" not in persisted
    assert "private text" not in persisted


@pytest.mark.parametrize("token_state", ["missing", "expired"])
def test_scan_with_expired_or_missing_member_token_is_blocked_and_audited(tmp_path, token_state):
    cp, service, _, identities = setup(tmp_path)
    subscription = service.subscribe(identities["member"], "oc_123456789", "GROUP", "测试群", True,
                                     WORKFLOW_SOURCE_CONSENT_VERSION)
    clock = [NOW.timestamp()]
    vault = UserTokenVault(cp.store, "awtrY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=",
                           clock=lambda: clock[0])
    if token_state == "expired":
        vault.replace(identities["member"], OAuthGrant("expired-token", CHAT_GROUP_READ_SCOPE, 60))
        clock[0] += 31

    def unexpected_request(request):
        raise AssertionError("Feishu API must not be called without a valid member token")

    client = httpx.Client(transport=httpx.MockTransport(unexpected_request))
    service.reader = FeishuUserChatHistoryReader(vault, client)
    with pytest.raises(UserDocumentAuthorizationRequired):
        service.scan_recent(identities["member"], subscription["subscription_id"])

    audit = cp.store.list_tenant_audits("tenant", limit=1)[0]
    assert audit["event_type"] == "WORKFLOW_DISCOVERY_SCAN_BLOCKED"
    assert audit["outcome"] == "BLOCKED"
    client.close()


def test_outdated_source_consent_blocks_scan_until_member_renews_consent(tmp_path):
    cp, service, reader, identities = setup(tmp_path)
    subscription = service.subscribe(identities["member"], "oc_123456789", "GROUP", "测试群", True,
                                     WORKFLOW_SOURCE_CONSENT_VERSION)
    cp.store.connection.execute(
        "UPDATE workflow_source_subscriptions SET consent_version = 'workflow-source-v0' "
        "WHERE subscription_id = ?", (subscription["subscription_id"],),
    )

    with pytest.raises(ValueError, match="读取说明已更新"):
        service.scan_recent(identities["member"], subscription["subscription_id"])

    assert reader.calls == []
    audit = cp.store.list_tenant_audits("tenant", limit=1)[0]
    assert audit["event_type"] == "WORKFLOW_DISCOVERY_SCAN_BLOCKED"
    assert audit["outcome"] == "BLOCKED"

    renewed = service.renew_consent(identities["member"], subscription["subscription_id"],
                                    True, WORKFLOW_SOURCE_CONSENT_VERSION)
    assert renewed["subscription_id"] == subscription["subscription_id"]
    assert renewed["status"] == "ACTIVE"
    assert renewed["consent_version"] == WORKFLOW_SOURCE_CONSENT_VERSION
    with pytest.raises(ValueError, match="active chat subscription"):
        service.renew_consent(identities["other"], subscription["subscription_id"],
                              True, WORKFLOW_SOURCE_CONSENT_VERSION)
    assert service.scan_recent(identities["member"], subscription["subscription_id"])["messages_scanned"] == 3
    renewal_audit = next(
        event for event in cp.store.list_tenant_audits("tenant", limit=5)
        if event["event_type"] == "WORKFLOW_SOURCE_CONSENT_RENEWED"
    )
    assert renewal_audit["outcome"] == "ACTIVE"
    assert "oc_123456789" not in json.dumps(renewal_audit)

    service.revoke(identities["member"], subscription["subscription_id"])
    with pytest.raises(ValueError, match="active chat subscription"):
        service.renew_consent(identities["member"], subscription["subscription_id"],
                              True, WORKFLOW_SOURCE_CONSENT_VERSION)
    with pytest.raises(ValueError, match="explicit workflow source consent"):
        service.renew_consent(identities["member"], "missing", False,
                              WORKFLOW_SOURCE_CONSENT_VERSION)
    with pytest.raises(ValueError, match="reload the page"):
        service.renew_consent(identities["member"], "missing", True, "stale-version")


def test_subscription_requires_scope_for_specific_chat_type(tmp_path):
    _, service, _, identities = setup(tmp_path)
    with pytest.raises(PermissionError, match="missing chat-read"):
        service.subscribe(identities["p2p"], "oc_123456789", "GROUP", "群聊", True,
                         WORKFLOW_SOURCE_CONSENT_VERSION)
    assert service.subscribe(identities["p2p"], "oc_987654321", "P2P", "私聊", True,
                             WORKFLOW_SOURCE_CONSENT_VERSION)["status"] == "ACTIVE"


def test_single_occurrence_accumulates_across_days(tmp_path):
    _, service, reader, identities = setup(tmp_path)
    subscription = service.subscribe(identities["member"], "oc_123456789", "GROUP", "测试群", True,
                                     WORKFLOW_SOURCE_CONSENT_VERSION)
    reader.messages = [ChatMessage("message-day-one", NOW - timedelta(hours=2), CHAT_TEXT)]
    assert service.scan_recent(identities["member"], subscription["subscription_id"])["opportunity_ids"] == []
    assert service.list_opportunities(identities["member"]) == []

    reader.messages = [ChatMessage("message-day-two", NOW - timedelta(hours=1), CHAT_TEXT)]
    result = service.scan_recent(identities["member"], subscription["subscription_id"])
    assert len(result["opportunity_ids"]) == 1
    assert service.list_opportunities(identities["member"])[0]["occurrence_count"] == 2


@pytest.mark.parametrize("chat_type,scope", [
    ("GROUP", CHAT_GROUP_READ_SCOPE),
    ("P2P", CHAT_P2P_READ_SCOPE),
])
def test_feishu_reader_uses_correct_member_scope_and_parses_only_text_messages(
        tmp_path, chat_type, scope):
    store = SQLiteStore(tmp_path / "reader.sqlite3")
    vault = UserTokenVault(store, "awtrY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=")
    subject = hashlib.sha256(b"reader-member").hexdigest()
    identity = IdentityContext("tenant", "member", "user_oauth", subject,
                               frozenset({scope}))
    vault.replace(identity, OAuthGrant("member-chat-token", scope, 3600))
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={
            "code": 0,
            "data": {"items": [
                {"message_id": "msg-opaque", "create_time": str(int(NOW.timestamp() * 1000)),
                 "msg_type": "text", "body": {"content": json.dumps({"text": CHAT_TEXT})}},
                {"message_id": "msg-image", "create_time": str(int(NOW.timestamp() * 1000)),
                 "msg_type": "image", "body": {"content": "{}"}},
            ], "has_more": False},
        })

    client = httpx.Client(transport=httpx.MockTransport(handle))
    reader = FeishuUserChatHistoryReader(vault, client, api_origin="https://open.feishu.cn")
    messages = reader.list_messages(identity, "oc_123456789", chat_type,
                                    NOW - timedelta(days=1), NOW)
    assert [(item.message_id, item.text) for item in messages] == [("msg-opaque", CHAT_TEXT)]
    assert seen[0].url.path == "/open-apis/im/v1/messages"
    assert seen[0].headers["Authorization"] == "Bearer member-chat-token"
    assert seen[0].url.params["container_id"] == "oc_123456789"
    assert seen[0].url.params["container_id_type"] == "chat"
    client.close()


def test_feishu_reader_converts_transport_errors_to_safe_codes(tmp_path):
    store = SQLiteStore(tmp_path / "reader-timeout.sqlite3")
    vault = UserTokenVault(store, "awtrY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=")
    subject = hashlib.sha256(b"reader-timeout-member").hexdigest()
    identity = IdentityContext("tenant", "member", "user_oauth", subject,
                               frozenset({CHAT_GROUP_READ_SCOPE}))
    vault.replace(identity, OAuthGrant("member-chat-token", CHAT_GROUP_READ_SCOPE, 3600))

    def timeout(request):
        raise httpx.ReadTimeout("authorization=secret-token", request=request)

    client = httpx.Client(transport=httpx.MockTransport(timeout))
    reader = FeishuUserChatHistoryReader(vault, client)
    with pytest.raises(RuntimeError, match="^FEISHU_CHAT_READ_TRANSPORT_ERROR$"):
        reader.list_messages(identity, "oc_123456789", "GROUP", NOW - timedelta(days=1), NOW)
    client.close()


@pytest.mark.parametrize("payload", [
    [],
    {"code": 0, "data": []},
    {"code": 0, "data": {"items": "not-a-list"}},
])
def test_feishu_reader_rejects_malformed_response_shapes(tmp_path, payload):
    store = SQLiteStore(tmp_path / "reader-malformed.sqlite3")
    vault = UserTokenVault(store, "awtrY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=")
    subject = hashlib.sha256(b"reader-malformed-member").hexdigest()
    identity = IdentityContext("tenant", "member", "user_oauth", subject,
                               frozenset({CHAT_GROUP_READ_SCOPE}))
    vault.replace(identity, OAuthGrant("member-chat-token", CHAT_GROUP_READ_SCOPE, 3600))
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload),
    ))
    reader = FeishuUserChatHistoryReader(vault, client)

    with pytest.raises(RuntimeError, match="^FEISHU_CHAT_READ_INVALID_RESPONSE$"):
        reader.list_messages(identity, "oc_123456789", "GROUP", NOW - timedelta(days=1), NOW)
    client.close()


def test_authenticated_api_runs_discovery_and_returns_agent_draft_link(tmp_path):
    cp, service, _, identities = setup(tmp_path)
    sessions = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant")
    app = FastAPI()
    app.include_router(build_workflow_discovery_router(
        service, sessions, public_origin="https://example.test",
    ))
    client = TestClient(app)
    assert client.get("/api/workflow-opportunities/authorization").status_code == 401
    session = sessions(identities["member"])
    client.cookies.set("fde_auth_session", session)
    headers = {"Origin": "https://example.test", "X-CSRF-Token": csrf_token(session)}
    assert client.get("/api/workflow-opportunities/subscriptions").json()["csrf_token"] == csrf_token(session)
    vault = UserTokenVault(cp.store, "awtrY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=",
                          clock=lambda: NOW.timestamp())
    vault.replace(identities["member"], OAuthGrant("member-chat-token", CHAT_GROUP_READ_SCOPE, 3600))
    service.token_vault = vault
    auth_status = client.get("/api/workflow-opportunities/authorization").json()["chat_read"]
    assert auth_status == {"group_chat": True, "p2p_chat": False}

    subscribe_payload = {"chat_id": "oc_123456789", "chat_type": "GROUP",
                         "display_label": "样例课程群",
                         "consent_version": WORKFLOW_SOURCE_CONSENT_VERSION}
    assert client.post("/api/workflow-opportunities/subscriptions", headers=headers,
                       json=subscribe_payload).status_code == 422
    assert client.post("/api/workflow-opportunities/subscriptions", headers=headers,
                       json={**subscribe_payload, "consent_confirmed": False}).status_code == 409
    assert client.post("/api/workflow-opportunities/subscriptions", headers=headers,
                       json={**subscribe_payload, "consent_confirmed": True,
                             "consent_version": "stale-version"}).status_code == 409
    subscribed = client.post("/api/workflow-opportunities/subscriptions", headers=headers,
                             json={**subscribe_payload, "consent_confirmed": True})
    assert subscribed.status_code == 200
    subscription_id = subscribed.json()["subscription_id"]
    scan = client.post(f"/api/workflow-opportunities/subscriptions/{subscription_id}/scan",
                       headers=headers)
    assert scan.status_code == 200
    opportunity_id = scan.json()["opportunity_ids"][0]
    opportunity = client.get("/api/workflow-opportunities").json()["opportunities"][0]
    assert opportunity["source_label"] == "样例课程群"

    decided = client.post(f"/api/workflow-opportunities/{opportunity_id}/decision",
                          headers=headers,
                          json={"decision": "ACCEPTED", "reason": "这项整理工作重复发生"})
    assert decided.status_code == 200
    draft = client.post(f"/api/workflow-opportunities/{opportunity_id}/agent-draft",
                        headers=headers,
                        json={"name": "课程资料整理 Agent",
                              "description": "整理课程资料并列出待确认事项"})
    assert draft.status_code == 200
    stored = cp.store.get_agent_config_draft(draft.json()["draft_id"], "tenant")
    assert json.loads(stored["data_json"])["workflow_opportunity_id"] == opportunity_id
    assert client.post(f"/api/workflow-opportunities/subscriptions/{subscription_id}/scan",
                       headers={"Origin": "https://example.test"}).status_code == 403
    cp.store.connection.execute(
        "UPDATE workflow_source_subscriptions SET consent_version = 'workflow-source-v0' "
        "WHERE subscription_id = ?", (subscription_id,),
    )
    outdated_scan = client.post(
        f"/api/workflow-opportunities/subscriptions/{subscription_id}/scan", headers=headers,
    )
    assert outdated_scan.status_code == 409
    assert outdated_scan.json()["detail"] == "读取说明已更新，请在原订阅中重新确认后再扫描。"
    consent_url = f"/api/workflow-opportunities/subscriptions/{subscription_id}/consent"
    assert client.post(consent_url, headers=headers,
                       json={"consent_confirmed": False,
                             "consent_version": WORKFLOW_SOURCE_CONSENT_VERSION}).status_code == 409
    assert client.post(consent_url, headers=headers,
                       json={"consent_confirmed": True,
                             "consent_version": "stale-version"}).status_code == 409
    assert client.post(consent_url, headers={"Origin": "https://example.test"},
                       json={"consent_confirmed": True,
                             "consent_version": WORKFLOW_SOURCE_CONSENT_VERSION}).status_code == 403
    renewed = client.post(consent_url, headers=headers,
                          json={"consent_confirmed": True,
                                "consent_version": WORKFLOW_SOURCE_CONSENT_VERSION})
    assert renewed.status_code == 200
    assert renewed.json()["subscription_id"] == subscription_id
    assert renewed.json()["consent_version"] == WORKFLOW_SOURCE_CONSENT_VERSION
    assert client.post(f"/api/workflow-opportunities/subscriptions/{subscription_id}/scan",
                       headers=headers).status_code == 200
