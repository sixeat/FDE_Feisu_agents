"""Authenticated meeting review UI/API; never executes remote writes."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter
from datetime import date
from threading import RLock
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from .feishu_oauth_web import SQLiteOAuthSessionIssuer, build_session_dependency
from .feishu_review import FeishuMeetingReviewGateway
from .follow_up import (
    OwnerDecisionEvent, apply_owner_decision_event, build_daily_follow_up_digest, confirmed_task_bindings,
    remote_due_mismatch,
)
from .meeting import review_todo_ref
from .models import ActorType, IdentityContext, TaskRunStatus
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


class AgentConfigApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: StrictStr = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    input_hash: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


AGENT_REVIEWER_ROLES = frozenset({"admin", "agent_reviewer"})
AGENT_ADMIN_ROLES = frozenset({"admin"})
KNOWN_AGENT_CAPABILITIES = frozenset({
    "doc.read", "doc.write", "doc.delete", "task.read", "task.write", "task.delete",
    "message.send",
})
HIGH_RISK_AGENT_CAPABILITIES = frozenset({"doc.write", "doc.delete", "task.write", "task.delete", "message.send"})


class DueCorrectionApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_hash: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class MeetingReviewWebService:
    def __init__(self, gateway: FeishuMeetingReviewGateway) -> None:
        self.gateway = gateway
        self.cp = gateway.control_plane
        self.lock = RLock()

    def agent_registry(self, identity: IdentityContext) -> dict[str, Any]:
        """Return a tenant-scoped, read-only directory of managed agents."""
        self.cp.verify_identity(identity)
        actors = self.cp.store.list_agent_actors(identity.tenant_id)
        versions = self.cp.store.list_agent_versions([row["actor_id"] for row in actors])
        by_agent: dict[str, list[Any]] = {}
        for version in versions:
            by_agent.setdefault(version["agent_id"], []).append(version)
        items = []
        for actor in actors:
            history = by_agent.get(actor["actor_id"], [])
            current = next((version for version in history if version["active"]), None)
            items.append({
                "agent_id": actor["actor_id"],
                "actor_type": actor["actor_type"],
                "status": "PAUSED" if not actor["active"] else ("UNVERSIONED" if current is None else "ACTIVE"),
                "current_version": int(current["version"]) if current is not None else None,
                "version_count": len(history),
                "capabilities": sorted(json.loads(current["capabilities_json"])) if current is not None else [],
                "skill_version": current["skill_version"] if current is not None else None,
            })
        return {"agents": items}

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

    def pending_member_access(self, identity: IdentityContext) -> list[dict[str, Any]]:
        if not self.can_confirm_agent_config(identity):
            raise HTTPException(403, "只有管理员可以查看成员接入申请")
        return [{"request_id": row["request_id"], "created_at": row["created_at"]}
                for row in self.cp.store.list_pending_member_access(identity.tenant_id)]

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
        previous = self.cp.store.get_latest_agent_config_review(draft_id, identity.tenant_id)
        if previous is not None and previous["input_hash"] == input_hash:
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
        if any(str(item["actor_id"]).casefold() == str(data["name"]).casefold() for item in existing):
            blockers.append("AGENT_NAME_CONFLICT")
        high_risk = sorted(capabilities & HIGH_RISK_AGENT_CAPABILITIES)
        if high_risk:
            warnings.append("HIGH_RISK_CAPABILITY:" + ",".join(high_risk))
        if data.get("actor_type") == "MANAGEMENT_AGENT":
            warnings.append("MANAGEMENT_AGENT_REQUIRES_ADMIN_SCOPE_REVIEW")
        status = "BLOCKED" if blockers else "READY_FOR_ADMIN"
        report = {
            "input_hash": input_hash, "blockers": blockers, "warnings": warnings,
            "requested_capabilities": sorted(capabilities), "reviewer_role": sorted(actor.roles & AGENT_REVIEWER_ROLES),
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
        try:
            result = self.cp.store.publish_agent_config_draft(
                draft_id=draft_id, tenant_id=identity.tenant_id, approver_id=identity.actor_id,
                review_id=body.review_id, input_hash=input_hash, agent_id=data["name"],
                actor_type=ActorType(data["actor_type"]).value, capabilities=list(data.get("capabilities") or []),
                skill_version=data.get("skill_version"), approval_id="agent-approval-" + secrets.token_hex(10),
            )
        except ValueError as exc:
            reasons = {
                "DRAFT_NOT_FOUND": (404, "配置草稿不存在或无权访问"),
                "DRAFT_ALREADY_PUBLISHED": (409, "配置草稿已经发布"),
                "PRECHECK_VERSION_STALE": (409, "审核预审版本已变化，请重新运行审核预审"),
                "PRECHECK_NOT_READY": (409, "审核预审未通过，不能发布 Agent"),
                "AGENT_NAME_CONFLICT": (409, "Agent 名称已存在，请修改草稿后重新预审"),
            }
            code, message = reasons.get(str(exc), (409, "Agent 配置发布未完成，请重新核对"))
            raise HTTPException(code, message) from exc
        self.cp._audit("AGENT_CONFIG_CONFIRMED", identity.tenant_id, identity.actor_id, None,
                       {"draft_id": draft_id, "approval_id": result["approval_id"],
                        "agent_id": data["name"], "version": result["version"]}, result["status"])
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
                "capabilities": capabilities, "skill_version": body.skill_version.strip() if body.skill_version else None}
        self.cp.store.save_agent_config_draft(draft_id, identity.tenant_id, identity.actor_id, "PENDING_REVIEW", data)
        self.cp._audit("AGENT_CONFIG_DRAFT_CREATED", identity.tenant_id, identity.actor_id, None,
                       {"draft_id": draft_id, "actor_type": body.actor_type, "capability_count": len(capabilities)},
                       "PENDING_REVIEW")
        return {"draft_id": draft_id, "status": "PENDING_REVIEW", "name": name}

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
        snapshot = hashlib.sha256(json.dumps(
            {"record": record, "task_status": task.status, "drafts": drafts},
            sort_keys=True, ensure_ascii=True,
        ).encode()).hexdigest()
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
                member["name"] = "当前成员" if member["actor_id"] == identity.actor_id else member["actor_id"]
            return {"meetings": items, "members": members,
                    "csrf_token": csrf_token(request.cookies["fde_auth_session"])}

    @router.get("/digest")
    def get_digest(business_date: str | None = None, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.daily_follow_up_digest(identity, business_date)

    @router.get("/agents")
    def get_agents(identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return service.agent_registry(identity)

    @router.get("/agent-drafts")
    def get_agent_drafts(request: Request, identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return {"drafts": service.agent_config_drafts(identity),
                    "can_precheck": service.can_precheck_agent_config(identity),
                    "can_confirm": service.can_confirm_agent_config(identity),
                    "csrf_token": csrf_token(request.cookies["fde_auth_session"])}

    @router.get("/member-access-requests")
    def member_access_requests(identity: IdentityContext = Depends(require_identity)):
        with service.lock:
            return {"requests": service.pending_member_access(identity)}

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

    @router.post("/agent-drafts/{draft_id}/precheck")
    def precheck_agent_draft(draft_id: str, request: Request,
                             identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        with service.lock:
            return service.precheck_agent_config_draft(draft_id, identity)

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
