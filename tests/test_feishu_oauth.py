import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fde_control_plane import (
    Actor, ActorType, ControlPlane, FeishuOAuthIdentityGateway, FeishuOAuthWebFlow,
    OAuthWebConfig, SQLiteStore,
)
from fde_control_plane.feishu_oauth import OAuthGrant, OAuthUser
from fde_control_plane.models import IdentityContext


class FakeOAuthTransport:
    def __init__(self, *, tenant_key="tenant-key", open_id="ou_member"):
        self.tenant_key = tenant_key
        self.open_id = open_id
        self.codes = []
        self.tokens = []

    def exchange_code(self, code):
        self.codes.append(code)
        return OAuthGrant("sensitive-user-token", "authen:user_info:read docx:document:readonly")

    def get_user(self, access_token):
        self.tokens.append(access_token)
        return OAuthUser(self.open_id, self.tenant_key)


def setup_oauth(tmp_path, *, now=None, transport=None):
    cp = ControlPlane(SQLiteStore(tmp_path / "oauth.sqlite3"))
    open_hash = hashlib.sha256(b"ou_member").hexdigest()
    cp.register_actor(Actor("member", "tenant", ActorType.USER, external_ref_hash=open_hash))
    transport = transport or FakeOAuthTransport()
    clock = (lambda: now[0]) if now is not None else (lambda: 1000)
    gateway = FeishuOAuthIdentityGateway(
        cp, transport, tenant_id="tenant", expected_tenant_key="tenant-key", clock=clock,
    )
    return cp, gateway, transport


def test_oauth_state_is_one_time_and_maps_only_bound_member(tmp_path):
    cp, gateway, transport = setup_oauth(tmp_path)
    state = gateway.issue_state(browser_session_ref="browser-1")
    stored = cp.store.connection.execute("SELECT state_hash FROM oauth_states").fetchone()
    assert stored["state_hash"] == hashlib.sha256(state.encode()).hexdigest()
    assert state != stored["state_hash"]
    identity = gateway.complete(state=state, code="one-use-code", browser_session_ref="browser-1")
    assert identity.actor_id == "member"
    assert identity.auth_mode == "user_oauth"
    assert identity.subject_ref_hash == hashlib.sha256(b"ou_member").hexdigest()
    assert "docx:document:readonly" in identity.scopes
    assert transport.codes == ["one-use-code"]
    assert transport.tokens == ["sensitive-user-token"]
    with pytest.raises(PermissionError, match="consumed"):
        gateway.complete(state=state, code="replayed-code", browser_session_ref="browser-1")
    assert transport.codes == ["one-use-code"]
    assert "sensitive-user-token" not in str(cp.store.list_audits())


def test_expired_state_fails_before_code_exchange(tmp_path):
    now = [1000]
    _, gateway, transport = setup_oauth(tmp_path, now=now)
    state = gateway.issue_state(browser_session_ref="browser-1")
    now[0] = 1301
    with pytest.raises(PermissionError, match="expired"):
        gateway.complete(state=state, code="code", browser_session_ref="browser-1")
    assert transport.codes == []


@pytest.mark.parametrize(
    ("transport", "error"),
    [
        (FakeOAuthTransport(tenant_key="other-tenant"), "another tenant"),
        (FakeOAuthTransport(open_id="ou_unknown"), "not bound"),
    ],
)
def test_oauth_rejects_foreign_or_unbound_user(tmp_path, transport, error):
    cp, gateway, _ = setup_oauth(tmp_path, transport=transport)
    state = gateway.issue_state(browser_session_ref="browser-1")
    with pytest.raises(PermissionError, match=error):
        gateway.complete(state=state, code="code", browser_session_ref="browser-1")
    pending = cp.store.list_pending_member_access("tenant")
    assert len(pending) == (1 if transport.tenant_key == "tenant-key" else 0)


