import json
import runpy
from pathlib import Path

from fastapi.testclient import TestClient

import fde_control_plane
from fde_control_plane.feishu_oauth import FeishuOAuthTransport


def test_production_oauth_client_accepts_user_token_and_h5_requires_session(tmp_path, monkeypatch):
    import lark_oapi as lark
    from lark_oapi.core.http import Transport
    from lark_oapi.core.model import RawResponse

    config = {
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "test-secret",
        "FEISHU_TENANT_KEY": "tenant-test",
        "FDE_OAUTH_REDIRECT_URI": "https://example.test/oauth/callback",
        "FDE_DB_PATH": str(tmp_path / "server.sqlite3"),
        "FDE_PREBOUND_OPEN_ID_HASH": "",
    }
    for name, value in config.items():
        monkeypatch.setenv(name, value)

    transports = []

    def capture_transport(client, *, redirect_uri):
        transport = FeishuOAuthTransport(client, redirect_uri=redirect_uri)
        transports.append(transport)
        return transport

    monkeypatch.setattr(fde_control_plane, "FeishuOAuthTransport", capture_transport)
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "fde_oauth_server.py"))
    calls = []

    def fake_execute(config, request, option):
        calls.append((request.uri, request.token_types, option.user_access_token))
        response = RawResponse()
        response.status_code = 200
        response.content = json.dumps({
            "code": 0,
            "data": {"open_id": "ou_test", "tenant_key": "tenant-test"},
        }).encode()
        return response

    # Keep SDK verification real; replace only the network transport.
    monkeypatch.setattr(Transport, "execute", fake_execute)
    user = transports[0].get_user("test-user-token")
    assert user.open_id == "ou_test"
    assert user.tenant_key == "tenant-test"
    assert calls == [(
        "/open-apis/authen/v1/user_info", {lark.AccessTokenType.USER}, "test-user-token",
    )]
    response = TestClient(namespace["app"]).get("/h5", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/oauth/start"


def test_prebound_admin_hash_assigns_explicit_admin_role(tmp_path, monkeypatch):
    config = {
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "test-secret",
        "FEISHU_TENANT_KEY": "tenant-test",
        "FDE_OAUTH_REDIRECT_URI": "https://example.test/oauth/callback",
        "FDE_DB_PATH": str(tmp_path / "server.sqlite3"),
        "FDE_PREBOUND_OPEN_ID_HASH": "a" * 64,
        "FDE_ADMIN_OPEN_ID_HASH": "a" * 64,
    }
    for name, value in config.items():
        monkeypatch.setenv(name, value)

    namespace = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "fde_oauth_server.py"))
    import sqlite3
    with sqlite3.connect(config["FDE_DB_PATH"]) as connection:
        roles_json = connection.execute(
            "SELECT roles_json FROM actors WHERE actor_id = 'member-primary'"
        ).fetchone()[0]
    assert "admin" in json.loads(roles_json)
