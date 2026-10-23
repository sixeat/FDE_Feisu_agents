"""Loopback-only, synthetic Agent lifecycle workspace with no Feishu client."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from fde_control_plane import (Actor, ActorType, AgentOrchestrator, AssistantBinding,
                               ControlPlane, FeishuMeetingReviewGateway, IdentityContext,
                               SQLiteOAuthSessionIssuer, SQLiteStore)
from fde_control_plane.meeting_review_web import MeetingReviewWebService, build_meeting_review_router
from fde_control_plane.runtime import RunEvent, RunRequest, RuntimeResult


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "fde_control_plane" / "static"
TENANT = "local-demo"
ROLES = {"member": frozenset(), "reviewer": frozenset({"agent_reviewer"}),
         "admin": frozenset({"admin"})}


class SyntheticRuntime:
    def run_once(self, request: RunRequest) -> RuntimeResult:
        sample = request.input_ref
        if request.tool_policy_snapshot != {"allowed_tools": []} or not isinstance(sample, dict):
            raise ValueError("local demo accepts only tool-free samples")
        answer = (f"v{sample['version']}：建议先核对职责和资料，再由成员确认。"
                  if sample["case_id"] == "read_only" else
                  f"v{sample['version']}：不会直接改写飞书任务；须由人审核批准。")
        return RuntimeResult("SUCCEEDED", {
            "schema_version": "agent-sample.v1", "case_id": sample["case_id"],
            "response": answer,
        }, None, events=(RunEvent(request.task_run_id, request.trace_id, 1,
                                  "completed", datetime.now(timezone.utc).isoformat(),
                                  "synthetic sample"),))


class DisabledMeetingRuntime:
    def run_once(self, request: RunRequest) -> RuntimeResult:
        raise RuntimeError("meeting execution is disabled in the local lifecycle demo")


class DisabledDocumentReader:
    def get_revision(self, document_id: str) -> str:
        raise RuntimeError("Feishu document access is disabled in the local lifecycle demo")


def create_app(db_path: Path | None = None, *, port: int = 8212) -> FastAPI:
    path = db_path or ROOT / "deploy" / "data" / "local-lifecycle-demo.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    cp = ControlPlane(SQLiteStore(path))
    if cp.store.get_actor("local-assistant") is None:
        cp.register_actor(Actor("local-assistant", TENANT, ActorType.PERSONAL_ASSISTANT,
                                frozenset({"doc.read"})))
    for role, roles in ROLES.items():
        if cp.store.get_actor(role) is None:
            cp.register_actor(Actor(role, TENANT, ActorType.USER, frozenset({"doc.read"}),
                                    roles=roles, external_ref_hash=f"local-demo-{role}"))
        if cp.store.get_binding(role, TENANT) is None:
            cp.bind_assistant(AssistantBinding(f"local-binding-{role}", TENANT, role,
                                               "local-assistant"))
    sessions = SQLiteOAuthSessionIssuer(cp, tenant_id=TENANT, ttl_seconds=86400)
    gateway = FeishuMeetingReviewGateway(cp, AgentOrchestrator(cp, DisabledMeetingRuntime()),
                                         DisabledDocumentReader())
    service = MeetingReviewWebService(gateway, sample_runtime=SyntheticRuntime())
    app = FastAPI(title="Local Agent Lifecycle Demo")
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    app.include_router(build_meeting_review_router(
        service, sessions, public_origin=f"http://127.0.0.1:{port}",
    ))
    app.state.control_plane = cp
    app.state.sessions = sessions

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if (request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}
                or request.url.hostname not in {"127.0.0.1", "testserver"}):
            return JSONResponse({"detail": "local demo accepts loopback clients only"}, status_code=403)
        if request.method not in {"GET", "HEAD"} and request.url.path.startswith("/api/meetings/"):
            allowed = ("/api/meetings/agents/", "/api/meetings/agent-drafts",
                       "/api/meetings/agent-evaluation-datasets", "/api/meetings/logout")
            if not request.url.path.startswith(allowed):
                return JSONResponse({"detail": "this demo runs Agent governance only"}, status_code=404)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "synthetic-local-demo"}

    @app.get("/local-demo", response_class=HTMLResponse)
    def choose_role(request: Request):
        identity = sessions.resolve(request.cookies.get("fde_auth_session", ""))
        current = identity.actor_id if identity is not None else "none"
        buttons = "".join(
            f'<form method="post" action="/local-demo/login/{role}">'
            f'<button type="submit">{label}</button></form>'
            for role, label in (("member", "普通成员"), ("reviewer", "独立审核官"),
                                ("admin", "管理员"))
        )
        return HTMLResponse(
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>本地 Agent 生命周期</title>"
            "<style>body{font:16px/1.6 system-ui;max-width:640px;margin:8vh auto;padding:24px;color:#18242a}"
            "button{padding:10px 16px;margin:6px 0;cursor:pointer}form{display:inline-block;margin-right:12px}"
            "</style><h1>本地 Agent 生命周期</h1>"
            "<p>虚构身份与固定样例回答；不连接飞书，也不执行远端写入。</p>"
            f"<p>当前身份：<strong>{current}</strong></p>{buttons}"
            "<p><a href='/local-demo/h5#agents'>进入 Agent 工作台</a></p></html>",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/local-demo/login/{role}")
    def login(role: str, request: Request):
        if role not in ROLES:
            raise HTTPException(404, "unknown demo role")
        origin = request.headers.get("origin")
        if origin is not None and origin != f"http://127.0.0.1:{port}":
            raise HTTPException(403, "invalid local origin")
        identity = IdentityContext(TENANT, role, "user_oauth", f"local-demo-{role}")
        response = RedirectResponse("/local-demo/h5#agents", status_code=303)
        response.set_cookie("fde_auth_session", sessions(identity), max_age=86400,
                            httponly=True, secure=False, samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/oauth/start")
    def oauth_start():
        return RedirectResponse("/local-demo", status_code=303)

    @app.get("/h5")
    def workspace(request: Request):
        if sessions.resolve(request.cookies.get("fde_auth_session", "")) is None:
            return RedirectResponse("/local-demo", status_code=303)
        return RedirectResponse("/local-demo/h5#agents", status_code=303)

    @app.get("/local-demo/h5", response_class=HTMLResponse)
    def demo_workspace(request: Request):
        if sessions.resolve(request.cookies.get("fde_auth_session", "")) is None:
            return RedirectResponse("/local-demo", status_code=303)
        html = (STATIC / "meeting_review.html").read_text(encoding="utf-8")
        script = '<script src="/assets/meeting_review.js" defer></script>'
        if script not in html:
            raise RuntimeError("Agent workspace script is missing")
        html = html.replace(script, '<link rel="stylesheet" href="/assets/local_demo.css">'
                            '<script src="/assets/local_demo_ui.js" defer></script>' + script, 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/")
    def home():
        return RedirectResponse("/local-demo", status_code=303)

    return app


app = create_app(port=int(os.environ.get("FDE_LOCAL_DEMO_PORT", "8212")))