def test_unknown_member_requests_access_once_then_logs_in_after_admin_binding(tmp_path):
    transport = FakeOAuthTransport(open_id="ou_second")
    cp, gateway, _ = setup_oauth(tmp_path, transport=transport)
    for _ in range(2):
        state = gateway.issue_state(browser_session_ref="browser-second")
        with pytest.raises(PermissionError, match="接入申请已登记"):
            gateway.complete(state=state, code="code", browser_session_ref="browser-second")
    requests = cp.store.list_pending_member_access("tenant")
    assert len(requests) == 1
    request_id = requests[0]["request_id"]
    subject_hash = hashlib.sha256(b"ou_second").hexdigest()
    assert "ou_second" not in str(cp.store.connection.execute(
        "SELECT * FROM member_access_requests"
    ).fetchone()[:])
    approved = cp.store.approve_member_access("tenant", request_id, "member-second")
    assert approved["duplicate"] is False
    assert cp.store.approve_member_access("tenant", request_id, "ignored")["actor_id"] == "member-second"
    assert cp.store.find_actor_by_external_hash("tenant", subject_hash)["actor_id"] == "member-second"
    assert cp._actor("member-second").roles == frozenset()
    state = gateway.issue_state(browser_session_ref="browser-second")
    assert gateway.complete(state=state, code="code", browser_session_ref="browser-second").actor_id == "member-second"


def test_oauth_state_survives_control_plane_reopen(tmp_path):
    cp, gateway, _ = setup_oauth(tmp_path)
    state = gateway.issue_state(browser_session_ref="browser-1")
    cp.close()
    reopened = ControlPlane(SQLiteStore(tmp_path / "oauth.sqlite3"))
    second = FeishuOAuthIdentityGateway(
        reopened, FakeOAuthTransport(), tenant_id="tenant",
        expected_tenant_key="tenant-key", clock=lambda: 1001,
    )
    assert second.complete(state=state, code="code", browser_session_ref="browser-1").actor_id == "member"
    reopened.close()


def test_oauth_state_cannot_cross_browser_sessions(tmp_path):
    _, gateway, transport = setup_oauth(tmp_path)
    state = gateway.issue_state(browser_session_ref="browser-1")
    with pytest.raises(PermissionError, match="another session"):
        gateway.complete(state=state, code="code", browser_session_ref="browser-2")
    assert transport.codes == []
    assert gateway.complete(state=state, code="code", browser_session_ref="browser-1").actor_id == "member"


def test_oauth_gateway_requires_configured_feishu_tenant(tmp_path):
    cp, _, transport = setup_oauth(tmp_path)
    with pytest.raises(ValueError, match="tenant identity"):
        FeishuOAuthIdentityGateway(cp, transport, tenant_id="tenant", expected_tenant_key="")


def test_web_flow_builds_authorization_url_and_consumes_callback(tmp_path):
    cp, gateway, transport = setup_oauth(tmp_path)
    flow = FeishuOAuthWebFlow(
        gateway,
        OAuthWebConfig(
            app_id="cli_test",
            redirect_uri="https://example.test/oauth/callback",
            scopes=("authen:user_info:read", "docx:document:readonly"),
        ),
    )
    url = flow.authorization_url(browser_session_ref="browser-1")
    assert url.startswith("https://accounts.feishu.cn/open-apis/authen/v1/authorize?")
    assert "client_id=cli_test" in url
    assert "response_type=code" in url
    assert "redirect_uri=https%3A%2F%2Fexample.test%2Foauth%2Fcallback" in url
    assert "scope=authen%3Auser_info%3Aread+docx%3Adocument%3Areadonly" in url
    state = url.split("state=", 1)[1].split("&", 1)[0]
    assert flow.callback(state=state, code="code", browser_session_ref="browser-1").actor_id == "member"
    assert transport.codes == ["code"]


def test_web_flow_rejects_declined_or_incomplete_callback(tmp_path):
    _, gateway, transport = setup_oauth(tmp_path)
    flow = FeishuOAuthWebFlow(gateway, OAuthWebConfig("cli_test", "https://example.test/callback"))
    declined_state = flow.authorization_url(browser_session_ref="browser-1").split("state=", 1)[1]
    with pytest.raises(PermissionError, match="declined"):
        flow.callback(
            state=declined_state,
            browser_session_ref="browser-1",
            error="access_denied",
        )
    assert transport.codes == []
    missing_state = flow.authorization_url(browser_session_ref="browser-1").split("state=", 1)[1]
    with pytest.raises(PermissionError, match="did not contain a code"):
        flow.callback(state=missing_state, browser_session_ref="browser-1")
    assert transport.codes == []


