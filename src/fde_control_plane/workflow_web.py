"""Authenticated API for member-owned workflow discovery opportunities."""

from __future__ import annotations

import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from .feishu_oauth_web import SQLiteOAuthSessionIssuer, build_session_dependency
from .meeting_review_web import csrf_token
from .models import IdentityContext
from .workflow_discovery import WorkflowDiscoveryService


class WorkflowSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: StrictStr = Field(min_length=9, max_length=128)
    chat_type: Literal["GROUP", "P2P"]
    display_label: StrictStr = Field(min_length=2, max_length=80)
    consent_confirmed: StrictBool
    consent_version: StrictStr = Field(min_length=1, max_length=80)


class WorkflowConsentRenewalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent_confirmed: StrictBool
    consent_version: StrictStr = Field(min_length=1, max_length=80)


class WorkflowDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: StrictStr = Field(pattern="^(ACCEPTED|REJECTED|DEFERRED)$")
    reason: StrictStr = Field(min_length=5, max_length=500)


class WorkflowAgentDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(min_length=2, max_length=100)
    description: StrictStr = Field(min_length=1, max_length=2000)


def build_workflow_discovery_router(service: WorkflowDiscoveryService,
                                    sessions: SQLiteOAuthSessionIssuer, *,
                                    public_origin: str) -> APIRouter:
    router = APIRouter(prefix="/api/workflow-opportunities")
    require_identity = build_session_dependency(sessions)

    def check_csrf(request: Request) -> None:
        origin = request.headers.get("origin")
        expected = csrf_token(request.cookies.get("fde_auth_session", ""))
        if (origin is not None and origin != public_origin) or not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), expected,
        ):
            raise HTTPException(403, "请求校验失败，请重新加载页面")

    def call(action, *args):
        try:
            return action(*args)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.get("/subscriptions")
    def subscriptions(request: Request, identity: IdentityContext = Depends(require_identity)):
        return {"subscriptions": call(service.list_subscriptions, identity),
                "csrf_token": csrf_token(request.cookies["fde_auth_session"])}

    @router.get("/authorization")
    def authorization(identity: IdentityContext = Depends(require_identity)):
        return {"chat_read": call(service.chat_authorization_status, identity)}

    @router.post("/subscriptions")
    def subscribe(body: WorkflowSubscriptionRequest, request: Request,
                  identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.subscribe, identity, body.chat_id, body.chat_type,
                    body.display_label, body.consent_confirmed, body.consent_version)

    @router.post("/subscriptions/{subscription_id}/consent")
    def renew_consent(subscription_id: str, body: WorkflowConsentRenewalRequest,
                      request: Request, identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.renew_consent, identity, subscription_id,
                    body.consent_confirmed, body.consent_version)

    @router.delete("/subscriptions/{subscription_id}")
    def revoke(subscription_id: str, request: Request,
               identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.revoke, identity, subscription_id)

    @router.post("/subscriptions/{subscription_id}/scan")
    def scan(subscription_id: str, request: Request,
             identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.scan_recent, identity, subscription_id)

    @router.get("")
    def opportunities(identity: IdentityContext = Depends(require_identity)):
        return {"opportunities": call(service.list_opportunities, identity)}

    @router.post("/{opportunity_id}/evidence-preview")
    def evidence_preview(opportunity_id: str, request: Request,
                         identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        result = call(service.preview_evidence, identity, opportunity_id)
        return JSONResponse(result, headers={"Cache-Control": "no-store",
                                             "Referrer-Policy": "no-referrer"})

    @router.post("/{opportunity_id}/decision")
    def decide(opportunity_id: str, body: WorkflowDecisionRequest, request: Request,
               identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.decide, identity, opportunity_id, body.decision, body.reason)

    @router.post("/{opportunity_id}/agent-draft")
    def create_agent_draft(opportunity_id: str, body: WorkflowAgentDraftRequest,
                           request: Request, identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.create_agent_draft, identity, opportunity_id,
                    body.name, body.description)

    return router


__all__ = ["build_workflow_discovery_router"]
