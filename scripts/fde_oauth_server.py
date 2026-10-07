"""Minimal production entry point for the FDE OAuth/H5 gateway.

All secrets are read from the process environment. The app intentionally does
not auto-register Feishu users: an operator must pre-bind an open_id hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import lark_oapi as lark
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import urlsplit

from fde_control_plane.feishu_review import FeishuDocumentRevisionReader, FeishuMeetingReviewGateway
from fde_control_plane.meeting_review_web import MeetingReviewWebService, build_meeting_review_router
from fde_control_plane.orchestration import AgentOrchestrator
from fde_control_plane.runtime import HermesRuntimeAdapter

from fde_control_plane import (
    Actor,
    ActorType,
    ControlPlane,
    FeishuOAuthIdentityGateway,
    FeishuOAuthTransport,
    FeishuOAuthWebFlow,
    OAuthWebConfig,
    SQLiteOAuthSessionIssuer,
    SQLiteStore,
    build_oauth_router,
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_app() -> FastAPI:
    app_id = _required("FEISHU_APP_ID")
    app_secret = _required("FEISHU_APP_SECRET")
    tenant_key = _required("FEISHU_TENANT_KEY")
    tenant_id = os.environ.get("FDE_TENANT_ID", "sixeat")
    redirect_uri = _required("FDE_OAUTH_REDIRECT_URI")
    db_path = Path(os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    control_plane = ControlPlane(SQLiteStore(db_path))
    bound_hash = os.environ.get("FDE_PREBOUND_OPEN_ID_HASH", "").strip()
    if bound_hash and control_plane.store.get_actor("member-primary") is None:
        control_plane.register_actor(
            Actor("member-primary", tenant_id, ActorType.USER, external_ref_hash=bound_hash)
        )

    # OAuth user-info requests pass the short-lived user token explicitly.
    # lark-oapi requires manual token mode for that request path.
    client = (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .timeout(12)
        .enable_set_token(True)
        .build()
    )
    gateway = FeishuOAuthIdentityGateway(
        control_plane,
        FeishuOAuthTransport(client, redirect_uri=redirect_uri),
        tenant_id=tenant_id,
        expected_tenant_key=tenant_key,
    )
    flow = FeishuOAuthWebFlow(
        gateway,
        OAuthWebConfig(
            app_id=app_id,
            redirect_uri=redirect_uri,
            scopes=tuple(filter(None, os.environ.get("FDE_OAUTH_SCOPES", "").split())),
        ),
    )
    sessions = SQLiteOAuthSessionIssuer(control_plane, tenant_id=tenant_id)

    app = FastAPI(title="FDE Agent Control Plane")
    request_lock = asyncio.Lock()

    @app.middleware("http")
    async def serialize_database_requests(request: Request, call_next):
        if request.url.path.startswith("/assets/"):
            return await call_next(request)
        # One demo process shares one SQLite connection, including OAuth writes.
        async with request_lock:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    static_dir = Path(__file__).resolve().parents[1] / "src" / "fde_control_plane" / "static"
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")
    app.include_router(
        build_oauth_router(
            flow,
            sessions,
            success_url="/h5",
            cookie_secure=True,
        )
    )
    # This router only reviews persisted Agent output; it exposes no run or worker endpoint.
    review_gateway = FeishuMeetingReviewGateway(
        control_plane, AgentOrchestrator(control_plane, HermesRuntimeAdapter()),
        FeishuDocumentRevisionReader(client),
    )
    uri = urlsplit(redirect_uri)
    app.include_router(build_meeting_review_router(
        MeetingReviewWebService(review_gateway), sessions,
        public_origin=f"{uri.scheme}://{uri.netloc}",
    ))

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "prebound_member": bool(bound_hash), "oauth": "configured"}

    @app.get("/")
    def home():
        return RedirectResponse("/h5", status_code=303)

    @app.get("/h5")
    def h5(request: Request):
        if sessions.resolve(request.cookies.get("fde_auth_session", "")) is None:
            return RedirectResponse("/oauth/start", status_code=303)
        return FileResponse(static_dir / "meeting_review.html", headers={"Cache-Control": "no-store"})

    return app


app = create_app()