def test_web_config_rejects_whitespace_scope(tmp_path):
    with pytest.raises(ValueError, match="single tokens"):
        OAuthWebConfig("cli_test", "https://example.test/callback", scopes=("bad scope",))


def test_http_router_sets_browser_cookie_redirects_and_issues_auth_session(tmp_path):
    cp, gateway, _ = setup_oauth(tmp_path)
    flow = FeishuOAuthWebFlow(gateway, OAuthWebConfig("cli_test", "https://example.test/oauth/callback"))
    issued = []

    def issue_session(identity):
        issued.append(identity)
        return "opaque-auth-session"

    from fde_control_plane import build_oauth_router
    app = FastAPI()
    app.include_router(build_oauth_router(flow, issue_session, cookie_secure=False))
    client = TestClient(app)
    start = client.get("/oauth/start", follow_redirects=False)
    assert start.status_code == 303
    assert start.headers["location"].startswith("https://accounts.feishu.cn/open-apis/authen/v1/authorize?")
    assert "fde_oauth_browser=" in start.headers["set-cookie"]
    state = start.headers["location"].split("state=", 1)[1].split("&", 1)[0]
    callback = client.get(
        f"/oauth/callback?state={state}&code=one-use-code",
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/h5"
    assert "fde_auth_session=opaque-auth-session" in callback.headers["set-cookie"]
    assert issued[0].actor_id == "member"


def test_http_router_rejects_callback_without_browser_cookie(tmp_path):
    cp, gateway, _ = setup_oauth(tmp_path)
    flow = FeishuOAuthWebFlow(gateway, OAuthWebConfig("cli_test", "https://example.test/oauth/callback"))
    from fde_control_plane import build_oauth_router
    app = FastAPI()
    app.include_router(build_oauth_router(flow, lambda _: "session", cookie_secure=False))
    response = TestClient(app).get("/oauth/callback?state=x&code=y")
    assert response.status_code == 403


def test_sqlite_session_issuer_persists_hash_only_and_supports_reopen(tmp_path):
    from fde_control_plane import SQLiteOAuthSessionIssuer

    now = [1000]
    cp, _, _ = setup_oauth(tmp_path)
    issuer = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant", ttl_seconds=60, clock=lambda: now[0])
    identity = IdentityContext("tenant", "member", "user_oauth", hashlib.sha256(b"ou_member").hexdigest(), frozenset({"scope:a"}))
    token = issuer(identity)
    row = cp.store.connection.execute("SELECT session_hash, data_json FROM web_sessions").fetchone()
    assert row["session_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row["data_json"]
    assert issuer.resolve(token) == identity
    cp.close()

    reopened = ControlPlane(SQLiteStore(tmp_path / "oauth.sqlite3"))
    reopened.register_actor(Actor("member", "tenant", ActorType.USER, external_ref_hash=hashlib.sha256(b"ou_member").hexdigest()))
    second = SQLiteOAuthSessionIssuer(reopened, tenant_id="tenant", ttl_seconds=60, clock=lambda: now[0])
    assert second.resolve(token) == identity
    second.revoke(token)
    assert second.resolve(token) is None
    reopened.close()


def test_sqlite_session_issuer_expires_and_does_not_reuse_raw_token(tmp_path):
    from fde_control_plane import SQLiteOAuthSessionIssuer

    now = [1000]
    cp, _, _ = setup_oauth(tmp_path)
    issuer = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant", ttl_seconds=10, clock=lambda: now[0])
    token = issuer(IdentityContext("tenant", "member", "user_oauth", hashlib.sha256(b"ou_member").hexdigest()))
    now[0] = 1011
    assert issuer.resolve(token) is None
    with pytest.raises(ValueError):
        SQLiteOAuthSessionIssuer(cp, tenant_id="tenant", ttl_seconds=0)
    cp.close()
