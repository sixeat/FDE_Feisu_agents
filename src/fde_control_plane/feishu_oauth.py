"""Map a verified Feishu OAuth login to an existing control-plane member."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .models import ActorType, IdentityContext
from .service import ControlPlane


@dataclass(frozen=True)
class OAuthGrant:
    access_token: str
    scope: str = ""


@dataclass(frozen=True)
class OAuthUser:
    open_id: str
    tenant_key: str | None = None


class OAuthTransport(Protocol):
    def exchange_code(self, code: str) -> OAuthGrant: ...
    def get_user(self, access_token: str) -> OAuthUser: ...


class FeishuOAuthTransport:
    """Keep the short-lived user token inside the SDK boundary."""

    def __init__(self, client: Any, *, redirect_uri: str) -> None:
        self.client = client
        self.redirect_uri = redirect_uri

    def exchange_code(self, code: str) -> OAuthGrant:
        result = self.client.access_token.retrieve_by_authorization_code(
            code, redirect_uri=self.redirect_uri
        )
        token = getattr(result, "access_token", None)
        if not token:
            raise RuntimeError("FEISHU_OAUTH_TOKEN_MISSING")
        return OAuthGrant(str(token), str(getattr(result, "scope", "") or ""))

    def get_user(self, access_token: str) -> OAuthUser:
        import lark_oapi as lark

        request = lark.api.authen.v1.model.GetUserInfoRequest.builder().build()
        option = lark.RequestOption.builder().user_access_token(access_token).build()
        response = self.client.authen.v1.user_info.get(request, option)
        code = getattr(response, "code", None)
        if code != 0:
            raise RuntimeError(f"FEISHU_OAUTH_USER_INFO_{code if code is not None else 'UNKNOWN'}")
        data = getattr(response, "data", None)
        open_id = getattr(data, "open_id", None)
        if not open_id:
            raise RuntimeError("FEISHU_OAUTH_OPEN_ID_MISSING")
        return OAuthUser(str(open_id), getattr(data, "tenant_key", None))


class FeishuOAuthIdentityGateway:
    """Consume one-time state before exchanging a code; never auto-register users."""

    def __init__(
        self, control_plane: ControlPlane, transport: OAuthTransport, *,
        tenant_id: str, expected_tenant_key: str,
        state_ttl_seconds: int = 300, clock: Callable[[], float] = time.time,
    ) -> None:
        if state_ttl_seconds < 1 or not tenant_id or not expected_tenant_key:
            raise ValueError("tenant identity and positive state lifetime are required")
        self.control_plane = control_plane
        self.transport = transport
        self.tenant_id = tenant_id
        self.expected_tenant_key = expected_tenant_key
        self.state_ttl_seconds = state_ttl_seconds
        self.clock = clock

    def issue_state(self, *, browser_session_ref: str) -> str:
        if not browser_session_ref:
            raise ValueError("browser session reference is required")
        state = secrets.token_urlsafe(32)
        now = int(self.clock())
        self.control_plane.store.save_oauth_state(
            hashlib.sha256(state.encode("utf-8")).hexdigest(),
            hashlib.sha256(browser_session_ref.encode("utf-8")).hexdigest(),
            now + self.state_ttl_seconds, now,
        )
        return state

    def complete(self, *, state: str, code: str, browser_session_ref: str) -> IdentityContext:
        if not state or not code or not browser_session_ref:
            raise PermissionError("OAuth state, code, and browser session are required")
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        session_hash = hashlib.sha256(browser_session_ref.encode("utf-8")).hexdigest()
        if not self.control_plane.store.consume_oauth_state(state_hash, session_hash, int(self.clock())):
            raise PermissionError("OAuth state is missing, expired, consumed, or bound to another session")
        grant = self.transport.exchange_code(code)
        user = self.transport.get_user(grant.access_token)
        if user.tenant_key != self.expected_tenant_key:
            raise PermissionError("OAuth user belongs to another tenant")
        subject_hash = hashlib.sha256(user.open_id.encode("utf-8")).hexdigest()
        row = self.control_plane.store.find_actor_by_external_hash(self.tenant_id, subject_hash)
        if row is None:
            raise PermissionError("OAuth user is not bound to a unique member")
        actor = self.control_plane._actor(row["actor_id"])
        if actor.actor_type != ActorType.USER:
            raise PermissionError("OAuth identity is not a member")
        scopes = frozenset(filter(None, re.split(r"[\s,]+", grant.scope)))
        identity = IdentityContext(self.tenant_id, actor.actor_id, "user_oauth", subject_hash, scopes)
        self.control_plane.verify_identity(identity)
        return identity


__all__ = [
    "FeishuOAuthIdentityGateway", "FeishuOAuthTransport", "OAuthGrant", "OAuthTransport", "OAuthUser",
]
