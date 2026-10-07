"""Durable domain value objects for the first control-plane slice."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ActorType(StrEnum):
    USER = "USER"
    PERSONAL_ASSISTANT = "PERSONAL_ASSISTANT"
    BUSINESS_AGENT = "BUSINESS_AGENT"
    MANAGEMENT_AGENT = "MANAGEMENT_AGENT"
    SYSTEM_SERVICE = "SYSTEM_SERVICE"


class TaskRunStatus(StrEnum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    WAITING_REVIEW = "WAITING_REVIEW"
    PAUSED = "PAUSED"
    RECONCILING = "RECONCILING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ToolCallStatus(StrEnum):
    PREPARED = "PREPARED"
    DISPATCHED = "DISPATCHED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"


@dataclass(frozen=True)
class IdentityContext:
    """Verified identity metadata; raw OAuth or application tokens never enter this object."""

    tenant_id: str
    actor_id: str
    auth_mode: str  # user_oauth or application
    subject_ref_hash: str
    scopes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TaskStep:
    step_id: str
    task_run_id: str
    name: str
    status: str = "PENDING"
    sequence: int = 0


@dataclass(frozen=True)
class Delegation:
    delegation_id: str
    task_run_id: str
    from_actor_id: str
    to_actor_id: str
    scope: frozenset[str] = frozenset()
    status: str = "ACTIVE"


@dataclass(frozen=True)
class MeetingRecord:
    record_id: str
    tenant_id: str
    submitted_by: str
    document_id_hash: str
    revision_id: str
    source_type: str
    source_ref_hash: str
    contract_status: str
    payload_hash: str
    source_block_ids: tuple[int, ...] = ()
    task_run_id: str | None = None


@dataclass
class MeetingTodoDraft:
    draft_id: str
    record_id: str
    todo_id: str
    title: str
    assignee_actor_id: str | None = None
    due_date: str | None = None
    evidence_block_ids: tuple[int, ...] = ()
    status: str = "NEEDS_CONFIRMATION"
    confirmation_reasons: list[str] = field(default_factory=list)
    visibility_scope: str = "assignee"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    tenant_id: str
    actor_type: ActorType
    capabilities: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    external_ref_hash: str | None = None
    active: bool = True


@dataclass(frozen=True)
class AgentVersion:
    agent_id: str
    version: int
    capabilities: frozenset[str]
    active: bool = True
    skill_version: str | None = None


@dataclass(frozen=True)
class AssistantBinding:
    binding_id: str
    tenant_id: str
    member_actor_id: str
    assistant_actor_id: str
    default: bool = True


@dataclass
class TaskRun:
    task_run_id: str
    tenant_id: str
    requested_by: str
    assistant_actor_id: str
    agent_id: str
    agent_version: int
    status: TaskRunStatus = TaskRunStatus.RECEIVED
    input_ref_hash: str | None = None
    current_step_id: str | None = None
    failure_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolDefinition:
    tool_name: str
    required_capabilities: frozenset[str]
    risk_level: RiskLevel
    side_effect: bool = False


@dataclass(frozen=True)
class ToolPolicy:
    policy_id: str
    tool_name: str
    allowed_capabilities: frozenset[str]
    risk_level: RiskLevel
    requires_approval: bool = False


@dataclass(frozen=True)
class OperationProposal:
    operation_id: str
    task_run_id: str
    step_id: str
    operation_type: str
    target_ref: str
    arguments: dict[str, Any]
    requested_by: str
    agent_id: str
    agent_version: int
    risk_level: RiskLevel
    required_capabilities: frozenset[str]
    permission_intersection: frozenset[str]
    proposal_hash: str
    action_version: int = 1


@dataclass
class ApprovalRequest:
    approval_id: str
    task_run_id: str
    step_id: str
    proposal: OperationProposal
    eligible_approver_roles: frozenset[str]
    eligible_approver_ids: frozenset[str] = frozenset()
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = None
    decision_reason: str | None = None


@dataclass
class ToolCall:
    tool_call_id: str
    task_run_id: str
    step_id: str
    operation_id: str
    write_idempotency_key: str
    status: ToolCallStatus = ToolCallStatus.PREPARED
    attempts: int = 0
    remote_ref: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    audit_id: str
    event_type: str
    tenant_id: str
    actor_id: str | None
    task_run_id: str | None
    correlation_id: str | None
    payload_hash: str
    outcome: str
