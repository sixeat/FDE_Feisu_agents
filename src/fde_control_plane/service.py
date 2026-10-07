"""M1 control-plane service.

This module owns authorization, approval binding and durable side-effect
state. It intentionally has no model or Feishu SDK dependency; a future
adapter consumes the outbox after the same fences have been checked.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from .models import (
    Actor,
    ActorType,
    ApprovalRequest,
    ApprovalStatus,
    AgentVersion,
    AssistantBinding,
    AuditEvent,
    Delegation,
    IdentityContext,
    OperationProposal,
    RiskLevel,
    TaskRun,
    TaskStep,
    TaskRunStatus,
    ToolCall,
    ToolCallStatus,
    ToolDefinition,
    ToolPolicy,
)
from .contract import ContractValidation, validate_meeting_contract
from .store import SQLiteStore


_SECRET_KEYS = {"token", "access_token", "refresh_token", "tenant_access_token", "user_access_token",
                "secret", "app_secret", "password", "authorization", "cookie", "access_key", "ticket", "credential"}
_SECRET_TEXT = re.compile(r"(?i)(access[_-]?key|access[_-]?token|refresh[_-]?token|tenant[_-]?access[_-]?token|user[_-]?access[_-]?token|app[_-]?secret|password|authorization|ticket)=([^&\s]+)")


def _safe(value: Any, key: str | None = None) -> Any:
    if key and (key.lower() in _SECRET_KEYS or any(part in key.lower() for part in ("token", "secret", "password", "access_key"))):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(v) for v in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    return value


def _hash(value: Any) -> str:
    payload = json.dumps(_safe(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    requires_approval: bool
    permission_intersection: frozenset[str]
    reason: str


class ControlPlane:
    def __init__(self, store: SQLiteStore | None = None) -> None:
        self.store = store or SQLiteStore()

    def close(self) -> None:
        self.store.close()

    def validate_meeting_agent_result(
        self,
        payload: dict[str, Any],
        *,
        document_id_hash: str,
        revision_id: str | int,
        valid_block_ids: set[int] | frozenset[int],
    ) -> ContractValidation:
        """Validate Agent evidence without granting it write authority."""
        result = validate_meeting_contract(
            payload,
            document_id_hash=document_id_hash,
            revision_id=revision_id,
            valid_block_ids=valid_block_ids,
        )
        task_id = payload.get("task_run_id")
        if task_id:
            task = self._task(task_id)
            self._audit("MEETING_CONTRACT_VALIDATED", task.tenant_id, task.requested_by, task_id,
                        {"status": result.status, "reason_count": len(result.reasons), "todo_count": result.todo_count}, result.status)
        return result

    def register_actor(self, actor: Actor) -> None:
        self.store.save_actor(actor)

    def register_agent_version(self, version: AgentVersion) -> None:
        if version.active:
            self.store.deactivate_other_agent_versions(version.agent_id, version.version)
        self.store.save_agent_version(version)

    def bind_assistant(self, binding: AssistantBinding) -> None:
        member = self._actor(binding.member_actor_id) if self.store.get_actor(binding.member_actor_id) else None
        assistant = self._actor(binding.assistant_actor_id) if self.store.get_actor(binding.assistant_actor_id) else None
        if member is None or assistant is None or member.tenant_id != binding.tenant_id or assistant.tenant_id != binding.tenant_id:
            raise PermissionError("assistant binding identities must belong to the same tenant")
        if member.actor_type != ActorType.USER or assistant.actor_type != ActorType.PERSONAL_ASSISTANT:
            raise PermissionError("binding must connect a user to a personal assistant")
        self.store.save_binding(binding)

    def verify_identity(self, context: IdentityContext, required_scope: str | None = None) -> Actor:
        actor = self._actor(context.actor_id)
        if not actor.active or actor.tenant_id != context.tenant_id:
            raise PermissionError("identity is inactive or belongs to another tenant")
        if context.auth_mode not in {"user_oauth", "application"}:
            raise PermissionError("unsupported authentication mode")
        if actor.external_ref_hash and actor.external_ref_hash != context.subject_ref_hash:
            raise PermissionError("identity subject does not match actor binding")
        if required_scope and required_scope not in context.scopes:
            raise PermissionError("identity is missing required scope")
        return actor

    def _actor(self, actor_id: str) -> Actor:
        row = self.store.get_actor(actor_id)
        if row is None:
            raise KeyError(f"unknown actor: {actor_id}")
        return Actor(actor_id=row["actor_id"], tenant_id=row["tenant_id"], actor_type=ActorType(row["actor_type"]),
                     capabilities=frozenset(json.loads(row["capabilities_json"])), roles=frozenset(json.loads(row["roles_json"])),
                     external_ref_hash=row["external_ref_hash"], active=bool(row["active"]))

    def _agent_version(self, agent_id: str, version: int) -> AgentVersion:
        row = self.store.get_agent_version(agent_id, version)
        if row is None:
            raise KeyError(f"unknown agent version: {agent_id}@{version}")
        return AgentVersion(agent_id=agent_id, version=version, capabilities=frozenset(json.loads(row["capabilities_json"])),
                            active=bool(row["active"]), skill_version=row["skill_version"])

    def create_task_run(self, requested_by: str, assistant_actor_id: str, agent_id: str, agent_version: int,
                        tenant_id: str | None = None, input_ref_hash: str | None = None,
                        task_run_id: str | None = None, identity: IdentityContext | None = None,
                        dispatch_key: str | None = None) -> TaskRun:
        if identity is not None:
            self.verify_identity(identity)
            if identity.actor_id != requested_by:
                raise PermissionError("identity actor does not match task requester")
        user = self._actor(requested_by)
        assistant = self._actor(assistant_actor_id)
        version = self._agent_version(agent_id, agent_version)
        tenant = tenant_id or user.tenant_id
        if not user.active or not assistant.active or not version.active:
            raise PermissionError("inactive actor or agent version")
        if user.tenant_id != tenant or assistant.tenant_id != tenant:
            raise PermissionError("tenant mismatch")
        binding = self.store.get_binding(requested_by, tenant)
        if binding is not None and binding["assistant_actor_id"] != assistant_actor_id:
            raise PermissionError("assistant is not bound to member")
        task = TaskRun(task_run_id=task_run_id or _id("run"), tenant_id=tenant, requested_by=requested_by,
                       assistant_actor_id=assistant_actor_id, agent_id=agent_id, agent_version=agent_version,
                       status=TaskRunStatus.QUEUED, input_ref_hash=input_ref_hash)
        if dispatch_key:
            dispatch_data = {
                "requested_by": requested_by,
                "assistant_actor_id": assistant_actor_id,
                "agent_id": agent_id,
                "agent_version": agent_version,
                "tenant_id": tenant,
                "input_ref_hash": input_ref_hash,
            }
            if not self.store.save_task_run_with_dispatch(task, dispatch_key, dispatch_data):
                existing = self.store.get_dispatch(dispatch_key)
                if existing is None:
                    raise RuntimeError("dispatch claim lost without a persisted owner")
                if any(existing.get(key) != value for key, value in dispatch_data.items()):
                    raise PermissionError("dispatch key is already bound to a different request")
                return self._task(existing["task_run_id"])
        else:
            self.store.save_task_run(task)
        self._audit("TASK_CREATED", tenant, requested_by, task.task_run_id, {"agent_id": agent_id, "agent_version": agent_version}, "ACCEPTED")
        return task

    def create_task_step(self, step: TaskStep) -> TaskStep:
        task = self._task(step.task_run_id)
        if task.tenant_id != self._actor(task.requested_by).tenant_id:
            raise PermissionError("task tenant is invalid")
        self.store.save_task_step(step)
        task.current_step_id = step.step_id
        self.store.save_task_run(task)
        return step

    def delegate(self, delegation: Delegation) -> Delegation:
        task = self._task(delegation.task_run_id)
        source, target = self._actor(delegation.from_actor_id), self._actor(delegation.to_actor_id)
        if not source.active or not target.active or source.tenant_id != task.tenant_id or target.tenant_id != task.tenant_id:
            raise PermissionError("delegation actors must be active members of the task tenant")
        if delegation.from_actor_id != task.assistant_actor_id:
            raise PermissionError("delegation source must be the task personal assistant")
        if delegation.to_actor_id != task.agent_id or target.actor_type != ActorType.BUSINESS_AGENT:
            raise PermissionError("delegation target must be the task business agent")
        version = self._agent_version(task.agent_id, task.agent_version)
        allowed_scope = source.capabilities & target.capabilities & version.capabilities
        if not delegation.scope or not delegation.scope.issubset(allowed_scope):
            raise PermissionError("delegation exceeds assistant, agent, or version capabilities")
        self.store.save_delegation(delegation)
        self._audit("DELEGATION_CREATED", task.tenant_id, delegation.from_actor_id, task.task_run_id,
                    {"delegation_id": delegation.delegation_id, "to_actor_id": delegation.to_actor_id,
                     "scope": sorted(delegation.scope)}, "ACCEPTED")
        return delegation

    def transition_task(self, task_run_id: str, status: TaskRunStatus, reason: str | None = None) -> TaskRun:
        task = self._task(task_run_id)
        allowed = {
            TaskRunStatus.RECEIVED: {TaskRunStatus.QUEUED, TaskRunStatus.CANCELLED},
            TaskRunStatus.QUEUED: {TaskRunStatus.PLANNING, TaskRunStatus.CANCELLED},
            TaskRunStatus.PLANNING: {TaskRunStatus.RUNNING, TaskRunStatus.WAITING_APPROVAL, TaskRunStatus.FAILED},
            TaskRunStatus.WAITING_APPROVAL: {TaskRunStatus.RUNNING, TaskRunStatus.CANCELLED, TaskRunStatus.PAUSED},
            TaskRunStatus.RUNNING: {TaskRunStatus.WAITING_REVIEW, TaskRunStatus.WAITING_APPROVAL, TaskRunStatus.RECONCILING, TaskRunStatus.SUCCEEDED,
                                    TaskRunStatus.PARTIAL_SUCCESS, TaskRunStatus.FAILED, TaskRunStatus.PAUSED},
            TaskRunStatus.WAITING_REVIEW: {TaskRunStatus.WAITING_APPROVAL, TaskRunStatus.CANCELLED, TaskRunStatus.PAUSED},
            TaskRunStatus.RECONCILING: {TaskRunStatus.RUNNING, TaskRunStatus.PAUSED, TaskRunStatus.SUCCEEDED, TaskRunStatus.FAILED},
            TaskRunStatus.PAUSED: {TaskRunStatus.QUEUED, TaskRunStatus.CANCELLED},
        }
        if status != task.status and status not in allowed.get(task.status, set()):
            raise ValueError(f"invalid task transition {task.status.value}->{status.value}")
        task.status = status
        if reason:
            task.failure_reason = reason
        self.store.save_task_run(task)
        return task

    def _task(self, task_run_id: str) -> TaskRun:
        data = self.store.get_task_run(task_run_id)
        if data is None:
            raise KeyError(f"unknown task run: {task_run_id}")
        return TaskRun(**{**data, "status": TaskRunStatus(data["status"])})

    def _refresh_task_status(self, task_run_id: str) -> TaskRun:
        task = self._task(task_run_id)
        calls = self.store.list_tool_calls(task_run_id)
        if not calls:
            return task
        statuses = [ToolCallStatus(item["status"]) for item in calls]
        if any(status in (ToolCallStatus.UNKNOWN, ToolCallStatus.RECONCILING) for status in statuses):
            task.status = TaskRunStatus.RECONCILING
        elif any(status in (ToolCallStatus.PREPARED, ToolCallStatus.DISPATCHED) for status in statuses):
            task.status = TaskRunStatus.RUNNING
        elif any(status == ToolCallStatus.FAILED for status in statuses):
            task.status = TaskRunStatus.PARTIAL_SUCCESS if any(status == ToolCallStatus.SUCCEEDED for status in statuses) else TaskRunStatus.FAILED
        else:
            task.status = TaskRunStatus.SUCCEEDED
        self.store.save_task_run(task)
        return task

    @staticmethod
    def _tool_definition(tool: ToolDefinition | ToolPolicy) -> ToolDefinition:
        if isinstance(tool, ToolPolicy):
            return ToolDefinition(tool.tool_name, tool.allowed_capabilities, tool.risk_level, tool.requires_approval)
        return tool

    def authorize_operation(self, task_run_id: str, tool: ToolDefinition | ToolPolicy) -> AuthorizationDecision:
        tool = self._tool_definition(tool)
        task = self._task(task_run_id)
        user = self._actor(task.requested_by)
        assistant = self._actor(task.assistant_actor_id)
        agent = self._actor(task.agent_id)
        version = self._agent_version(task.agent_id, task.agent_version)
        intersection = user.capabilities & assistant.capabilities & agent.capabilities & version.capabilities
        required = frozenset(tool.required_capabilities)
        missing = required - intersection
        if not all(actor.active for actor in (user, assistant, agent)) or not version.active:
            return AuthorizationDecision(False, False, intersection, "inactive actor or agent version")
        if missing:
            return AuthorizationDecision(False, False, intersection, f"missing capabilities: {','.join(sorted(missing))}")
        return AuthorizationDecision(True, tool.side_effect or tool.risk_level == RiskLevel.HIGH,
                                    intersection, "allowed")

    def create_operation_proposal(self, task_run_id: str, step_id: str, operation_type: str, target_ref: str,
                                  arguments: dict[str, Any], tool: ToolDefinition | ToolPolicy, operation_id: str | None = None,
                                  eligible_approver_roles: frozenset[str] = frozenset({"admin", "manager"}),
                                  eligible_approver_ids: frozenset[str] = frozenset()) -> OperationProposal:
        tool = self._tool_definition(tool)
        task = self._task(task_run_id)
        decision = self.authorize_operation(task_run_id, tool)
        if not decision.allowed:
            self._audit("OPERATION_DENIED", task.tenant_id, task.requested_by, task_run_id,
                        {"operation_type": operation_type, "reason": decision.reason}, "DENIED")
            raise PermissionError(decision.reason)
        safe_args = _safe(arguments)
        operation_id = operation_id or _id("op")
        base = {"operation_id": operation_id, "task_run_id": task_run_id, "step_id": step_id,
                "operation_type": operation_type, "target_ref": _safe(target_ref), "arguments": safe_args,
                "requested_by": task.requested_by, "agent_id": task.agent_id, "agent_version": task.agent_version,
                "risk_level": tool.risk_level.value, "required_capabilities": sorted(tool.required_capabilities),
                "permission_intersection": sorted(decision.permission_intersection), "action_version": 1}
        proposal = OperationProposal(
            operation_id=operation_id, task_run_id=task_run_id, step_id=step_id,
            operation_type=operation_type, target_ref=_safe(target_ref), arguments=safe_args,
            requested_by=task.requested_by, agent_id=task.agent_id, agent_version=task.agent_version,
            risk_level=tool.risk_level, required_capabilities=frozenset(tool.required_capabilities),
            permission_intersection=decision.permission_intersection, proposal_hash=_hash(base), action_version=1,
        )
        if decision.requires_approval:
            self.create_approval_request(proposal, eligible_approver_roles, eligible_approver_ids)
            task.status = TaskRunStatus.WAITING_APPROVAL
            self.store.save_task_run(task)
        else:
            self._audit("OPERATION_PROPOSED", task.tenant_id, task.requested_by, task_run_id, base, "ALLOWED")
        return proposal

    # Concise alias used by adapters.
    propose_operation = create_operation_proposal

    def create_approval_request(self, proposal: OperationProposal,
                                eligible_approver_roles: frozenset[str] = frozenset({"admin", "manager"}),
                                eligible_approver_ids: frozenset[str] = frozenset()) -> ApprovalRequest:
        if not eligible_approver_roles and not eligible_approver_ids:
            raise ValueError("approval requires an eligible role or actor")
        approval = ApprovalRequest(approval_id=_id("approval"), task_run_id=proposal.task_run_id, step_id=proposal.step_id,
                                   proposal=proposal, eligible_approver_roles=eligible_approver_roles,
                                   eligible_approver_ids=eligible_approver_ids)
        data = self._approval_data(approval)
        self.store.save_approval(approval, data)
        task = self._task(proposal.task_run_id)
        self._audit("APPROVAL_REQUESTED", task.tenant_id, proposal.requested_by, proposal.task_run_id,
                    {"approval_id": approval.approval_id, "proposal_hash": proposal.proposal_hash}, "PENDING")
        return approval

    def _approval_data(self, approval: ApprovalRequest) -> dict[str, Any]:
        proposal = asdict(approval.proposal)
        for key in ("risk_level",):
            if hasattr(proposal[key], "value"):
                proposal[key] = proposal[key].value
        proposal["required_capabilities"] = sorted(proposal["required_capabilities"])
        proposal["permission_intersection"] = sorted(proposal["permission_intersection"])
        return {"approval_id": approval.approval_id, "task_run_id": approval.task_run_id, "step_id": approval.step_id,
                "proposal": _safe(proposal), "eligible_approver_roles": sorted(approval.eligible_approver_roles),
                "eligible_approver_ids": sorted(approval.eligible_approver_ids),
                "status": approval.status.value, "decided_by": approval.decided_by, "decision_reason": approval.decision_reason}

    def _approval_from_row(self, row: Any) -> ApprovalRequest:
        data = json.loads(row["data_json"])
        p = data["proposal"]
        proposal = OperationProposal(operation_id=p["operation_id"], task_run_id=p["task_run_id"], step_id=p["step_id"],
                                     operation_type=p["operation_type"], target_ref=p["target_ref"], arguments=p["arguments"],
                                     requested_by=p["requested_by"], agent_id=p["agent_id"], agent_version=int(p["agent_version"]),
                                     risk_level=RiskLevel(p["risk_level"]), required_capabilities=frozenset(p["required_capabilities"]),
                                     permission_intersection=frozenset(p["permission_intersection"]), proposal_hash=p["proposal_hash"],
                                     action_version=int(p["action_version"]))
        return ApprovalRequest(approval_id=data["approval_id"], task_run_id=data["task_run_id"], step_id=data["step_id"], proposal=proposal,
                               eligible_approver_roles=frozenset(data["eligible_approver_roles"]), status=ApprovalStatus(data["status"]),
                               eligible_approver_ids=frozenset(data.get("eligible_approver_ids") or []),
                               decided_by=data.get("decided_by"), decision_reason=data.get("decision_reason"))

    def get_approval_for_operation(self, operation_id: str) -> ApprovalRequest | None:
        for row in self.store.list_approvals():
            approval = self._approval_from_row(row)
            if approval.proposal.operation_id == operation_id:
                return approval
        return None

    def expire_approval(self, approval_id: str, reason: str = "approval expired") -> ApprovalRequest:
        row = self.store.get_approval(approval_id)
        if row is None:
            raise KeyError(approval_id)
        approval = self._approval_from_row(row)
        if approval.status == ApprovalStatus.PENDING:
            approval.status = ApprovalStatus.EXPIRED
            approval.decision_reason = reason
            self.store.save_approval(approval, self._approval_data(approval))
            task = self._task(approval.task_run_id)
            if task.status == TaskRunStatus.WAITING_APPROVAL:
                task.status = TaskRunStatus.CANCELLED
                task.failure_reason = reason
                self.store.save_task_run(task)
            self._audit("APPROVAL_EXPIRED", task.tenant_id, None, task.task_run_id,
                        {"approval_id": approval_id}, "EXPIRED")
        return approval

    def revise_operation_proposal(self, approval_id: str, updates: dict[str, Any]) -> ApprovalRequest:
        """Invalidate the old action and create a new approval-bound version."""
        row = self.store.get_approval(approval_id)
        if row is None:
            raise KeyError(approval_id)
        old = self._approval_from_row(row)
        if old.status != ApprovalStatus.PENDING:
            raise ValueError("only a pending proposal can be revised")
        allowed = {"operation_type", "target_ref", "arguments", "required_capabilities", "permission_intersection", "risk_level"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported proposal fields: {','.join(sorted(unknown))}")
        values = {
            "operation_id": old.proposal.operation_id, "task_run_id": old.proposal.task_run_id,
            "step_id": old.proposal.step_id, "operation_type": old.proposal.operation_type,
            "target_ref": old.proposal.target_ref, "arguments": old.proposal.arguments,
            "requested_by": old.proposal.requested_by, "agent_id": old.proposal.agent_id,
            "agent_version": old.proposal.agent_version, "risk_level": old.proposal.risk_level,
            "required_capabilities": old.proposal.required_capabilities,
            "permission_intersection": old.proposal.permission_intersection,
            "action_version": old.proposal.action_version + 1,
        }
        values.update(updates)
        if isinstance(values["risk_level"], str):
            values["risk_level"] = RiskLevel(values["risk_level"])
        values["target_ref"] = _safe(values["target_ref"])
        values["arguments"] = _safe(values["arguments"])
        values["required_capabilities"] = frozenset(values["required_capabilities"])
        values["permission_intersection"] = frozenset(values["permission_intersection"])
        canonical = {**values, "risk_level": values["risk_level"].value if hasattr(values["risk_level"], "value") else values["risk_level"],
                     "required_capabilities": sorted(values["required_capabilities"]),
                     "permission_intersection": sorted(values["permission_intersection"])}
        new_proposal = OperationProposal(proposal_hash=_hash(canonical), **values)
        old.status = ApprovalStatus.INVALIDATED
        old.decision_reason = "proposal revised"
        self.store.save_approval(old, self._approval_data(old))
        return self.create_approval_request(new_proposal, old.eligible_approver_roles, old.eligible_approver_ids)

    def handle_approval_callback(self, event_id: str, approval_id: str, approver_id: str, approve: bool,
                                 action_version: int, proposal_hash: str, reason: str | None = None) -> dict[str, Any]:
        prior = self.store.get_inbox_event(event_id)
        if prior is not None:
            return {**prior, "duplicate": True}
        row = self.store.get_approval(approval_id)
        result: dict[str, Any]
        if row is None:
            result = {"accepted": False, "reason": "unknown approval"}
            self.store.record_inbox_event(event_id, result)
            return result
        approval = self._approval_from_row(row)
        task = self._task(approval.task_run_id)
        action_key = f"{approval_id}:{action_version}:{'APPROVE' if approve else 'REJECT'}"
        prior_action = self.store.get_approval_action(action_key)
        if prior_action is not None:
            self.store.record_inbox_event(event_id, prior_action)
            return {**prior_action, "duplicate": True}
        try:
            approver = self._actor(approver_id)
            version = self._agent_version(approval.proposal.agent_id, approval.proposal.agent_version)
            if approval.status != ApprovalStatus.PENDING:
                raise ValueError("approval is no longer pending")
            if action_version != approval.proposal.action_version:
                raise ValueError("stale action version")
            if proposal_hash != approval.proposal.proposal_hash:
                approval.status = ApprovalStatus.INVALIDATED
                raise ValueError("proposal hash mismatch")
            if (
                not approver.active
                or approver.tenant_id != task.tenant_id
                or not (approver.actor_id in approval.eligible_approver_ids
                        or approver.roles & approval.eligible_approver_roles)
            ):
                raise PermissionError("approver role is not eligible")
            if not version.active:
                raise ValueError("agent version is inactive")
            current_permission = self.authorize_operation(
                approval.task_run_id,
                ToolDefinition(
                    tool_name=approval.proposal.operation_type,
                    required_capabilities=approval.proposal.required_capabilities,
                    risk_level=approval.proposal.risk_level,
                    side_effect=True,
                ),
            )
            if not current_permission.allowed:
                raise PermissionError(f"current permission revoked: {current_permission.reason}")
            approval.decided_by = approver_id
            approval.decision_reason = reason
            approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            if approve:
                write_key = _hash({"operation_id": approval.proposal.operation_id, "proposal_hash": approval.proposal.proposal_hash})
                existing = self.store.get_tool_call_by_key(write_key)
                if existing is not None:
                    call = ToolCall(**{**existing, "status": ToolCallStatus(existing["status"])})
                    call_data = None
                    outbox_data = None
                else:
                    call = ToolCall(tool_call_id=_id("call"), task_run_id=approval.task_run_id, step_id=approval.step_id,
                                    operation_id=approval.proposal.operation_id, write_idempotency_key=write_key)
                    call_data = asdict(call)
                    call_data["status"] = call.status.value
                    outbox_data = (_id("outbox"), write_key, "READY", {"tool_call_id": call.tool_call_id, "operation_id": approval.proposal.operation_id})
                result = {"accepted": True, "status": approval.status.value, "tool_call_id": call.tool_call_id}
            else:
                result = {"accepted": True, "status": approval.status.value}
                call_data = None
                outbox_data = None
            inserted, prior = self.store.save_approval_and_effects(
                approval, self._approval_data(approval), action_key, result, event_id,
                call_data=call_data, outbox_data=outbox_data,
            )
            if not inserted:
                return {**(prior or result), "duplicate": True}
            task.status = TaskRunStatus.RUNNING if approve else TaskRunStatus.CANCELLED
            self.store.save_task_run(task)
            self._audit("APPROVAL_DECIDED", task.tenant_id, approver_id, task.task_run_id,
                        {"approval_id": approval_id, "approved": approve}, approval.status.value)
        except (KeyError, PermissionError, ValueError) as exc:
            if approval.status == ApprovalStatus.INVALIDATED:
                self.store.save_approval(approval, self._approval_data(approval))
            result = {"accepted": False, "reason": str(exc), "status": approval.status.value}
            self._audit("APPROVAL_REJECTED", task.tenant_id, approver_id, task.task_run_id,
                        {"approval_id": approval_id, "reason": str(exc)}, "REJECTED")
        self.store.record_inbox_event(event_id, result)
        return result

    def _enqueue_tool_call(self, proposal: OperationProposal) -> ToolCall:
        write_key = _hash({"operation_id": proposal.operation_id, "proposal_hash": proposal.proposal_hash})
        existing = self.store.get_tool_call_by_key(write_key)
        if existing is not None:
            return ToolCall(**{**existing, "status": ToolCallStatus(existing["status"])})
        call = ToolCall(tool_call_id=_id("call"), task_run_id=proposal.task_run_id, step_id=proposal.step_id,
                        operation_id=proposal.operation_id, write_idempotency_key=write_key)
        inserted = self.store.save_tool_call_and_outbox(
            call, _id("outbox"), "READY", {"tool_call_id": call.tool_call_id, "operation_id": proposal.operation_id}
        )
        if not inserted:
            existing = self.store.get_tool_call_by_key(write_key)
            if existing is not None:
                return ToolCall(**{**existing, "status": ToolCallStatus(existing["status"])})
            raise RuntimeError("tool call/outbox transaction was not committed")
        task = self._task(proposal.task_run_id)
        self._audit("OUTBOX_ENQUEUED", task.tenant_id, proposal.requested_by, task.task_run_id,
                    {"tool_call_id": call.tool_call_id, "write_idempotency_key": write_key}, "READY")
        return call

    def execute_approved(self, operation_id: str) -> ToolCall:
        approval = self.get_approval_for_operation(operation_id)
        if approval is None or approval.status != ApprovalStatus.APPROVED:
            raise PermissionError("operation has no approved request")
        existing = self.store.get_tool_call_by_key(_hash({"operation_id": operation_id, "proposal_hash": approval.proposal.proposal_hash}))
        if existing is not None:
            return ToolCall(**{**existing, "status": ToolCallStatus(existing["status"])})
        return self._enqueue_tool_call(approval.proposal)

    def approval_for_tool_call(self, call: ToolCall) -> ApprovalRequest | None:
        for row in self.store.list_approvals(call.task_run_id):
            approval = self._approval_from_row(row)
            if approval.proposal.operation_id != call.operation_id:
                continue
            write_key = _hash({"operation_id": call.operation_id, "proposal_hash": approval.proposal.proposal_hash})
            if write_key == call.write_idempotency_key:
                return approval
        return None

    def dispatch_tool_call(self, tool_call_id: str) -> ToolCall:
        data = self.store.get_tool_call(tool_call_id)
        if data is None:
            raise KeyError(tool_call_id)
        call = ToolCall(**{**data, "status": ToolCallStatus(data["status"])})
        if call.status in (ToolCallStatus.UNKNOWN, ToolCallStatus.RECONCILING):
            raise RuntimeError("call must be reconciled before retry")
        if call.status == ToolCallStatus.DISPATCHED:
            raise RuntimeError("DISPATCHED call must be reconciled before retry")
        if call.status in (ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED):
            return call
        approval = self.approval_for_tool_call(call)
        denial: str | None = None
        if approval is None or approval.status != ApprovalStatus.APPROVED:
            denial = "APPROVAL_NOT_CURRENT"
        else:
            decision = self.authorize_operation(
                call.task_run_id,
                ToolDefinition(
                    tool_name=approval.proposal.operation_type,
                    required_capabilities=approval.proposal.required_capabilities,
                    risk_level=approval.proposal.risk_level,
                    side_effect=True,
                ),
            )
            if not decision.allowed:
                denial = "PERMISSION_REVOKED"
        if denial is not None:
            call.status = ToolCallStatus.FAILED
            call.error_code = denial
            self.store.update_tool_call(call)
            task = self._refresh_task_status(call.task_run_id)
            self._audit("TOOL_CALL_DENIED", task.tenant_id, task.requested_by, task.task_run_id,
                        {"tool_call_id": tool_call_id, "reason": denial}, "DENIED")
            return call
        if not self.store.mark_tool_call_dispatched(tool_call_id):
            latest = self.store.get_tool_call(tool_call_id)
            if latest is None:
                raise KeyError(tool_call_id)
            latest_call = ToolCall(**{**latest, "status": ToolCallStatus(latest["status"])})
            if latest_call.status in (ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED):
                return latest_call
            raise RuntimeError(f"tool call dispatch fence already crossed: {latest_call.status.value}")
        call.status = ToolCallStatus.DISPATCHED
        call.attempts += 1
        return call

    def complete_tool_call(self, tool_call_id: str, success: bool, remote_ref: str | None = None,
                           error_code: str | None = None) -> ToolCall:
        data = self.store.get_tool_call(tool_call_id)
        if data is None:
            raise KeyError(tool_call_id)
        call = ToolCall(**{**data, "status": ToolCallStatus(data["status"])})
        if call.status == ToolCallStatus.UNKNOWN:
            raise RuntimeError("reconcile UNKNOWN before completion")
        call.status = ToolCallStatus.SUCCEEDED if success else ToolCallStatus.FAILED
        call.remote_ref, call.error_code = remote_ref, error_code
        self.store.update_tool_call(call)
        task = self._refresh_task_status(call.task_run_id)
        self._audit("TOOL_CALL_COMPLETED", task.tenant_id, task.requested_by, task.task_run_id,
                    {"tool_call_id": tool_call_id, "success": success, "error_code": error_code}, call.status.value)
        return call

    def mark_timeout(self, tool_call_id: str, error_code: str = "TIMEOUT") -> ToolCall:
        data = self.store.get_tool_call(tool_call_id)
        if data is None:
            raise KeyError(tool_call_id)
        call = ToolCall(**{**data, "status": ToolCallStatus(data["status"])})
        call.status, call.error_code = ToolCallStatus.UNKNOWN, error_code
        self.store.update_tool_call(call)
        task = self._refresh_task_status(call.task_run_id)
        if task.status not in (TaskRunStatus.RECONCILING, TaskRunStatus.RUNNING):
            task.status = TaskRunStatus.RECONCILING
            self.store.save_task_run(task)
        self._audit("TOOL_CALL_UNKNOWN", task.tenant_id, task.requested_by, task.task_run_id,
                    {"tool_call_id": tool_call_id, "error_code": error_code}, "UNKNOWN")
        return call

    def reconcile_tool_call(self, tool_call_id: str, found_remote_ref: str | None = None,
                            remote_status: str = "UNKNOWN", error_code: str | None = None) -> ToolCall:
        data = self.store.reconcile_tool_call_result(
            tool_call_id, remote_status.upper(), found_remote_ref, error_code,
        )
        call = ToolCall(**{**data, "status": ToolCallStatus(data["status"])})
        task = self._refresh_task_status(call.task_run_id)
        self._audit("TOOL_CALL_RECONCILED", task.tenant_id, task.requested_by, task.task_run_id,
                    {"tool_call_id": tool_call_id, "remote_status": remote_status, "error_code": error_code}, call.status.value)
        return call

    def _audit(self, event_type: str, tenant_id: str, actor_id: str | None, task_run_id: str | None,
               payload: Any, outcome: str) -> None:
        event = AuditEvent(audit_id=_id("audit"), event_type=event_type, tenant_id=tenant_id, actor_id=actor_id,
                           task_run_id=task_run_id, correlation_id=task_run_id, payload_hash=_hash(payload), outcome=outcome)
        self.store.save_audit(event)
