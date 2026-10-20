"""Authenticated meeting review UI/API; never executes remote writes."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections import Counter
from datetime import date
from threading import RLock
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from .feishu_oauth_web import SQLiteOAuthSessionIssuer, build_session_dependency
from .feishu_review import FeishuMeetingReviewGateway
from .follow_up import (
    OwnerDecisionEvent, apply_owner_decision_event, build_daily_follow_up_digest, confirmed_task_bindings,
    remote_due_mismatch,
)
from .meeting import meeting_review_snapshot, review_todo_ref
from .models import ActorType, AuditEvent, IdentityContext, TaskRunStatus
from .task_due_correction import (
    approve_due_correction, correction_for_task, latest_remote_observation, propose_due_correction,
)


def csrf_token(session: str) -> str:
    return hashlib.sha256(("meeting-review-csrf:" + session).encode()).hexdigest()


class ReviewEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: StrictStr | None = Field(default=None, max_length=2000)
    assignee_actor_id: StrictStr | None = None
    due_date: StrictStr | None = None
    discard: StrictBool | None = None
    resolve_conflict: StrictBool = False


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot: StrictStr = Field(min_length=64, max_length=64)
    updates: dict[str, ReviewEdit] = Field(max_length=200)


class MeetingStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_url: StrictStr = Field(min_length=20, max_length=1000)


class OwnerDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: StrictStr = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    source_revision: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")
    decision: Literal["ACCEPTED", "RETURNED"]
    reason: StrictStr | None = Field(default=None, max_length=1000)


class DueCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")
    observation_id: StrictStr = Field(min_length=1, max_length=200)


class AgentConfigDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(min_length=2, max_length=100)
    actor_type: Literal["PERSONAL_ASSISTANT", "BUSINESS_AGENT", "MANAGEMENT_AGENT"]
    description: StrictStr = Field(min_length=1, max_length=2000)
    capabilities: list[StrictStr] = Field(default_factory=list, max_length=30)
    skill_version: StrictStr | None = Field(default=None, max_length=100)
    base_version: int | None = Field(default=None, ge=1)


class AgentConfigApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: StrictStr = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    input_hash: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class AgentEvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: StrictStr = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    expected_hash: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")
    observed_hash: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")
    evidence_ref: StrictStr = Field(min_length=1, max_length=200)


class AgentEvaluationDatasetCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: StrictStr = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    expected_hash: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")
    evidence_ref: StrictStr = Field(min_length=1, max_length=200)


class AgentEvaluationDatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: StrictStr = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    cases: list[AgentEvaluationDatasetCase] = Field(min_length=2, max_length=50)


class AgentEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_hash: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")
    dataset_id: StrictStr = Field(min_length=1, max_length=100)
    cases: list[AgentEvaluationCase] = Field(min_length=2, max_length=50)


class AgentRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    target_version: int | None = Field(default=None, ge=1)
    volunteer_ids: list[StrictStr] = Field(default_factory=list, max_length=20)
    reason: StrictStr = Field(min_length=5, max_length=500)


class AgentVolunteerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_version: int = Field(ge=1)
    enroll: StrictBool


class AgentCanaryReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    outcome: Literal["PASS", "FAIL"]
    evidence_ref: StrictStr = Field(min_length=3, max_length=200,
                                    pattern=r"^[A-Za-z0-9_.:-]+$")


class AgentPauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int | None = Field(default=None, ge=1)
    reason: StrictStr = Field(min_length=5, max_length=500)


class AgentResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: StrictStr = Field(min_length=5, max_length=500)


class MemberStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_status: Literal["ACTIVE", "INACTIVE"]
    status: Literal["ACTIVE", "INACTIVE"]
    reason: StrictStr = Field(min_length=5, max_length=500)


class MemberReviewerRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_reviewer: StrictBool
    reviewer: StrictBool
    reason: StrictStr = Field(min_length=5, max_length=500)


class AuditTriageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_status: Literal["OPEN", "ACKNOWLEDGED"]
    status: Literal["ACKNOWLEDGED", "RESOLVED"]
    resolution: StrictStr | None = Field(default=None, max_length=500)


AGENT_REVIEWER_ROLES = frozenset({"admin", "agent_reviewer"})
AGENT_ADMIN_ROLES = frozenset({"admin"})
KNOWN_AGENT_CAPABILITIES = frozenset({
    "doc.read", "doc.write", "doc.delete", "task.read", "task.write", "task.delete",
    "message.send",
})
HIGH_RISK_AGENT_CAPABILITIES = frozenset({"doc.write", "doc.delete", "task.write", "task.delete", "message.send"})


AUDIT_EVENT_PREFIX_CATEGORIES = (
    ("AUDIT_TRIAGE", "OPERATIONS"),
    ("AGENT_CONFIG", "AGENT_GOVERNANCE"),
    ("AGENT_", "AGENT_GOVERNANCE"),
    ("APPROVAL", "APPROVAL"),
    ("FEISHU_DOCUMENT", "SOURCE"),
    ("MEMBER", "MEMBER"),
    ("OWNER_", "TASK_FOLLOW_UP"),
    ("TASK_", "TASK"),
    ("TOOL_", "TOOL"),
)
AUDIT_ERROR_OUTCOMES = frozenset({"ERROR", "FAILED", "UNKNOWN"})
AUDIT_WARNING_OUTCOMES = frozenset({"BLOCKED", "DENIED", "INVALIDATED", "REJECTED", "STALE"})


class DueCorrectionApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_hash: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class MeetingReviewWebService:
    def __init__(self, gateway: FeishuMeetingReviewGateway, *, meeting_agent_id: str = "meeting-agent") -> None:
        self.gateway = gateway
        self.cp = gateway.control_plane
        self.meeting_agent_id = meeting_agent_id
        self.lock = RLock()

    def start_meeting(self, identity: IdentityContext, body: MeetingStartRequest) -> dict[str, Any]:
        try:
            return self.gateway.start_meeting(
                document_url=body.document_url,
                requester_identity=identity,
                agent_id=self.meeting_agent_id,
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, "会议文档暂时无法读取，请稍后重试") from exc

    def agent_registry(self, identity: IdentityContext) -> dict[str, Any]:
        """Return a tenant-scoped, read-only directory of managed agents."""
        self.cp.verify_identity(identity)
        actors = self.cp.store.list_agent_actors(identity.tenant_id)
        versions = self.cp.store.list_agent_versions([row["actor_id"] for row in actors])
        can_manage = self.can_confirm_agent_config(identity)
        pauses = self.cp.store.latest_agent_pauses(identity.tenant_id) if can_manage else {}
        by_agent: dict[str, list[Any]] = {}
        for version in versions:
            by_agent.setdefault(version["agent_id"], []).append(version)
        items = []
        for actor in actors:
            history = by_agent.get(actor["actor_id"], [])
            route = self.cp.store.get_agent_route(identity.tenant_id, actor["actor_id"])
            current = (next((version for version in history if version["version"] == route["stable_version"]), None)
                       if route is not None else next((version for version in history if version["active"]), None))
            status = ("ARCHIVED" if route is not None and route["status"] == "ARCHIVED" else
                      "PAUSED" if not actor["active"] else
                      "UNVERSIONED" if current is None else
                      "CANARY" if route is not None and route["status"] == "CANARY" else "ACTIVE")
            candidate_versions = [int(version["version"]) for version in history
                                  if route is not None and version["version"] > route["stable_version"]
                                  and self.cp.store.get_agent_snapshot(identity.tenant_id, actor["actor_id"],
                                                                       version["version"]) is not None]
            candidate_version = (int(route["canary_version"]) if route is not None
                                 and route["status"] == "CANARY" else
                                 max(candidate_versions) if candidate_versions else None)
            consent = self.cp.store.agent_volunteer_consent(identity.tenant_id, actor["actor_id"], identity.actor_id)
            items.append({
                "agent_id": actor["actor_id"],
                "actor_type": actor["actor_type"],
                "status": status,
                "current_version": int(current["version"]) if current is not None else None,
                "canary_version": int(route["canary_version"]) if route is not None and route["canary_version"] else None,
                "volunteer_count": len(json.loads(route["volunteer_ids_json"])) if route is not None else 0,
                "candidate_version": candidate_version,
                "volunteer_enrolled": bool(consent["active"]) and consent["candidate_version"] == candidate_version
                if consent is not None else False,
                "version_count": len(history),
                "versions": [int(version["version"]) for version in history] if can_manage else [],
                "capabilities": sorted(json.loads(current["capabilities_json"])) if current is not None else [],
                "skill_version": current["skill_version"] if current is not None else None,
                "pause_reason": pauses[actor["actor_id"]]["reason"] if not actor["active"] and actor["actor_id"] in pauses else None,
            })
        return {"agents": items, "can_pause": can_manage, "can_resume": can_manage,
                "can_manage_lifecycle": can_manage}

    def agent_diagnostics(self, agent_id: str, identity: IdentityContext) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以查看运行诊断")
        actor = self.cp.store.get_actor(agent_id)
        if actor is None or actor["tenant_id"] != identity.tenant_id:
            raise HTTPException(404, "Agent 不存在")
        return {"agent_id": agent_id, **self.cp.store.agent_run_diagnostics(identity.tenant_id, agent_id)}

    def agent_canary_runs(self, agent_id: str, identity: IdentityContext) -> dict[str, Any]:
        if not self.can_precheck_agent_config(identity):
            raise HTTPException(403, "只有审核官或管理员可以查看灰度运行记录")
        route = self.cp.store.get_agent_route(identity.tenant_id, agent_id)
        if route is None or route["status"] != "CANARY":
            raise HTTPException(409, "Agent 当前没有灰度版本")
        runs = self.cp.store.agent_canary_runs(identity.tenant_id, agent_id)
        reviews = self.cp.store.list_agent_canary_reviews(
            identity.tenant_id, agent_id, [run["task_run_id"] for run in runs],
        )
        return {"agent_id": agent_id, "candidate_version": int(route["canary_version"]),
                "runs": [{"task_run_id": run["task_run_id"], "status": run["status"],
                          "created_at": run["created_at"],
                          "review_outcome": reviews[run["task_run_id"]]["outcome"]
                          if run["task_run_id"] in reviews else None,
                          "reviewer_id": reviews[run["task_run_id"]]["reviewer_id"]
                          if run["task_run_id"] in reviews else None,
                          "evidence_ref": reviews[run["task_run_id"]]["evidence_ref"]
                          if run["task_run_id"] in reviews else None}
                         for run in runs]}

    def agent_promotion_readiness(self, agent_id: str, identity: IdentityContext) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以查看转正就绪情况")
        route = self.cp.store.get_agent_route(identity.tenant_id, agent_id)
        if route is None:
            raise HTTPException(404, "Agent 不存在或尚无治理版本")
        if route["status"] != "CANARY":
            raise HTTPException(409, "Agent 当前没有灰度版本")
        return {"agent_id": agent_id, **self.cp.store.agent_promotion_readiness(
            identity.tenant_id, agent_id, identity.actor_id,
        )}

    def agent_promotion_history(self, agent_id: str, identity: IdentityContext) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以查看转正证据历史")
        actor = self.cp.store.get_actor(agent_id)
        if actor is None or actor["tenant_id"] != identity.tenant_id:
            raise HTTPException(404, "Agent 不存在")
        return {"agent_id": agent_id,
                "promotions": self.cp.store.list_agent_promotion_evidence(identity.tenant_id, agent_id)}

    def review_agent_canary_run(self, agent_id: str, task_run_id: str,
                                identity: IdentityContext, body: AgentCanaryReviewRequest) -> dict[str, Any]:
        if not self.can_precheck_agent_config(identity):
            raise HTTPException(403, "只有审核官或管理员可以评审灰度运行")
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="AGENT_CANARY_REVIEWED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id, task_run_id=task_run_id,
            correlation_id=None, payload_hash=hashlib.sha256(json.dumps({
                "agent_id": agent_id, "version": body.expected_version,
                "outcome": body.outcome, "evidence_ref": body.evidence_ref,
            }, sort_keys=True).encode()).hexdigest(), outcome=body.outcome,
        )
        if not self.cp.store.save_agent_canary_review(
                tenant_id=identity.tenant_id, agent_id=agent_id, task_run_id=task_run_id,
                expected_version=body.expected_version, reviewer_id=identity.actor_id,
                outcome=body.outcome, evidence_ref=body.evidence_ref, event=event):
            raise HTTPException(409, "灰度任务、版本或评审状态已变化")
        return {"agent_id": agent_id, "task_run_id": task_run_id,
                "outcome": body.outcome, "remote_write": False}

    def agent_comparison(self, agent_id: str, old_version: int, new_version: int,
                         identity: IdentityContext) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以比较版本")
        snapshots = [self.cp.store.get_agent_snapshot(identity.tenant_id, agent_id, version)
                     for version in (old_version, new_version)]
        if any(snapshot is None for snapshot in snapshots):
            raise HTTPException(404, "版本快照不存在")
        old, new = [json.loads(snapshot["config_json"]) for snapshot in snapshots]
        diagnostics = self.cp.store.agent_run_diagnostics(identity.tenant_id, agent_id)["versions"]
        return {"agent_id": agent_id, "old_version": old_version, "new_version": new_version,
                "changes": {key: {"old": old.get(key), "new": new.get(key)}
                            for key in ("description", "capabilities", "skill_version")
                            if old.get(key) != new.get(key)},
                "runs": {str(version): diagnostics.get(version, {"total": 0, "statuses": {}, "failures": 0})
                         for version in (old_version, new_version)},
                "config_hashes": {str(old_version): snapshots[0]["config_hash"],
                                  str(new_version): snapshots[1]["config_hash"]}}

    def transition_agent(self, agent_id: str, action: str, identity: IdentityContext,
                         body: AgentRouteRequest) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以变更 Agent 生命周期")
        reason = body.reason.strip()
        if len(reason) < 5:
            raise HTTPException(422, "请填写至少 5 个字的处置原因")
        volunteers = list(dict.fromkeys(body.volunteer_ids))
        if action == "CANARY" and (not volunteers or body.target_version is None):
            raise HTTPException(422, "灰度需要候选版本和至少一名志愿者")
        if action in {"PROMOTE", "ROLLBACK"} and body.target_version is None:
            raise HTTPException(422, "请指定目标版本")
        if action not in {"CANARY", "PROMOTE", "ROLLBACK", "ARCHIVE"}:
            raise HTTPException(404, "未知生命周期操作")
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="AGENT_" + action,
            tenant_id=identity.tenant_id, actor_id=identity.actor_id, task_run_id=None,
            correlation_id=None, payload_hash=hashlib.sha256(json.dumps({
                "agent_id": agent_id, "expected_version": body.expected_version,
                "target_version": body.target_version, "volunteers": volunteers,
                "reason": reason,
            }, sort_keys=True, ensure_ascii=True).encode()).hexdigest(), outcome=action,
        )
        changed = self.cp.store.transition_agent_route(
            tenant_id=identity.tenant_id, agent_id=agent_id, action=action,
            expected_version=body.expected_version, target_version=body.target_version,
            volunteer_ids=volunteers, event=event,
        )
        if not changed:
            raise HTTPException(409, "Agent 版本、状态或志愿者已变化，请刷新后核对")
        return {"agent_id": agent_id, "action": action, "remote_write": False}

    def set_agent_volunteer(self, agent_id: str, identity: IdentityContext,
                            body: AgentVolunteerRequest) -> dict[str, Any]:
        actor = self.cp.verify_identity(identity)
        if identity.auth_mode != "user_oauth" or actor.actor_type != ActorType.USER:
            raise HTTPException(403, "只有成员本人可以报名或退出灰度")
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12),
            event_type="AGENT_VOLUNTEER_JOINED" if body.enroll else "AGENT_VOLUNTEER_LEFT",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id, task_run_id=None,
            correlation_id=None, payload_hash=hashlib.sha256(json.dumps({
                "agent_id": agent_id, "candidate_version": body.candidate_version,
                "enroll": body.enroll,
            }, sort_keys=True).encode()).hexdigest(),
            outcome="ENROLLED" if body.enroll else "WITHDRAWN",
        )
        if not self.cp.store.set_agent_volunteer_consent(
                identity.tenant_id, agent_id, identity.actor_id, body.candidate_version,
                body.enroll, event):
            raise HTTPException(409, "候选版本、报名状态或成员身份已变化")
        return {"agent_id": agent_id, "candidate_version": body.candidate_version,
                "enrolled": body.enroll, "remote_write": False}

    def pause_agent(self, agent_id: str, identity: IdentityContext,
                    body: AgentPauseRequest) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以暂停 Agent")
        reason = body.reason.strip()
        if len(reason) < 5:
            raise HTTPException(422, "请填写至少 5 个字的暂停原因")
        payload = {"agent_id": agent_id, "expected_version": body.expected_version, "reason": reason}
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="AGENT_PAUSED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id,
            task_run_id=None, correlation_id=None,
            payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest(),
            outcome="PAUSED",
        )
        if not self.cp.store.pause_agent(identity.tenant_id, agent_id, body.expected_version, reason, event):
            raise HTTPException(409, "Agent 状态或版本已变化，请刷新目录后核对")
        return {"agent_id": agent_id, "status": "PAUSED", "remote_write": False}

    def resume_agent(self, agent_id: str, identity: IdentityContext,
                     body: AgentResumeRequest) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以恢复 Agent")
        reason = body.reason.strip()
        if len(reason) < 5:
            raise HTTPException(422, "请填写至少 5 个字的恢复原因")
        payload = {"agent_id": agent_id, "expected_version": body.expected_version, "reason": reason}
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="AGENT_RESUMED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id,
            task_run_id=None, correlation_id=None,
            payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest(),
            outcome="ACTIVE",
        )
        if not self.cp.store.resume_agent(identity.tenant_id, agent_id, body.expected_version, reason, event):
            raise HTTPException(409, "Agent 状态或版本已变化，请刷新目录后核对")
        return {"agent_id": agent_id, "status": "ACTIVE", "remote_write": False}

    def agent_config_drafts(self, identity: IdentityContext) -> list[dict[str, Any]]:
        actor = self.cp.verify_identity(identity)
        items = []
        for row in self.cp.store.list_agent_config_drafts(identity.tenant_id):
            data = json.loads(row["data_json"])
            review = self.cp.store.get_latest_agent_config_review(row["draft_id"], identity.tenant_id)
            report = json.loads(review["report_json"]) if review is not None else None
            items.append({
                "draft_id": row["draft_id"], "status": row["status"],
                "created_by_current_member": row["created_by"] == identity.actor_id,
                "created_at": row["created_at"], "name": data["name"],
                "actor_type": data["actor_type"], "description": data["description"],
                "capabilities": data["capabilities"], "skill_version": data.get("skill_version"),
                "base_version": data.get("base_version"),
                "evaluation_status": (evaluation["status"] if (evaluation := self.cp.store.latest_agent_evaluation(
                    identity.tenant_id, row["draft_id"], hashlib.sha256(json.dumps(
                        data, sort_keys=True, ensure_ascii=True).encode()).hexdigest())) is not None else None),
                "review_status": review["status"] if review is not None else None,
                "review_id": review["review_id"] if review is not None else None,
                "review_input_hash": review["input_hash"] if review is not None else None,
                "review_blockers": (report or {}).get("blockers", []),
                "review_warnings": (report or {}).get("warnings", []),
            })
        return items

    def can_precheck_agent_config(self, identity: IdentityContext) -> bool:
        actor = self.cp.verify_identity(identity)
        return (identity.auth_mode == "user_oauth" and actor.actor_type.value == "USER"
                and bool(actor.roles & AGENT_REVIEWER_ROLES))

    def can_confirm_agent_config(self, identity: IdentityContext) -> bool:
        actor = self.cp.verify_identity(identity)
        return (identity.auth_mode == "user_oauth" and actor.actor_type.value == "USER"
                and bool(actor.roles & AGENT_ADMIN_ROLES))

    def audit_events(self, identity: IdentityContext, limit: int = 100) -> dict[str, Any]:
        """Return a redacted audit view for administrators only."""
        if not self.can_confirm_agent_config(identity):
            return {"can_view": False, "events": [], "counts": {}}
        events = []
        for item in self.cp.store.list_tenant_audits(identity.tenant_id, limit):
            event_type = str(item.get("event_type", "UNKNOWN"))
            outcome = str(item.get("outcome", "UNKNOWN"))
            category = "SYSTEM"
            for prefix, candidate in AUDIT_EVENT_PREFIX_CATEGORIES:
                if event_type.startswith(prefix):
                    category = candidate
                    break
            if outcome in AUDIT_ERROR_OUTCOMES:
                severity, action = "HIGH", "RECONCILE_BEFORE_RETRY"
            elif outcome in AUDIT_WARNING_OUTCOMES:
                severity, action = "WARN", "CHECK_POLICY_OR_SOURCE"
            else:
                severity, action = "INFO", "NONE"
            event = {
                "event_type": event_type,
                "outcome": outcome,
                "category": category,
                "severity": severity,
                "recommended_action": action,
                "created_at": str(item.get("created_at", "")),
                "audit_ref": hashlib.sha256(str(item.get("audit_id", "")).encode()).hexdigest()[:12],
                "payload_ref": str(item.get("payload_hash", ""))[:12],
                "triage_status": (self.cp.store.get_audit_triage(identity.tenant_id, str(item["audit_id"])) or {}).get("status", "OPEN"),
            }
            if item.get("task_run_id"):
                event["task_ref"] = hashlib.sha256(str(item["task_run_id"]).encode()).hexdigest()[:12]
            events.append(event)
        counts: dict[str, int] = {}
        for event in events:
            key = event["outcome"]
            counts[key] = counts.get(key, 0) + 1
        return {"can_view": True, "events": events, "counts": counts,
                "summary": self.cp.store.audit_operations_summary(identity.tenant_id)}

    def triage_audit(self, identity: IdentityContext, audit_ref: str,
                     body: AuditTriageRequest) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以处置审计事件")
        matches = [item for item in self.cp.store.list_tenant_audits(identity.tenant_id, 100)
                   if hashlib.sha256(str(item["audit_id"]).encode()).hexdigest()[:12] == audit_ref]
        if len(matches) != 1:
            raise HTTPException(404, "审计事件不存在或不在最近列表中")
        source = matches[0]
        if str(source.get("outcome", "")).upper() not in AUDIT_ERROR_OUTCOMES | AUDIT_WARNING_OUTCOMES:
            raise HTTPException(409, "该事件无需人工处置")
        resolution = (body.resolution or "").strip()
        if body.status == "RESOLVED" and not resolution:
            raise HTTPException(422, "关闭事件必须填写核实结论")
        if body.status == "ACKNOWLEDGED" and resolution:
            raise HTTPException(422, "认领事件时无需填写结论")
        audit_event = AuditEvent(
            audit_id="audit-triage-" + secrets.token_hex(10), event_type="AUDIT_TRIAGE_UPDATED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id,
            task_run_id=None, correlation_id=None,
            payload_hash=hashlib.sha256(json.dumps({
                "source_audit_id": source["audit_id"], "status": body.status,
                "resolution_hash": hashlib.sha256(resolution.encode()).hexdigest() if resolution else None,
            }, sort_keys=True).encode()).hexdigest(), outcome=body.status,
        )
        result = self.cp.store.set_audit_triage(
            identity.tenant_id, str(source["audit_id"]), identity.actor_id,
            body.expected_status, body.status, resolution or None, audit_event,
        )
        if result != body.status:
            raise HTTPException(409 if result != "NOT_FOUND" else 404, "事件状态已变化，请刷新后核对")
        return {"audit_ref": audit_ref, "status": result, "remote_write": False}

    def pending_member_access(self, identity: IdentityContext) -> list[dict[str, Any]]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以查看成员接入申请")
        return [{"request_id": row["request_id"], "created_at": row["created_at"]}
                for row in self.cp.store.list_pending_member_access(identity.tenant_id)]

    def member_directory(self, identity: IdentityContext) -> list[dict[str, Any]]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以查看成员目录")
        changes = self.cp.store.latest_member_status_changes(identity.tenant_id)
        reviewer_changes = self.cp.store.latest_member_reviewer_role_changes(identity.tenant_id)
        return [{
            "actor_id": row["actor_id"],
            "display_name": row["display_name"],
            "role": "ADMIN" if "admin" in json.loads(row["roles_json"]) else "MEMBER",
            "reviewer": "agent_reviewer" in json.loads(row["roles_json"]),
            "status": "ACTIVE" if row["active"] else "INACTIVE",
            "joined_at": row["joined_at"],
            "status_reason": changes[row["actor_id"]]["reason"] if row["actor_id"] in changes else None,
            "reviewer_reason": reviewer_changes[row["actor_id"]]["reason"]
                               if row["actor_id"] in reviewer_changes else None,
        } for row in self.cp.store.list_member_directory(identity.tenant_id)]

    def set_member_status(self, actor_id: str, identity: IdentityContext,
                          body: MemberStatusRequest) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以变更成员状态")
        if actor_id == identity.actor_id:
            raise HTTPException(409, "不能通过此入口停用当前管理员")
        target = self.cp.store.get_actor(actor_id)
        if target is None or target["tenant_id"] != identity.tenant_id or target["actor_type"] != "USER" \
                or not target["external_ref_hash"]:
            raise HTTPException(404, "成员不存在或不属于当前租户")
        if "admin" in json.loads(target["roles_json"]):
            raise HTTPException(409, "不能通过此入口变更管理员状态")
        if body.expected_status == body.status:
            raise HTTPException(422, "目标状态必须与当前状态不同")
        expected_active = body.expected_status == "ACTIVE"
        active = body.status == "ACTIVE"
        reason = body.reason.strip()
        payload = {"actor_id": actor_id, "expected_status": body.expected_status,
                   "status": body.status, "reason": reason}
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="MEMBER_STATUS_CHANGED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id,
            task_run_id=None, correlation_id=None,
            payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest(),
            outcome=body.status,
        )
        if not self.cp.store.set_member_active(identity.tenant_id, actor_id, expected_active, active, reason, event):
            raise HTTPException(409, "成员状态已变化，请刷新目录后核对")
        return {"actor_id": actor_id, "status": body.status, "remote_write": False}

    def set_member_reviewer_role(self, actor_id: str, identity: IdentityContext,
                                 body: MemberReviewerRoleRequest) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以变更审核官角色")
        target = self.cp.store.get_actor(actor_id)
        if target is None or target["tenant_id"] != identity.tenant_id or target["actor_type"] != "USER" \
                or not target["external_ref_hash"]:
            raise HTTPException(404, "成员不存在或不属于当前租户")
        if actor_id == identity.actor_id or "admin" in json.loads(target["roles_json"]):
            raise HTTPException(409, "不能通过此入口变更管理员角色")
        if not target["active"]:
            raise HTTPException(409, "只有已启用成员可以担任审核官")
        if body.expected_reviewer == body.reviewer:
            raise HTTPException(422, "目标角色必须与当前角色不同")
        reason = body.reason.strip()
        if len(reason) < 5:
            raise HTTPException(422, "请填写至少 5 个字的原因")
        payload = {"actor_id": actor_id, "expected_reviewer": body.expected_reviewer,
                   "reviewer": body.reviewer, "reason": reason}
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="MEMBER_REVIEWER_ROLE_CHANGED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id,
            task_run_id=None, correlation_id=None,
            payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest(),
            outcome="GRANTED" if body.reviewer else "REVOKED",
        )
        if not self.cp.store.set_member_reviewer_role(identity.tenant_id, actor_id,
                                                      body.expected_reviewer, body.reviewer, reason, event):
            raise HTTPException(409, "审核官角色或成员状态已变化，请刷新目录后核对")
        return {"actor_id": actor_id, "reviewer": body.reviewer, "remote_write": False}

    def approve_member_access(self, request_id: str, identity: IdentityContext) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以批准成员接入")
        try:
            result = self.cp.store.approve_member_access(
                identity.tenant_id, request_id, "member-" + secrets.token_hex(8),
            )
        except ValueError as exc:
            if str(exc) == "REQUEST_NOT_FOUND":
                raise HTTPException(404, "成员接入申请不存在") from exc
            raise HTTPException(409, "成员接入申请已变化，请刷新后核对") from exc
        if not result["duplicate"]:
            self.cp._audit("MEMBER_ACCESS_APPROVED", identity.tenant_id, identity.actor_id, None,
                           {"request_id": request_id, "actor_id": result["actor_id"]}, "APPROVED")
        return result

    def precheck_agent_config_draft(self, draft_id: str, identity: IdentityContext) -> dict[str, Any]:
        actor = self.cp.verify_identity(identity)
        if not self.can_precheck_agent_config(identity):
            raise HTTPException(403, "只有审核官或管理员可以执行 Agent 配置预审")
        row = self.cp.store.get_agent_config_draft(draft_id, identity.tenant_id)
        if row is None:
            raise HTTPException(404, "配置草稿不存在或无权访问")
        data = json.loads(row["data_json"])
        input_hash = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        evaluation = self.cp.store.latest_agent_evaluation(identity.tenant_id, draft_id, input_hash)
        previous = self.cp.store.get_latest_agent_config_review(draft_id, identity.tenant_id)
        if (previous is not None and previous["input_hash"] == input_hash
                and json.loads(previous["report_json"]).get("evaluation_id") ==
                (evaluation["evaluation_id"] if evaluation is not None else None)):
            report = json.loads(previous["report_json"])
            return {"draft_id": draft_id, "review_id": previous["review_id"], "input_hash": input_hash,
                    "status": previous["status"],
                    "blockers": report.get("blockers", []), "warnings": report.get("warnings", []),
                    "requires_admin_confirmation": previous["status"] == "READY_FOR_ADMIN", "duplicate": True}

        blockers: list[str] = []
        warnings: list[str] = []
        capabilities = set(data.get("capabilities") or [])
        unknown = sorted(capabilities - KNOWN_AGENT_CAPABILITIES)
        if unknown:
            blockers.append("UNKNOWN_CAPABILITY:" + ",".join(unknown))
        if row["created_by"] == identity.actor_id:
            blockers.append("SELF_REVIEW")
        existing = self.cp.store.list_agent_actors(identity.tenant_id)
        matched = [item for item in existing if str(item["actor_id"]).casefold() == str(data["name"]).casefold()]
        if data.get("base_version") is None:
            if matched:
                blockers.append("AGENT_NAME_CONFLICT")
        else:
            route = self.cp.store.get_agent_route(identity.tenant_id, data["name"])
            if (len(matched) != 1 or matched[0]["actor_type"] != data["actor_type"]
                    or not matched[0]["active"] or route is None or route["status"] != "ACTIVE"
                    or route["stable_version"] != data["base_version"]):
                blockers.append("AGENT_BASE_VERSION_STALE")
            evaluation = self.cp.store.latest_agent_evaluation(identity.tenant_id, draft_id, input_hash)
            if evaluation is None or evaluation["status"] != "PASSED":
                blockers.append("AGENT_EVALUATION_REQUIRED")
        high_risk = sorted(capabilities & HIGH_RISK_AGENT_CAPABILITIES)
        if high_risk:
            warnings.append("HIGH_RISK_CAPABILITY:" + ",".join(high_risk))
        if data.get("actor_type") == "MANAGEMENT_AGENT":
            warnings.append("MANAGEMENT_AGENT_REQUIRES_ADMIN_SCOPE_REVIEW")
        status = "BLOCKED" if blockers else "READY_FOR_ADMIN"
        report = {
            "input_hash": input_hash, "blockers": blockers, "warnings": warnings,
            "requested_capabilities": sorted(capabilities), "reviewer_role": sorted(actor.roles & AGENT_REVIEWER_ROLES),
            "evaluation_id": evaluation["evaluation_id"] if evaluation is not None else None,
        }
        review_id = "agent-review-" + secrets.token_hex(10)
        self.cp.store.save_agent_config_review(review_id, draft_id, identity.tenant_id, identity.actor_id,
                                               input_hash, status, report)
        self.cp._audit("AGENT_CONFIG_PRECHECKED", identity.tenant_id, identity.actor_id, None,
                       {"draft_id": draft_id, "review_id": review_id, "status": status,
                        "blocker_count": len(blockers), "warning_count": len(warnings)}, status)
        return {"draft_id": draft_id, "review_id": review_id, "input_hash": input_hash, "status": status,
                "blockers": blockers, "warnings": warnings,
                "requires_admin_confirmation": status == "READY_FOR_ADMIN", "duplicate": False}

    def confirm_agent_config_draft(self, draft_id: str, identity: IdentityContext,
                                   body: AgentConfigApprovalRequest) -> dict[str, Any]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以确认 Agent 配置发布")
        row = self.cp.store.get_agent_config_draft(draft_id, identity.tenant_id)
        if row is None:
            raise HTTPException(404, "配置草稿不存在或无权访问")
        data = json.loads(row["data_json"])
        input_hash = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        if body.input_hash != input_hash:
            raise HTTPException(409, "配置草稿已变化，请重新运行审核预审")
        review = self.cp.store.get_latest_agent_config_review(draft_id, identity.tenant_id)
        if review is None or review["review_id"] != body.review_id or review["input_hash"] != input_hash:
            raise HTTPException(409, "审核预审版本已变化，请重新运行审核预审")
        if review["status"] != "READY_FOR_ADMIN":
            raise HTTPException(409, "审核预审未通过，不能发布 Agent")
        approval_id = "agent-approval-" + secrets.token_hex(10)
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="AGENT_CONFIG_CONFIRMED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id, task_run_id=None,
            correlation_id=None, payload_hash=hashlib.sha256(json.dumps({
                "draft_id": draft_id, "approval_id": approval_id,
                "agent_id": data["name"], "input_hash": input_hash,
            }, sort_keys=True, ensure_ascii=True).encode()).hexdigest(), outcome="PUBLISHED",
        )
        try:
            result = self.cp.store.publish_agent_config_draft(
                draft_id=draft_id, tenant_id=identity.tenant_id, approver_id=identity.actor_id,
                review_id=body.review_id, input_hash=input_hash, agent_id=data["name"],
                actor_type=ActorType(data["actor_type"]).value, capabilities=list(data.get("capabilities") or []),
                skill_version=data.get("skill_version"), approval_id=approval_id, event=event,
            )
        except ValueError as exc:
            reasons = {
                "DRAFT_NOT_FOUND": (404, "配置草稿不存在或无权访问"),
                "DRAFT_ALREADY_PUBLISHED": (409, "配置草稿已经发布"),
                "PRECHECK_VERSION_STALE": (409, "审核预审版本已变化，请重新运行审核预审"),
                "PRECHECK_NOT_READY": (409, "审核预审未通过，不能发布 Agent"),
                "AGENT_NAME_CONFLICT": (409, "Agent 名称已存在，请修改草稿后重新预审"),
                "AGENT_BASE_VERSION_STALE": (409, "Agent 基线版本已变化，请重新提交草稿"),
                "AGENT_EVALUATION_REQUIRED": (409, "候选版本缺少通过的独立评测"),
                "SELF_EVALUATION_APPROVAL": (409, "评测人不能确认发布自己的评测结果"),
            }
            code, message = reasons.get(str(exc), (409, "Agent 配置发布未完成，请重新核对"))
            raise HTTPException(code, message) from exc
        return {**result, "draft_id": draft_id}

    def create_agent_config_draft(self, identity: IdentityContext, body: AgentConfigDraftRequest) -> dict[str, Any]:
        actor = self.cp.verify_identity(identity)
        if identity.auth_mode != "user_oauth" or actor.actor_type.value != "USER":
            raise HTTPException(403, "请使用成员身份提交 Agent 配置草稿")
        name = body.name.strip()
        description = body.description.strip()
        capabilities = sorted({item.strip() for item in body.capabilities if item.strip()})
        if not name or not description:
            raise HTTPException(422, "Agent 名称和职责说明不能为空")
        if any(len(item) > 100 for item in capabilities):
            raise HTTPException(422, "单项能力名称不能超过 100 个字符")
        draft_id = "agent-draft-" + secrets.token_hex(10)
        data = {"name": name, "actor_type": body.actor_type, "description": description,
                "capabilities": capabilities, "skill_version": body.skill_version.strip() if body.skill_version else None,
                "base_version": body.base_version}
        self.cp.store.save_agent_config_draft(draft_id, identity.tenant_id, identity.actor_id, "PENDING_REVIEW", data)
        self.cp._audit("AGENT_CONFIG_DRAFT_CREATED", identity.tenant_id, identity.actor_id, None,
                       {"draft_id": draft_id, "actor_type": body.actor_type, "capability_count": len(capabilities)},
                       "PENDING_REVIEW")
        return {"draft_id": draft_id, "status": "PENDING_REVIEW", "name": name}

    def evaluate_agent_draft(self, draft_id: str, identity: IdentityContext,
                             body: AgentEvaluationRequest) -> dict[str, Any]:
        if not self.can_precheck_agent_config(identity):
            raise HTTPException(403, "只有审核官或管理员可以登记独立评测")
        row = self.cp.store.get_agent_config_draft(draft_id, identity.tenant_id)
        if row is None:
            raise HTTPException(404, "配置草稿不存在")
        if row["status"] != "PENDING_REVIEW" or row["created_by"] == identity.actor_id:
            raise HTTPException(409, "已发布草稿或提交人本人不能登记评测")
        data = json.loads(row["data_json"])
        current_hash = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        if body.input_hash != current_hash:
            raise HTTPException(409, "评测输入版本已变化")
        dataset = self.cp.store.get_agent_eval_dataset(identity.tenant_id, body.dataset_id)
        if dataset is None or dataset["created_by"] == row["created_by"]:
            raise HTTPException(409, "独立评测集不存在或由草稿提交人创建")
        cases = [case.model_dump() for case in body.cases]
        if len({case["case_id"] for case in cases}) != len(cases):
            raise HTTPException(422, "评测案例编号必须唯一")
        expected = {case["case_id"]: case for case in json.loads(dataset["cases_json"])}
        if (set(expected) != {case["case_id"] for case in cases}
                or any(case["expected_hash"] != expected[case["case_id"]]["expected_hash"]
                       or case["evidence_ref"] != expected[case["case_id"]]["evidence_ref"]
                       for case in cases)):
            raise HTTPException(409, "案例与不可变评测集不一致")
        passed = sum(case["expected_hash"] == case["observed_hash"] for case in cases)
        status = "PASSED" if passed == len(cases) else "FAILED"
        evaluation_id = "agent-eval-" + secrets.token_hex(10)
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="AGENT_EVALUATED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id, task_run_id=None,
            correlation_id=None, payload_hash=hashlib.sha256(json.dumps({
                "draft_id": draft_id, "input_hash": current_hash, "dataset_id": body.dataset_id,
                "case_count": len(cases), "passed": passed,
            }, sort_keys=True).encode()).hexdigest(), outcome=status,
        )
        self.cp.store.save_agent_evaluation(
            evaluation_id=evaluation_id, tenant_id=identity.tenant_id, draft_id=draft_id,
            input_hash=current_hash, evaluator_id=identity.actor_id, dataset_id=body.dataset_id,
            cases=cases, status=status, event=event,
        )
        return {"evaluation_id": evaluation_id, "draft_id": draft_id, "status": status,
                "passed": passed, "total": len(cases), "remote_write": False}

    def create_agent_eval_dataset(self, identity: IdentityContext,
                                  body: AgentEvaluationDatasetRequest) -> dict[str, Any]:
        if not self.can_precheck_agent_config(identity):
            raise HTTPException(403, "只有审核官或管理员可以登记独立评测集")
        cases = [case.model_dump() for case in body.cases]
        if len({case["case_id"] for case in cases}) != len(cases):
            raise HTTPException(422, "评测案例编号必须唯一")
        event = AuditEvent(
            audit_id="audit-" + secrets.token_hex(12), event_type="AGENT_EVAL_DATASET_CREATED",
            tenant_id=identity.tenant_id, actor_id=identity.actor_id, task_run_id=None,
            correlation_id=None, payload_hash=hashlib.sha256(json.dumps({
                "dataset_id": body.dataset_id, "cases": cases,
            }, sort_keys=True, ensure_ascii=True).encode()).hexdigest(), outcome="CREATED",
        )
        try:
            self.cp.store.save_agent_eval_dataset(identity.tenant_id, body.dataset_id,
                                                  identity.actor_id, cases, event)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "评测集编号已存在；版本不可覆盖") from exc
        return {"dataset_id": body.dataset_id, "case_count": len(cases), "remote_write": False}

    def owned_record(self, record_id: str, identity: IdentityContext) -> dict[str, Any]:
        self.cp.verify_identity(identity)
        record = self.cp.store.get_meeting_record(record_id)
        if record is None or record["tenant_id"] != identity.tenant_id or record["submitted_by"] != identity.actor_id:
            raise HTTPException(404, "会议不存在或无权访问")
        task = self.cp._task(record["task_run_id"])
        if task.tenant_id != identity.tenant_id or task.requested_by != identity.actor_id:
            raise HTTPException(404, "会议不存在或无权访问")
        return record

    def detail(self, record_id: str, identity: IdentityContext) -> dict[str, Any]:
        record = self.owned_record(record_id, identity)
        task = self.cp._task(record["task_run_id"])
        drafts = self.cp.store.list_meeting_todos(record_id)
        source = self.cp.store.get_meeting_source(record_id)
        evidence = {int(b["block_id"]): b["text"] for b in (source or {}).get("evidence", [])}
        snapshot = meeting_review_snapshot(record, task.status, drafts)
        return {
            "record_id": record_id,
            "title": (source or {}).get("title", "会议纪要"),
            "revision_id": record["revision_id"],
            "task_status": task.status,
            "snapshot": snapshot,
            "source_available": bool(source),
            "todos": [{
                "todo_ref": review_todo_ref(d["todo_id"]),
                "title": d["title"], "assignee_actor_id": d["assignee_actor_id"],
                "due_date": d["due_date"], "status": d["status"],
                "confirmation_reasons": d["confirmation_reasons"],
                "evidence": [{"block_id": b, "text": evidence.get(int(b), "")}
                             for b in d["evidence_block_ids"]],
            } for d in drafts],
            "approvals": [{"approval_id": a["approval_id"], "status": a["status"],
                           "card_status": self.card_status(a["approval_id"])}
                          for a in self.cp.store.list_approvals(task.task_run_id)],
            "execution": self.execution_summary(task.task_run_id),
        }

    def card_status(self, approval_id: str) -> str:
        if self.cp.store.get_approval_delivery(approval_id) is not None:
            return "SENT"
        attempt = self.cp.store.get_approval_delivery_attempt(approval_id)
        if attempt is None:
            return "NOT_SENT"
        # An in-flight send could have succeeded even if the process died.
        return "UNKNOWN" if attempt["status"] == "SENDING" else str(attempt["status"])

    def execution_summary(self, task_run_id: str) -> dict[str, Any]:
        """Return a redacted execution view for the meeting owner."""
        items: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for call in self.cp.store.list_tool_calls(task_run_id):
            status = str(call.get("status", "UNKNOWN"))
            counts[status] = counts.get(status, 0) + 1
            outbox = self.cp.store.get_outbox_for_tool_call(str(call["tool_call_id"]))
            items.append({
                "status": status,
                "error_code": call.get("error_code"),
                "remote_ref_present": bool(call.get("remote_ref")),
                "outbox_status": str(outbox["status"]) if outbox is not None else None,
            })
        return {"counts": counts, "items": items}

    def daily_follow_up_digest(self, identity: IdentityContext, business_date: str | None = None) -> dict[str, Any]:
        """Return a redacted, unsent digest preview for the current member."""
        self.cp.verify_identity(identity)
        normalized_date = date.today().isoformat() if business_date is None else str(business_date)
        try:
            normalized_date = date.fromisoformat(normalized_date).isoformat()
        except ValueError as exc:
            raise HTTPException(422, "业务日期格式应为 YYYY-MM-DD") from exc
        observations = self.cp.store.list_follow_up_observations_for_tenant(identity.tenant_id)
        # Organizers can follow their own meeting runs. Assignees see only the
        # specific remote tasks assigned to them, never sibling meeting todos.
        bindings = confirmed_task_bindings(self.cp.store.connection, identity.tenant_id)
        owned = {(b["task"]["task_run_id"], b["call"]["remote_ref"]) for b in self.owner_bindings(identity)}
        approved_due = {(b["task"]["task_run_id"], b["call"]["remote_ref"]):
                        b["proposal"]["arguments"].get("due_date") for b in bindings}
        runs = {str(o.get("task_run_id") or ""): self.cp.store.get_task_run(str(o.get("task_run_id") or ""))
                for o in observations}
        observations = [o for o in observations if (
            (runs.get(o.get("task_run_id")) or {}).get("tenant_id") == identity.tenant_id
            and ((runs[o["task_run_id"]].get("requested_by") == identity.actor_id)
                 or (o["task_run_id"], o.get("remote_task_id")) in owned))]
        observations = [
            {**o, "status": "UNKNOWN", "reason": "REMOTE_DUE_MISMATCH"}
            if not o.get("decision") and remote_due_mismatch(
                approved_due.get((o["task_run_id"], o.get("remote_task_id"))), o,
            ) else o for o in observations
        ]
        digest = build_daily_follow_up_digest(
            tenant_id=identity.tenant_id,
            business_date=normalized_date,
            observations=observations,
            recipient_actor_id=identity.actor_id,
        )
        return {
            "business_date": digest.business_date,
            "counts": {status: count for status, count in digest.counts.items() if count},
            "attention_count": len(digest.attention_task_ids),
            "attention_task_hashes": [hashlib.sha256(task_id.encode()).hexdigest()[:12]
                                      for task_id in digest.attention_task_ids],
            "notification_key_hash": hashlib.sha256(digest.notification_key.encode()).hexdigest()[:12],
            "notification_sent": False,
            "remote_write": False,
        }

    def owner_bindings(self, identity: IdentityContext) -> list[dict[str, Any]]:
        actor = self.cp.verify_identity(identity)
        if identity.auth_mode != "user_oauth" or actor.actor_type != "USER":
            raise HTTPException(403, "请使用成员身份登录")
        bindings = confirmed_task_bindings(self.cp.store.connection, identity.tenant_id)
        counts = Counter((b["task"]["task_run_id"], b["call"]["remote_ref"]) for b in bindings)
        return [b for b in bindings
                if b["proposal"]["arguments"].get("assignee_actor_id") == identity.actor_id
                and counts[(b["task"]["task_run_id"], b["call"]["remote_ref"])] == 1]

    @staticmethod
    def owner_task_ref(binding: dict[str, Any]) -> str:
        # Opaque reference is a locator, not authority. Resolve permissions on
        # every GET/POST and keep raw remote IDs out of the browser response.
        return hashlib.sha256(json.dumps([
            binding["task"]["tenant_id"], binding["call"]["tool_call_id"], binding["call"]["remote_ref"],
        ], separators=(",", ":")).encode()).hexdigest()

    def owner_tasks(self, identity: IdentityContext) -> list[dict[str, Any]]:
        bindings = self.owner_bindings(identity)
        observations = self.cp.store.list_follow_up_observations_for_tenant(identity.tenant_id)
        items = []
        for binding in bindings:
            task, call, proposal = binding["task"], binding["call"], binding["proposal"]
            relevant = [o for o in observations if o.get("task_run_id") == task["task_run_id"]
                        and o.get("remote_task_id") == call["remote_ref"]
                        and (not o.get("decision") or o.get("source_revision") == proposal["proposal_hash"])]
            decisions = [o for o in relevant if o.get("decision")]
            remotes = [o for o in relevant if not o.get("decision")]
            latest_remote = latest_remote_observation(self.cp.store, task["task_run_id"], call["remote_ref"])
            due_mismatch = latest_remote is not None and remote_due_mismatch(
                proposal["arguments"].get("due_date"), latest_remote,
            )
            latest = max(decisions, key=lambda o: o.get("observed_at", ""), default={})
            digest = build_daily_follow_up_digest(
                tenant_id=identity.tenant_id, business_date=date.today(), observations=relevant,
                recipient_actor_id=identity.actor_id,
            )
            status = next((key for key, count in digest.counts.items() if count), "WAITING_OWNER")
            if due_mismatch:
                status = "UNKNOWN"
            args = proposal["arguments"]
            items.append({
                "task_ref": self.owner_task_ref(binding), "source_revision": proposal["proposal_hash"],
                "title": args.get("title", "会议待办"), "due_date": args.get("due_date"),
                "status": status, "decision": latest.get("decision"), "reason": latest.get("reason"),
                "due_mismatch": due_mismatch,
                "can_decide": not latest and not due_mismatch
                              and status not in {"COMPLETED", "UNKNOWN", "WAITING_HUMAN"},
            })
        return items

    def organizer_bindings(self, identity: IdentityContext) -> list[dict[str, Any]]:
        actor = self.cp.verify_identity(identity)
        if identity.auth_mode != "user_oauth" or actor.actor_type != "USER":
            raise HTTPException(403, "请使用成员身份登录")
        bindings = confirmed_task_bindings(self.cp.store.connection, identity.tenant_id)
        counts = Counter((b["task"]["task_run_id"], b["call"]["remote_ref"]) for b in bindings)
        return [b for b in bindings if b["task"]["requested_by"] == identity.actor_id
                and counts[(b["task"]["task_run_id"], b["call"]["remote_ref"])] == 1]

    def organizer_tasks(self, identity: IdentityContext) -> list[dict[str, Any]]:
        items = []
        for binding in self.organizer_bindings(identity):
            task, call, proposal = binding["task"], binding["call"], binding["proposal"]
            remote = latest_remote_observation(self.cp.store, task["task_run_id"], call["remote_ref"])
            correction = correction_for_task(self.cp.store, identity.tenant_id, call["remote_ref"])
            approved_due = proposal["arguments"].get("due_date")
            mismatch = bool(remote and remote_due_mismatch(approved_due, remote))
            correction_stale = bool(correction and remote and correction["status"] == "PENDING"
                                    and correction["observation_id"] != remote["observation_id"])
            items.append({
                "task_ref": self.owner_task_ref(binding), "title": proposal["arguments"].get("title", "会议待办"),
                "approved_due_date": approved_due, "remote_due_date": (remote.get("due_at") or "")[:10] if remote else None,
                "remote_status": remote.get("raw_status") if remote else None,
                "observation_id": remote.get("observation_id") if remote else None,
                "source_revision": proposal["proposal_hash"], "due_mismatch": mismatch,
                "correction": ({"correction_id": correction["correction_id"], "status": correction["status"],
                                "proposal_hash": correction["proposal_hash"]} if correction else None),
                "can_propose_correction": mismatch and remote.get("raw_status") == "TODO"
                    and (not correction or correction["status"] in {"FAILED", "SUCCEEDED", "INVALIDATED"}
                         or correction_stale),
            })
        return items

    def _organizer_binding(self, task_ref: str, identity: IdentityContext) -> dict[str, Any]:
        binding = next((b for b in self.organizer_bindings(identity) if self.owner_task_ref(b) == task_ref), None)
        if binding is None:
            raise HTTPException(404, "任务不存在或无权处理")
        return binding

    def propose_due_correction(self, task_ref: str, identity: IdentityContext,
                               body: DueCorrectionRequest) -> dict[str, Any]:
        binding = self._organizer_binding(task_ref, identity)
        if binding["proposal"]["proposal_hash"] != body.source_revision:
            raise HTTPException(409, "任务版本已变化，请刷新后核对")
        remote = latest_remote_observation(self.cp.store, binding["task"]["task_run_id"], binding["call"]["remote_ref"])
        if remote is None or remote["observation_id"] != body.observation_id:
            raise HTTPException(409, "飞书任务观察已变化，请刷新后核对")
        try:
            correction = propose_due_correction(self.cp.store, binding, identity)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"correction_id": correction["correction_id"], "status": correction["status"],
                "proposal_hash": correction["proposal_hash"], "approved_due_date": correction["approved_due_date"],
                "remote_due_date": (correction["observed_due_at"] or "")[:10], "remote_write": False}

    def approve_due_correction(self, task_ref: str, correction_id: str, identity: IdentityContext,
                               body: DueCorrectionApproval) -> dict[str, Any]:
        binding = self._organizer_binding(task_ref, identity)
        correction = correction_for_task(self.cp.store, identity.tenant_id, binding["call"]["remote_ref"])
        if not correction or correction["correction_id"] != correction_id:
            raise HTTPException(404, "改期提案不存在")
        try:
            result = approve_due_correction(self.cp.store, correction_id, body.proposal_hash, identity)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": result["status"], "remote_write": False}

    def decide_owner_task(self, task_ref: str, identity: IdentityContext, body: OwnerDecisionRequest) -> dict[str, Any]:
        binding = next((b for b in self.owner_bindings(identity) if self.owner_task_ref(b) == task_ref), None)
        if binding is None:
            raise HTTPException(404, "任务不存在或未分配给你")
        result = apply_owner_decision_event(self.cp.store, OwnerDecisionEvent(
            event_id=body.event_id, tenant_id=identity.tenant_id,
            task_run_id=binding["task"]["task_run_id"], remote_task_id=binding["call"]["remote_ref"],
            owner_actor_id=identity.actor_id, decision=body.decision,
            source_revision=body.source_revision, reason=body.reason,
        ), identity=identity)
        if not result["accepted"]:
            messages = {
                "RETURN_REASON_REQUIRED": (422, "请填写退回原因"),
                "OWNER_ACTION_STALE": (409, "任务版本已更新，请重新加载后核对"),
                "OWNER_EVENT_PAYLOAD_MISMATCH": (409, "这次提交的内容已改变，请重新加载后核对"),
                "OWNER_DECISION_ALREADY_RECORDED": (409, "该任务已有确认结果，请重新加载查看"),
                "OWNER_TASK_REQUIRES_RECONCILIATION": (409, "任务状态需要核验，暂不能接受或退回"),
                "OWNER_DUE_RECONCILIATION_REQUIRED": (409, "飞书任务截止日期与批准日期不一致，请先核验"),
            }
            code, message = messages.get(result["reason"], (403, "没有本次操作的权限，请重新登录核对"))
            raise HTTPException(code, message)
        return {key: result.get(key, False) for key in
                ("accepted", "status", "duplicate", "remote_write", "notification_sent")}

    def apply(self, record_id: str, identity: IdentityContext, request: ReviewRequest, *, submit: bool) -> dict[str, Any]:
        current = self.detail(record_id, identity)
        if not secrets.compare_digest(current["snapshot"], request.snapshot):
            raise HTTPException(409, "草稿已更新，请重新加载后核对")
        if current["task_status"] != TaskRunStatus.WAITING_REVIEW:
            raise HTTPException(409, "本次会议已提交或已结束，不能继续修改")
        record = self.owned_record(record_id, identity)
        updates = {ref: edit.model_dump(exclude_unset=True) for ref, edit in request.updates.items()}
        member_ids = {m["actor_id"] for m in self.cp.store.list_bound_members(identity.tenant_id)}
        if any(edit.get("assignee_actor_id") and edit["assignee_actor_id"] not in member_ids
               for edit in updates.values()):
            raise HTTPException(422, "负责人必须是本企业已绑定的有效成员")
        if any(edit.get("title", "") is None for edit in updates.values()):
            raise HTTPException(422, "待办标题不能为空")
        if submit:
            source = self.cp.store.get_meeting_source(record_id)
            if source is None:
                raise HTTPException(409, "来源快照缺失，无法提交")
            self.gateway.submit(
                task_run_id=record["task_run_id"], record_id=record_id,
                document_id=source["document_id"], reviewer_identity=identity,
                updates_by_todo_ref=updates,
            )
        else:
            workflow = self.gateway.orchestrator.meeting
            by_id = {workflow.resolve_review_todo_ref(record_id, ref): edit for ref, edit in updates.items()}
            drafts = [workflow.prepare_draft_revision(record_id, draft["todo_id"], **by_id.get(draft["todo_id"], {}))
                      for draft in self.cp.store.list_meeting_todos(record_id)]
            self.cp.store.save_meeting_todos(drafts)
            self.cp._audit("MEETING_REVIEW_SAVED", identity.tenant_id, identity.actor_id,
                           record["task_run_id"], {"record_id": record_id, "todo_count": len(drafts)}, "SAVED")
        return self.detail(record_id, identity)


def build_meeting_review_router(
    service: MeetingReviewWebService, sessions: SQLiteOAuthSessionIssuer, *, public_origin: str,
) -> APIRouter:
    router = APIRouter(prefix="/api/meetings")
    require_identity = build_session_dependency(sessions)

    def check_csrf(request: Request):
        origin = request.headers.get("origin")
        expected = csrf_token(request.cookies.get("fde_auth_session", ""))
        if (origin is not None and origin != public_origin) or not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), expected,
        ):
            raise HTTPException(403, "请求校验失败，请重新加载页面")

    @router.post("/logout")
    def logout(request: Request, identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        sessions.revoke(request.cookies["fde_auth_session"])
        response = JSONResponse({"status": "LOGGED_OUT"})
        response.delete_cookie(
            "fde_auth_session", path="/", secure=public_origin.startswith("https://"),
            httponly=True, samesite="lax",
        )
        response.delete_cookie(
            "fde_oauth_browser", path="/", secure=public_origin.startswith("https://"),
            httponly=True, samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/logged-out", response_class=HTMLResponse)
    def logged_out():
        return HTMLResponse(
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>已退出工作台</title>"
            "<main style='font:16px/1.7 sans-serif;max-width:38rem;margin:4rem auto;padding:1.5rem'>"
            "<h1>已退出工作台</h1>"
            "<p>当前工作台会话已结束。飞书账号仍可能保持登录；切换账号时，请先在飞书中切换账号，或使用另一个浏览器资料。</p>"
            "<p><a href='/oauth/start'>重新登录工作台</a></p></main></html>",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("")
    def list_meetings(request: Request, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            records = service.cp.store.list_member_meetings(identity.tenant_id, identity.actor_id)
            items = []
            for record in records:
                source = service.cp.store.get_meeting_source(record["record_id"])
                task = service.cp._task(record["task_run_id"])
                items.append({"record_id": record["record_id"], "title": (source or {}).get("title", "会议纪要"),
                              "revision_id": record["revision_id"], "status": task.status})
            members = service.cp.store.list_bound_members(identity.tenant_id)
            for member in members:
                member["name"] = member["name"] or ("当前成员" if member["actor_id"] == identity.actor_id else member["actor_id"])
            return {"meetings": items, "members": members,
                    "csrf_token": csrf_token(request.cookies["fde_auth_session"])}

    @router.get("/digest")
    def get_digest(business_date: str | None = None, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.daily_follow_up_digest(identity, business_date)

    @router.post("/start")
    def start_meeting(body: MeetingStartRequest, request: Request,
                      identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.start_meeting(identity, body)

    @router.get("/agents")
    def get_agents(identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.agent_registry(identity)

    @router.get("/agents/{agent_id}/diagnostics")
    def agent_diagnostics(agent_id: str, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.agent_diagnostics(agent_id, identity)

    @router.get("/agents/{agent_id}/canary-runs")
    def agent_canary_runs(agent_id: str, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.agent_canary_runs(agent_id, identity)

    @router.get("/agents/{agent_id}/promotion-readiness")
    def agent_promotion_readiness(agent_id: str, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.agent_promotion_readiness(agent_id, identity)

    @router.get("/agents/{agent_id}/promotion-history")
    def agent_promotion_history(agent_id: str, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.agent_promotion_history(agent_id, identity)

    @router.post("/agents/{agent_id}/canary-runs/{task_run_id}/review")
    def review_agent_canary_run(agent_id: str, task_run_id: str, body: AgentCanaryReviewRequest,
                                request: Request, identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.review_agent_canary_run(agent_id, task_run_id, identity, body)

    @router.get("/agents/{agent_id}/compare")
    def compare_agent(agent_id: str, old_version: int, new_version: int,
                      identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.agent_comparison(agent_id, old_version, new_version, identity)

    @router.post("/agents/{agent_id}/pause")
    def pause_agent(agent_id: str, body: AgentPauseRequest, request: Request,
                    identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.pause_agent(agent_id, identity, body)

    @router.post("/agents/{agent_id}/resume")
    def resume_agent(agent_id: str, body: AgentResumeRequest, request: Request,
                     identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.resume_agent(agent_id, identity, body)

    @router.post("/agents/{agent_id}/volunteer")
    def volunteer_agent(agent_id: str, body: AgentVolunteerRequest, request: Request,
                        identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.set_agent_volunteer(agent_id, identity, body)

    @router.post("/agents/{agent_id}/{action}")
    def transition_agent(agent_id: str, action: Literal["canary", "promote", "rollback", "archive"],
                         body: AgentRouteRequest, request: Request,
                         identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.transition_agent(agent_id, action.upper(), identity, body)

    @router.get("/agent-drafts")
    def get_agent_drafts(request: Request, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return {"drafts": service.agent_config_drafts(identity),
                    "can_precheck": service.can_precheck_agent_config(identity),
                    "can_confirm": service.can_confirm_agent_config(identity),
                    "csrf_token": csrf_token(request.cookies["fde_auth_session"])}

    @router.get("/member-access-requests")
    def member_access_requests(request: Request, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return {"requests": service.pending_member_access(identity),
                    "csrf_token": csrf_token(request.cookies["fde_auth_session"])}

    @router.get("/member-directory")
    def member_directory(identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return {"members": service.member_directory(identity)}

    @router.post("/member-directory/{actor_id}/status")
    def set_member_status(actor_id: str, body: MemberStatusRequest, request: Request,
                          identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.set_member_status(actor_id, identity, body)

    @router.post("/member-directory/{actor_id}/reviewer-role")
    def set_member_reviewer_role(actor_id: str, body: MemberReviewerRoleRequest, request: Request,
                                 identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.set_member_reviewer_role(actor_id, identity, body)

    @router.get("/audit")
    def get_audit(request: Request, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            result = service.audit_events(identity)
            if result["can_view"]:
                result["csrf_token"] = csrf_token(request.cookies["fde_auth_session"])
            return result

    @router.post("/audit/{audit_ref}/triage")
    def triage_audit(audit_ref: str, body: AuditTriageRequest, request: Request,
                     identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.triage_audit(identity, audit_ref, body)

    @router.post("/member-access-requests/{request_id}/approve")
    def approve_member_access(request_id: str, request: Request,
                              identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.approve_member_access(request_id, identity)

    @router.post("/agent-drafts")
    def create_agent_draft(body: AgentConfigDraftRequest, request: Request,
                           identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.create_agent_config_draft(identity, body)

    @router.post("/agent-evaluation-datasets")
    def create_agent_eval_dataset(body: AgentEvaluationDatasetRequest, request: Request,
                                  identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.create_agent_eval_dataset(identity, body)

    @router.post("/agent-drafts/{draft_id}/precheck")
    def precheck_agent_draft(draft_id: str, request: Request,
                             identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.precheck_agent_config_draft(draft_id, identity)

    @router.post("/agent-drafts/{draft_id}/evaluate")
    def evaluate_agent_draft(draft_id: str, body: AgentEvaluationRequest, request: Request,
                             identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.evaluate_agent_draft(draft_id, identity, body)

    @router.post("/agent-drafts/{draft_id}/confirm")
    def confirm_agent_draft(draft_id: str, body: AgentConfigApprovalRequest, request: Request,
                            identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.confirm_agent_config_draft(draft_id, identity, body)

    @router.get("/owner-tasks")
    def get_owner_tasks(request: Request, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return {"tasks": service.owner_tasks(identity),
                    "csrf_token": csrf_token(request.cookies["fde_auth_session"])}

    @router.get("/organizer-tasks")
    def get_organizer_tasks(request: Request, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return {"tasks": service.organizer_tasks(identity),
                    "csrf_token": csrf_token(request.cookies["fde_auth_session"])}

    @router.post("/organizer-tasks/{task_ref}/due-correction")
    def propose_correction(task_ref: str, body: DueCorrectionRequest, request: Request,
                           identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.propose_due_correction(task_ref, identity, body)

    @router.post("/organizer-tasks/{task_ref}/due-correction/{correction_id}/approve")
    def approve_correction(task_ref: str, correction_id: str, body: DueCorrectionApproval, request: Request,
                           identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.approve_due_correction(task_ref, correction_id, identity, body)

    @router.post("/owner-tasks/{task_ref}/decision")
    def owner_decision(task_ref: str, body: OwnerDecisionRequest, request: Request,
                       identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.decide_owner_task(task_ref, identity, body)

    @router.get("/{record_id}")
    def get_meeting(record_id: str, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.detail(record_id, identity)

    def mutate(record_id: str, body: ReviewRequest, request: Request, identity: IdentityContext, submit: bool):
        check_csrf(request)
        with service.lock:
            try:
                return service.apply(record_id, identity, body, submit=submit)
            except HTTPException:
                raise
            except PermissionError as exc:
                raise HTTPException(403, "没有本次操作的权限") from exc
            except KeyError as exc:
                raise HTTPException(422, "待办或负责人不存在") from exc
            except ValueError as exc:
                messages = {
                    "source document changed since Agent extraction": "源文档已更新，请重新提取后审核",
                    "all todos need human confirmation before proposal creation": "请补齐负责人、截止日期并处理冲突",
                }
                raise HTTPException(409, messages.get(str(exc), "草稿修改未通过校验，请核对字段和冲突")) from exc
            except RuntimeError as exc:
                raise HTTPException(503, "飞书文档版本暂时无法核验，请稍后重试") from exc

    @router.post("/{record_id}/save")
    def save(record_id: str, body: ReviewRequest, request: Request, identity: IdentityContext = Depends(require_identity)):
        return mutate(record_id, body, request, identity, False)

    @router.post("/{record_id}/submit")
    def submit(record_id: str, body: ReviewRequest, request: Request, identity: IdentityContext = Depends(require_identity)):
        return mutate(record_id, body, request, identity, True)

    return router
