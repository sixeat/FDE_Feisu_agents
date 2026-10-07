"""Loopback-only preview of review interactions using an in-memory database."""

import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from fde_control_plane import (
    Actor, ActorType, AgentOrchestrator, ControlPlane, HermesRuntimeAdapter,
    IdentityContext, SQLiteOAuthSessionIssuer, SQLiteStore,
)
from fde_control_plane.feishu_review import FeishuMeetingReviewGateway
from fde_control_plane.meeting_review_web import MeetingReviewWebService, build_meeting_review_router
from scripts.meeting_review_bundle import import_bundle

ROOT = Path(__file__).resolve().parents[1]
bundle = json.loads((ROOT / "data/meeting_review_bundle.json").read_text(encoding="utf-8"))
cp = ControlPlane(SQLiteStore())
subject = bundle["source_member_hash"]
cp.register_actor(Actor("member-primary", "preview", ActorType.USER, external_ref_hash=subject))
cp.register_actor(Actor("admin-primary", "preview", ActorType.USER, roles=frozenset({"admin"}), external_ref_hash="preview-admin-subject"))
import_bundle(cp, bundle, tenant_id="preview", member_id="member-primary", member_hash=subject)


class PreviewRevisionReader:
    def get_revision(self, document_id):
        return bundle["record"]["revision_id"]


sessions = SQLiteOAuthSessionIssuer(cp, tenant_id="preview")
gateway = FeishuMeetingReviewGateway(cp, AgentOrchestrator(cp, HermesRuntimeAdapter()), PreviewRevisionReader())
app = FastAPI()
app.include_router(build_meeting_review_router(MeetingReviewWebService(gateway), sessions, public_origin="http://127.0.0.1:8211"))
static = ROOT / "src/fde_control_plane/static"
app.mount("/assets", StaticFiles(directory=static))


@app.get("/preview-login")
def preview_login(role: str = "member"):
    if role == "admin":
        identity = IdentityContext("preview", "admin-primary", "user_oauth", "preview-admin-subject")
    else:
        identity = IdentityContext("preview", "member-primary", "user_oauth", subject)
    token = sessions(identity)
    response = RedirectResponse("/h5", status_code=303)
    response.set_cookie("fde_auth_session", token, httponly=True, samesite="lax")
    return response


@app.get("/oauth/start")
def preview_oauth_start():
    """Keep the fixture's expired-session recovery local and deterministic."""
    return RedirectResponse("/preview-login", status_code=303)


@app.get("/h5")
def h5():
    return FileResponse(static / "meeting_review.html")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8211)
