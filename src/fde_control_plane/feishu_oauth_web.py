"""Web-facing Feishu OAuth flow without exposing tokens to the web layer."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
import time
from typing import Callable, Protocol
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .feishu_oauth import FeishuOAuthIdentityGateway
from .models import ActorType, IdentityContext
from .service import ControlPlane


@dataclass(frozen=True)
class OAuthWebConfig:
    app_id: str
    redirect_uri: str
    scopes: tuple[str, ...] = ()
    authorize_url: str = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
    prompt: str | None = None

    def __post_init__(self) -> None:
        if not self.app_id or not self.redirect_uri or not self.authorize_url:
            raise ValueError("OAuth app_id, redirect_uri, and authorize_url are required")
        if any(not scope or any(ch.isspace() for ch in scope) for scope in self.scopes):
            raise ValueError("OAuth scopes must be non-empty single tokens")


class FeishuOAuthWebFlow:
    """Translate HTTP-level OAuth parameters into the identity gateway.

    The surrounding web framework owns the browser session cookie. It passes
    only an opaque, server-generated session reference here; this class never
    accepts or returns a Feishu access token.
    """

    def __init__(self, gateway: FeishuOAuthIdentityGateway, config: OAuthWebConfig) -> None:
        self.gateway = gateway
        self.config = config

    def authorization_url(self, *, browser_session_ref: str) -> str:
        state = self.gateway.issue_state(browser_session_ref=browser_session_ref)
        params: dict[str, str] = {
            "client_id": self.config.app_id,
            "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "state": state,
        }
        if self.config.scopes:
            params["scope"] = " ".join(self.config.scopes)
        if self.config.prompt:
            params["prompt"] = self.config.prompt
        return f"{self.config.authorize_url}?{urlencode(params)}"

    def callback(
        self,
        *,
        state: str,
        browser_session_ref: str,
        code: str | None = None,
        error: str | None = None,
    ) -> IdentityContext:
        if error:
            raise PermissionError(f"Feishu OAuth was declined: {error}")
        if not code:
            raise PermissionError("Feishu OAuth callback did not contain a code")
        return self.gateway.complete(
            state=state,
            code=code,
            browser_session_ref=browser_session_ref,
        )


class OAuthSessionIssuer(Protocol):
    def __call__(self, identity: IdentityContext) -> str: ...


class SQLiteOAuthSessionIssuer:
    """Issue opaque sessions while persisting only their hashes."""

    def __init__(
        self, control_plane: ControlPlane, *, tenant_id: str,
        ttl_seconds: int = 3600, clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds < 1 or not tenant_id:
            raise ValueError("tenant and positive session lifetime are required")
        self.control_plane = control_plane
        self.store = control_plane.store
        self.tenant_id = tenant_id
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def __call__(self, identity: IdentityContext) -> str:
        self._verify_member(identity)
        token = secrets.token_urlsafe(32)
        now = int(self.clock())
        payload = {
            "tenant_id": identity.tenant_id,
            "actor_id": identity.actor_id,
            "auth_mode": identity.auth_mode,
            "subject_ref_hash": identity.subject_ref_hash,
            "scopes": sorted(identity.scopes),
        }
        self.store.save_web_session(_hash_session(token), payload, now + self.ttl_seconds, now)
        return token

    def resolve(self, token: str) -> IdentityContext | None:
        if not token:
            return None
        data = self.store.get_web_session(_hash_session(token), int(self.clock()))
        if data is None:
            return None
        identity = IdentityContext(
            tenant_id=data["tenant_id"],
            actor_id=data["actor_id"],
            auth_mode=data["auth_mode"],
            subject_ref_hash=data["subject_ref_hash"],
            scopes=frozenset(data.get("scopes", [])),
        )
        try:
            self._verify_member(identity)
        except (PermissionError, KeyError):
            self.revoke(token)
            return None
        return identity

    def _verify_member(self, identity: IdentityContext) -> None:
        if identity.tenant_id != self.tenant_id or identity.auth_mode != "user_oauth":
            raise PermissionError("session requires an OAuth member of the configured tenant")
        member = self.control_plane.verify_identity(identity)
        row = self.store.find_actor_by_external_hash(identity.tenant_id, identity.subject_ref_hash)
        if member.actor_type != ActorType.USER or row is None or row["actor_id"] != identity.actor_id:
            raise PermissionError("session member no longer has a unique identity binding")

    def revoke(self, token: str) -> None:
        if token:
            self.store.revoke_web_session(_hash_session(token))


def _hash_session(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_session_dependency(
    sessions: SQLiteOAuthSessionIssuer, *, auth_cookie_name: str = "fde_auth_session",
) -> Callable[[Request], IdentityContext]:
    def require_identity(request: Request) -> IdentityContext:
        identity = sessions.resolve(request.cookies.get(auth_cookie_name, ""))
        if identity is None:
            raise HTTPException(status_code=401, detail="Login session is missing or invalid")
        return identity

    return require_identity


def build_oauth_router(
    flow: FeishuOAuthWebFlow,
    issue_session: OAuthSessionIssuer,
    *,
    success_url: str = "/h5",
    browser_cookie_name: str = "fde_oauth_browser",
    auth_cookie_name: str = "fde_auth_session",
    cookie_secure: bool = True,
    cookie_max_age: int = 300,
    auth_cookie_max_age: int = 3600,
) -> APIRouter:
    """Build the two minimal HTTP routes needed by the H5 entry point.

    `issue_session` is deliberately injected so the web adapter cannot invent
    authentication state or persist raw Feishu credentials. Production code
    should provide a durable, hashed session store; tests can use a fake.
    """
    if not browser_cookie_name or not auth_cookie_name or cookie_max_age < 1 or auth_cookie_max_age < 1:
        raise ValueError("OAuth cookie names and positive max age are required")

    router = APIRouter()

    @router.get("/oauth/start")
    def oauth_start(request: Request) -> RedirectResponse:
        browser_ref = request.cookies.get(browser_cookie_name) or secrets.token_urlsafe(32)
        response = RedirectResponse(flow.authorization_url(browser_session_ref=browser_ref), status_code=303)
        response.set_cookie(
            browser_cookie_name,
            browser_ref,
            max_age=cookie_max_age,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
        )
        return response

    @router.get("/oauth/callback")
    def oauth_callback(
        request: Request,
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        browser_ref = request.cookies.get(browser_cookie_name)
        if not browser_ref:
            raise HTTPException(status_code=403, detail="OAuth browser session is missing")
        try:
            identity = flow.callback(
                state=state or "",
                code=code,
                error=error,
                browser_session_ref=browser_ref,
            )
            session_id = issue_session(identity)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        response = RedirectResponse(success_url, status_code=303)
        response.set_cookie(
            auth_cookie_name,
            session_id,
            max_age=auth_cookie_max_age,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
        )
        response.delete_cookie(browser_cookie_name, secure=cookie_secure, httponly=True, samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return router


__all__ = [
    "FeishuOAuthWebFlow", "OAuthSessionIssuer", "OAuthWebConfig", "SQLiteOAuthSessionIssuer",
    "build_oauth_router", "build_session_dependency",
]
