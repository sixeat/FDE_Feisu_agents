"""Durable orchestration from a member's assistant to a business Agent."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .meeting import MEETING_TASK_TOOL, MeetingIngestResult, MeetingWorkflow
from .models import ActorType, Delegation, IdentityContext, TaskRunStatus, TaskStep
from .runtime import RunEvent, RunRequest, RuntimeAdapter, RuntimeResult
from .service import ControlPlane


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentExecution:
    task_run_id: str
    delegation_id: str
    runtime_result: RuntimeResult
    meeting_result: MeetingIngestResult | None
    deduplicated: bool = False


@dataclass(frozen=True)
class ReviewSubmission:
    task_run_id: str
    record_id: str
    approval_ids: tuple[str, ...]


class AgentOrchestrator:
    """Run one meeting request through assistant delegation and Hermes.

    This is intentionally a bounded vertical slice. It does not execute tools;
    it persists the delegation and runtime events, then sends successful Agent
    output through the existing MeetingWorkflow contract gate.
    """

    def __init__(self, control_plane: ControlPlane, runtime: RuntimeAdapter) -> None:
        self.control_plane = control_plane
        self.runtime = runtime
        self.meeting = MeetingWorkflow(control_plane)

    def execute_meeting_request(
        self,
        *,
        requester_identity: IdentityContext,
        member_actor_id: str,
        assistant_actor_id: str,
        agent_id: str,
        agent_version: int,
        document_id_hash: str,
        revision_id: str | int,
        blocks: list[dict[str, Any]],
        valid_block_ids: set[int] | frozenset[int] | None = None,
        record_id: str | None = None,
        task_run_id: str | None = None,
        dispatch_key: str | None = None,
        runtime_deadline_seconds: int = 120,
    ) -> AgentExecution:
        if runtime_deadline_seconds < 1:
            raise ValueError("runtime deadline must be positive")
        user = self.control_plane.verify_identity(requester_identity)
        if user.actor_id != member_actor_id:
            raise PermissionError("requester identity does not match meeting member")
        assistant = self.control_plane._actor(assistant_actor_id)
        agent = self.control_plane._actor(agent_id)
        if user.actor_type != ActorType.USER or assistant.actor_type != ActorType.PERSONAL_ASSISTANT:
            raise PermissionError("meeting request must start from a user and personal assistant")
        if agent.actor_type != ActorType.BUSINESS_AGENT:
            raise PermissionError("meeting target must be a business agent")
        binding = self.control_plane.store.get_binding(member_actor_id, user.tenant_id)
        if binding is None or binding["assistant_actor_id"] != assistant_actor_id:
            raise PermissionError("member is not bound to the requested personal assistant")
        if len({user.tenant_id, assistant.tenant_id, agent.tenant_id}) != 1:
            raise PermissionError("delegation actors must share a tenant")
        if not dispatch_key:
            raise ValueError("dispatch_key is required for event-driven agent execution")
        input_ref = {
            "document_id_hash": document_id_hash,
            "revision_id": str(revision_id),
            "blocks": blocks,
        }
        candidate_task_id = task_run_id or _id("run")
        task = self.control_plane.create_task_run(
            member_actor_id,
            assistant_actor_id,
            agent_id,
            agent_version,
            input_ref_hash=_hash(input_ref),
            task_run_id=candidate_task_id,
            dispatch_key=dispatch_key,
            identity=requester_identity,
        )
        existing_dispatch = task.task_run_id != candidate_task_id
        delegations = self.control_plane.store.list_delegations(task.task_run_id)
        prior_events = self.control_plane.store.list_runtime_events(task.task_run_id)
        if existing_dispatch and (task.status not in {TaskRunStatus.QUEUED, TaskRunStatus.PLANNING} or prior_events):
            delegation_id = str(delegations[0].get("delegation_id")) if delegations else ""
            return AgentExecution(
                task.task_run_id, delegation_id, RuntimeResult("DEDUPLICATED", None, None), None, deduplicated=True
            )
        if task.status == TaskRunStatus.QUEUED:
            self.control_plane.transition_task(task.task_run_id, TaskRunStatus.PLANNING)
        steps = self.control_plane.store.list_task_steps(task.task_run_id)
        if steps:
            step_id = str(steps[0]["step_id"])
        else:
            step_id = _id("step")
            self.control_plane.create_task_step(
                TaskStep(step_id, task.task_run_id, "personal_assistant.delegate.meeting_agent", sequence=1)
            )
        scope = assistant.capabilities & agent.capabilities
        if delegations:
            delegation_id = str(delegations[0]["delegation_id"])
            delegation = Delegation(delegation_id, task.task_run_id, assistant_actor_id, agent_id, scope)
        else:
            delegation = self.control_plane.delegate(
                Delegation(_id("delegation"), task.task_run_id, assistant_actor_id, agent_id, scope)
            )
        self.control_plane.transition_task(task.task_run_id, TaskRunStatus.RUNNING)
        request = RunRequest(
            task_run_id=task.task_run_id,
            trace_id=_id("trace"),
            actor_ref=member_actor_id,
            agent_version=f"{agent_id}@{agent_version}",
            input_ref=input_ref,
            tool_policy_snapshot={"allowed_tools": [], "risk": "NONE", "delegation_id": delegation.delegation_id},
            deadline=(datetime.now(timezone.utc) + timedelta(seconds=runtime_deadline_seconds)).isoformat(),
        )
        try:
            result = self.runtime.run_once(request)
        except Exception as exc:
            result = RuntimeResult(
                "FAILED", None, None, error_code="RUNTIME_ADAPTER_EXCEPTION", error_detail=str(exc)[:300]
            )
        try:
            self._persist_events(result.events, task.task_run_id, request.trace_id)
        except ValueError as exc:
            self.control_plane.transition_task(task.task_run_id, TaskRunStatus.FAILED, str(exc))
            result = RuntimeResult(
                "FAILED", None, result.external_run_ref, (), "RUNTIME_EVENT_INVALID", str(exc)[:300]
            )
        meeting_result: MeetingIngestResult | None = None
        if result.status == "SUCCEEDED" and result.payload is not None:
            try:
                if not result.payload.get("todos"):
                    raise ValueError("meeting agent returned no todo candidates")
                meeting_result = self.meeting.ingest_runtime_result(
                    runtime_result=result,
                    record_id=record_id or f"meeting_{_hash(dispatch_key)[:20]}",
                    tenant_id=task.tenant_id,
                    submitted_by=member_actor_id,
                    document_id_hash=document_id_hash,
                    revision_id=revision_id,
                    valid_block_ids=valid_block_ids or {int(block["index"]) for block in blocks},
                    task_run_id=task.task_run_id,
                )
                self.meeting.build_drafts(
                    record_id=meeting_result.record.record_id,
                    payload=result.payload,
                )
                self.control_plane.transition_task(task.task_run_id, TaskRunStatus.WAITING_REVIEW)
            except Exception as exc:
                self.control_plane.transition_task(
                    task.task_run_id, TaskRunStatus.FAILED, f"MEETING_INGEST_FAILED:{type(exc).__name__}"
                )
                result = RuntimeResult(
                    "FAILED", None, result.external_run_ref, result.events,
                    "MEETING_INGEST_FAILED", str(exc)[:300],
                )
        elif result.status == "UNKNOWN":
            self.control_plane.transition_task(task.task_run_id, TaskRunStatus.RECONCILING, "runtime result unknown")
        else:
            self.control_plane.transition_task(
                task.task_run_id,
                TaskRunStatus.FAILED,
                result.error_code or "runtime execution failed",
            )
        return AgentExecution(task.task_run_id, delegation.delegation_id, result, meeting_result)

    def submit_meeting_review(
        self,
        *,
        task_run_id: str,
        record_id: str,
        reviewer_identity: IdentityContext,
        updates_by_todo: dict[str, dict[str, Any]],
    ) -> ReviewSubmission:
        task = self.control_plane._task(task_run_id)
        record = self.control_plane.store.get_meeting_record(record_id)
        if record is None:
            raise KeyError(record_id)
        if record.get("task_run_id") != task_run_id or record["tenant_id"] != task.tenant_id:
            raise PermissionError("meeting record is not linked to this task")
        reviewer = self.control_plane.verify_identity(reviewer_identity)
        reviewer_actor_id = reviewer.actor_id
        if (
            not reviewer.active
            or reviewer.tenant_id != task.tenant_id
            or reviewer.actor_type != ActorType.USER
            or reviewer_actor_id != task.requested_by
        ):
            raise PermissionError("reviewer is not allowed to confirm this meeting")
        if task.status != TaskRunStatus.WAITING_REVIEW:
            raise ValueError("task is not waiting for meeting review")
        drafts = self.control_plane.store.list_meeting_todos(record_id)
        if not drafts:
            raise ValueError("meeting has no todo drafts")
        known_todos = {draft["todo_id"] for draft in drafts}
        if set(updates_by_todo) - known_todos:
            raise ValueError("review contains an unknown todo")
        reviewed = [
            self.meeting.prepare_draft_revision(record_id, draft["todo_id"], **updates_by_todo.get(draft["todo_id"], {}))
            for draft in drafts
        ]
        active_count = sum(draft.status != "DISCARDED" for draft in reviewed)
        if active_count and any(draft.status != "READY_FOR_APPROVAL" for draft in reviewed if draft.status != "DISCARDED"):
            raise ValueError("all todos need human confirmation before proposal creation")
        if active_count:
            decision = self.control_plane.authorize_operation(task_run_id, MEETING_TASK_TOOL)
            if not decision.allowed:
                raise PermissionError(decision.reason)
        self.control_plane.store.save_meeting_todos(reviewed)
        if not active_count:
            self.control_plane._audit(
                "MEETING_REVIEW_SUBMITTED", task.tenant_id, reviewer_actor_id,
                task_run_id, {"record_id": record_id, "todo_count": len(drafts), "approval_count": 0}, "DISCARDED"
            )
            self.control_plane.transition_task(task_run_id, TaskRunStatus.CANCELLED)
            return ReviewSubmission(task_run_id, record_id, ())
        proposals = self.meeting.create_task_proposals(record_id, task_run_id)
        self.control_plane._audit(
            "MEETING_REVIEW_SUBMITTED",
            task.tenant_id,
            reviewer_actor_id,
            task_run_id,
            {"record_id": record_id, "todo_count": len(drafts), "approval_count": len(proposals)},
            "ACCEPTED",
        )
        self.control_plane.transition_task(task_run_id, TaskRunStatus.WAITING_APPROVAL)
        return ReviewSubmission(task_run_id, record_id, tuple(approval.approval_id for _, approval in proposals))

    def submit_meeting_review_refs(
        self,
        *,
        task_run_id: str,
        record_id: str,
        reviewer_identity: IdentityContext,
        updates_by_todo_ref: dict[str, dict[str, Any]],
    ) -> ReviewSubmission:
        """Submit UI review patches using opaque per-record todo references."""
        updates_by_todo: dict[str, dict[str, Any]] = {}
        for todo_ref, updates in updates_by_todo_ref.items():
            todo_id = self.meeting.resolve_review_todo_ref(record_id, todo_ref)
            if todo_id in updates_by_todo:
                raise ValueError("duplicate todo review reference")
            updates_by_todo[todo_id] = updates
        return self.submit_meeting_review(
            task_run_id=task_run_id,
            record_id=record_id,
            reviewer_identity=reviewer_identity,
            updates_by_todo=updates_by_todo,
        )

    def _persist_events(self, events: tuple[RunEvent, ...], task_run_id: str, trace_id: str) -> None:
        sequences = [event.sequence for event in events]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("RUNTIME_EVENT_SEQUENCE_INVALID")
        for event in events:
            if event.task_run_id != task_run_id:
                raise ValueError("RUNTIME_EVENT_TASK_MISMATCH")
            if event.trace_id != trace_id:
                raise ValueError("RUNTIME_EVENT_TRACE_MISMATCH")
            if event.sequence <= 0:
                raise ValueError("RUNTIME_EVENT_SEQUENCE_INVALID")
            key = f"{event.task_run_id}:{event.sequence}"
            data = {
                "task_run_id": event.task_run_id,
                "trace_id": event.trace_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at,
                "summary": event.summary,
            }
            prior = self.control_plane.store.get_runtime_event(key)
            if prior is not None:
                if prior != data:
                    raise ValueError("RUNTIME_EVENT_CONFLICT")
                continue
            self.control_plane.store.save_runtime_event(key, event.task_run_id, event.sequence, data)


__all__ = ["AgentExecution", "AgentOrchestrator", "ReviewSubmission"]
