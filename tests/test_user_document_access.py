import base64
import hashlib
from types import SimpleNamespace

import pytest

from fde_control_plane import Actor, ActorType, ControlPlane, IdentityContext, SQLiteStore
from fde_control_plane.feishu_oauth import FeishuOAuthIdentityGateway, OAuthGrant, OAuthUser
from fde_control_plane.user_document_access import FeishuUserDocumentReader, UserTokenVault


KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
SUBJECT = hashlib.sha256(b"ou_member").hexdigest()
SCOPES = frozenset({"authen:user_info:read", "docx:document:readonly"})


def identity():
    return IdentityContext("tenant", "member", "user_oauth", SUBJECT, SCOPES)


def test_short_lived_grant_is_encrypted_bound_and_expires(tmp_path):
    now = [1000]
    store = SQLiteStore(tmp_path / "vault.sqlite3")
    vault = UserTokenVault(store, KEY, clock=lambda: now[0])
    vault.replace(identity(), OAuthGrant("raw-sensitive-token", " ".join(SCOPES), 120))
    row = store.get_user_oauth_token("member")
    assert row["expires_at"] == 1120
    assert "raw-sensitive-token" not in row["token_ciphertext"]
    assert "raw-sensitive-token" not in "\n".join(store.connection.iterdump())
    assert vault.get(identity()) == "raw-sensitive-token"
    with pytest.raises(PermissionError):
        vault.get(IdentityContext("tenant", "member", "feishu_event", "wrong-subject"))
    with pytest.raises(PermissionError, match="member identity"):
        vault.get(IdentityContext("tenant", "member", "untrusted", SUBJECT))
    now[0] = 1091  # 30-second safety margin
    with pytest.raises(PermissionError, match="expired"):
        vault.get(identity())


def test_grant_downgrade_and_revocation_remove_old_document_access(tmp_path):
    store = SQLiteStore(tmp_path / "vault.sqlite3")
    vault = UserTokenVault(store, KEY, clock=lambda: 1000)
    vault.replace(identity(), OAuthGrant("first", " ".join(SCOPES), 3600))
    vault.replace(IdentityContext("tenant", "member", "user_oauth", SUBJECT,
                                  frozenset({"authen:user_info:read"})),
                  OAuthGrant("second", "authen:user_info:read", 3600))
    assert store.get_user_oauth_token("member") is None
    vault.replace(identity(), OAuthGrant("third", " ".join(SCOPES), 3600))
    vault.revoke("member")
    assert store.get_user_oauth_token("member") is None


def test_empty_user_token_cannot_be_stored_or_used(tmp_path):
    store = SQLiteStore(tmp_path / "vault.sqlite3")
    vault = UserTokenVault(store, KEY, clock=lambda: 1000)
    with pytest.raises(PermissionError, match="token is missing"):
        vault.replace(identity(), OAuthGrant("  ", " ".join(SCOPES), 3600))
    assert store.get_user_oauth_token("member") is None


def test_ciphertext_tampering_cannot_yield_token(tmp_path):
    store = SQLiteStore(tmp_path / "vault.sqlite3")
    vault = UserTokenVault(store, KEY, clock=lambda: 1000)
    vault.replace(identity(), OAuthGrant("secret", " ".join(SCOPES), 3600))
    store.connection.execute("UPDATE user_oauth_tokens SET token_ciphertext = ?", (base64.urlsafe_b64encode(b"x" * 40).decode(),))
    with pytest.raises(PermissionError, match="cannot be verified"):
        vault.get(identity())


def test_document_reader_uses_user_token_for_metadata_and_blocks(tmp_path):
    store = SQLiteStore(tmp_path / "vault.sqlite3")
    vault = UserTokenVault(store, KEY, clock=lambda: 1000)
    vault.replace(identity(), OAuthGrant("user-doc-token", " ".join(SCOPES), 3600))
    options = []

    class Document:
        def get(self, request, option):
            options.append(option)
            return SimpleNamespace(code=0, data=SimpleNamespace(
                document=SimpleNamespace(revision_id=5, title="会议纪要")))

    class Blocks:
        def get(self, request, option):
            options.append(option)
            element = SimpleNamespace(text_run=SimpleNamespace(content="张三负责整理文档"))
            block = SimpleNamespace(block_type=2, text=SimpleNamespace(elements=[element]))
            return SimpleNamespace(code=0, data=SimpleNamespace(items=[block], has_more=False))

    client = SimpleNamespace(config=SimpleNamespace(enable_set_token=True), docx=SimpleNamespace(v1=SimpleNamespace(
        document=Document(), document_block_children=Blocks())))
    source = FeishuUserDocumentReader(vault, client).read_document(identity(), "doc123")
    assert source["blocks"][0]["text"] == "张三负责整理文档"
    assert [option.user_access_token for option in options] == ["user-doc-token", "user-doc-token"]
    assert all(option.tenant_access_token is None for option in options)


def test_user_document_reader_rejects_sdk_that_can_fall_back_to_tenant_token(tmp_path):
    store = SQLiteStore(tmp_path / "vault.sqlite3")
    vault = UserTokenVault(store, KEY, clock=lambda: 1000)
    for client in (object(), SimpleNamespace(config=SimpleNamespace(enable_set_token=False))):
        with pytest.raises(ValueError, match="explicit access token"):
            FeishuUserDocumentReader(vault, client)


def test_user_document_403_never_retries_with_application_identity(tmp_path):
    store = SQLiteStore(tmp_path / "vault.sqlite3")
    vault = UserTokenVault(store, KEY, clock=lambda: 1000)
    vault.replace(identity(), OAuthGrant("user-doc-token", " ".join(SCOPES), 3600))
    options = []

    class Document:
        def get(self, request, option):
            options.append(option)
            return SimpleNamespace(code=403)

    client = SimpleNamespace(config=SimpleNamespace(enable_set_token=True), docx=SimpleNamespace(
        v1=SimpleNamespace(document=Document(), document_block_children=object())))
    with pytest.raises(RuntimeError, match="FEISHU_DOCUMENT_READ_403"):
        FeishuUserDocumentReader(vault, client).read_document(
            IdentityContext("tenant", "member", "feishu_event", SUBJECT), "doc123")
    assert len(options) == 1
    assert options[0].user_access_token == "user-doc-token"
    assert options[0].tenant_access_token is None


def test_oauth_callback_saves_only_member_grant_after_identity_check(tmp_path):
    store = SQLiteStore(tmp_path / "oauth.sqlite3")
    cp = ControlPlane(store)
    cp.register_actor(Actor("member", "tenant", ActorType.USER, external_ref_hash=SUBJECT))
    vault = UserTokenVault(store, KEY, clock=lambda: 1000)

    class Transport:
        def exchange_code(self, code):
            return OAuthGrant("fresh-user-token", " ".join(SCOPES), 3600)

        def get_user(self, access_token):
            assert access_token == "fresh-user-token"
            return OAuthUser("ou_member", "tenant-key", "成员")

    gateway = FeishuOAuthIdentityGateway(
        cp, Transport(), tenant_id="tenant", expected_tenant_key="tenant-key",
        clock=lambda: 1000, user_token_store=vault,
    )
    state = gateway.issue_state(browser_session_ref="browser")
    member = gateway.complete(state=state, code="code", browser_session_ref="browser")
    assert vault.get(member) == "fresh-user-token"
    assert "fresh-user-token" not in str(store.connection.execute("SELECT * FROM web_sessions").fetchall())
