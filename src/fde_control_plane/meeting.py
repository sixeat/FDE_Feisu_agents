"""Stage 2 meeting-artifact vertical slice.

The workflow consumes a structured Agent result and produces only durable,
approval-bound operation proposals. ``FakeFeishuTaskGateway`` is a local
deterministic adapter used by tests and demonstrations; a real Feishu adapter
can implement the same two methods without changing the control-plane flow.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import time
from datetime import date, datetime, timezone
from dataclasses import dataclass
from typing import Any, Protocol

from .contract import ContractValidation
from .meeting_input import normalize_document_blocks
from .models import (
    ActorType,
    MeetingRecord,
    MeetingTodoDraft,
    OperationProposal,
    ApprovalRequest,
    RiskLevel,
    ToolDefinition,
    ToolCall,
    ToolCallStatus,
)
from .service import ControlPlane
from .runtime import RuntimeResult


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def review_todo_ref(todo_id: str) -> str:
    """Opaque stable reference exposed to a review client, not the DB key."""
    return hashlib.sha256(str(todo_id).encode("utf-8")).hexdigest()[:12]


MEETING_TASK_TOOL = ToolDefinition(
    tool_name="feishu.task.create",
    required_capabilities=frozenset({"task.write"}),
    risk_level=RiskLevel.HIGH,
    side_effect=True,
)


@dataclass(frozen=True)
class MeetingIngestResult:
    record: MeetingRecord
    validation: ContractValidation


@dataclass(frozen=True)
class TaskExecutionResult:
    tool_call_id: str
    status: str
    remote_task_id: str | None = None
    notification_status: str = "NOT_SENT"
    warning: str | None = None


class TaskGateway(Protocol):
    def create_task(self, payload: dict[str, Any], idempotency_key: str) -> str: ...
    def query_task(self, idempotency_key: str, *, remote_task_id: str | None = None) -> str | None: ...
    def send_notification(self, actor_id: str, remote_task_id: str) -> None: ...


class MeetingWorkflow:
    def __init__(self, control_plane: ControlPlane) -> None:
        self.control_plane = control_plane

    def ingest(
        self,
        *,
        record_id: str,
        tenant_id: str,
        submitted_by: str,
        payload: dict[str, Any],
        document_id_hash: str,
        revision_id: str | int,
        valid_block_ids: set[int] | frozenset[int],
        source_type: str = "feishu_document",
        source_ref_hash: str = "",
        task_run_id: str | None = None,
    ) -> MeetingIngestResult:
        validation = self.control_plane.validate_meeting_agent_result(
            payload,
            document_id_hash=document_id_hash,
            revision_id=revision_id,
            valid_block_ids=valid_block_ids,
        )
        if validation.status == "INVALID":
            raise ValueError(f"invalid meeting agent contract: {','.join(validation.reasons)}")
        source = payload.get("source") or {}
        record = MeetingRecord(
            record_id=record_id,
            tenant_id=tenant_id,
            submitted_by=submitted_by,
            document_id_hash=document_id_hash,
            revision_id=str(revision_id),
            source_type=source_type,
            source_ref_hash=source_ref_hash or _hash({"document_id_hash": document_id_hash, "revision_id": revision_id}),
            contract_status=validation.status,
            payload_hash=_hash(payload),
            source_block_ids=tuple(sorted(set(source.get("source_block_ids") or []))),
            task_run_id=task_run_id,
        )
        self.control_plane.store.save_meeting_record(record)
        return MeetingIngestResult(record, validation)

    def ingest_runtime_result(
        self,
        *,
        runtime_result: RuntimeResult,
        record_id: str,
        tenant_id: str,
        submitted_by: str,
        document_id_hash: str,
        revision_id: str | int,
        valid_block_ids: set[int] | frozenset[int],
        source_type: str = "feishu_document",
        source_ref_hash: str = "",
        task_run_id: str | None = None,
    ) -> MeetingIngestResult:
        """Pass a successful Runtime result through the normal contract gate.

        Runtime output is untrusted. Failed or uncertain runs never become a
        meeting record, and successful output still goes through ``ingest``
        for source, evidence and human-review validation.
        """
        if runtime_result.status != "SUCCEEDED" or runtime_result.payload is None:
            raise ValueError(
                f"runtime result is not ingestible: {runtime_result.status}/"
                f"{runtime_result.error_code or 'NO_PAYLOAD'}"
            )
        return self.ingest(
            record_id=record_id,
            tenant_id=tenant_id,
            submitted_by=submitted_by,
            payload=runtime_result.payload,
            document_id_hash=document_id_hash,
            revision_id=revision_id,
            valid_block_ids=valid_block_ids,
            source_type=source_type,
            source_ref_hash=source_ref_hash,
            task_run_id=task_run_id,
        )

    def ingest_document_blocks(
        self,
        *,
        record_id: str,
        tenant_id: str,
        submitted_by: str,
        document_id_hash: str,
        revision_id: str | int,
        blocks: list[dict[str, Any]],
        source_ref_hash: str = "",
    ) -> MeetingIngestResult:
        payload = normalize_document_blocks(
            document_id_hash=document_id_hash, revision_id=revision_id, blocks=blocks
        )
        return self.ingest(
            record_id=record_id, tenant_id=tenant_id, submitted_by=submitted_by,
            payload=payload, document_id_hash=document_id_hash, revision_id=revision_id,
            valid_block_ids={int(block["index"]) for block in blocks}, source_type="feishu_document",
            source_ref_hash=source_ref_hash,
        )

    def build_drafts(
        self,
        *,
        record_id: str,
        payload: dict[str, Any],
        assignee_actor_by_todo: dict[str, str] | None = None,
    ) -> list[MeetingTodoDraft]:
        record_data = self.control_plane.store.get_meeting_record(record_id)
        if record_data is None:
            raise KeyError(record_id)
        assignee_actor_by_todo = assignee_actor_by_todo or {}
        relation_conflicts: set[str] = set()
        for relation in payload.get("relations") or []:
            if relation.get("needs_human_confirmation") or relation.get("relation") == "possible_duplicate_or_conflict":
                relation_conflicts.update(str(value) for value in relation.get("todo_ids") or [])

        drafts: list[MeetingTodoDraft] = []
        for todo in payload.get("todos") or []:
            todo_id = str(todo.get("todo_id") or "")
            reasons: list[str] = []
            title = str(todo.get("title") or "").strip()
            if not title:
                reasons.append("TITLE_MISSING")
            assignee_actor_id = assignee_actor_by_todo.get(todo_id)
            if not assignee_actor_id:
                reasons.append("ASSIGNEE_UNMAPPED")
            else:
                actor = self.control_plane._actor(assignee_actor_id)
                if actor.tenant_id != record_data["tenant_id"] or actor.actor_type != ActorType.USER or not actor.active:
                    reasons.append("ASSIGNEE_INVALID")
            due = todo.get("due_date_candidate") or {}
            due_date = due.get("normalized_date")
            if due.get("status") not in {"normalized", "confirmed"} or not due_date:
                reasons.append("DUE_DATE_UNCONFIRMED")
                due_date = None
            evidence = tuple(sorted(set(todo.get("evidence_block_ids") or [])))
            if not evidence or not set(evidence).issubset(set(record_data.get("source_block_ids") or [])):
                reasons.append("EVIDENCE_INVALID")
            if todo.get("needs_confirmation"):
                reasons.extend(str(reason) for reason in todo.get("confirmation_reasons") or [] if reason)
            if todo_id in relation_conflicts:
                reasons.append("POSSIBLE_DUPLICATE_OR_CONFLICT")
            draft = MeetingTodoDraft(
                draft_id=_id("draft"), record_id=record_id, todo_id=todo_id, title=title,
                assignee_actor_id=assignee_actor_id, due_date=str(due_date) if due_date else None,
                evidence_block_ids=evidence, status="NEEDS_CONFIRMATION" if reasons else "READY_FOR_APPROVAL",
                confirmation_reasons=sorted(set(reasons)),
            )
            self.control_plane.store.save_meeting_todo(draft)
            drafts.append(draft)
        return drafts

    def revise_draft(self, record_id: str, todo_id: str, **updates: Any) -> MeetingTodoDraft:
        draft = self.prepare_draft_revision(record_id, todo_id, **updates)
        self.control_plane.store.save_meeting_todo(draft)
        return draft

    def prepare_draft_revision(self, record_id: str, todo_id: str, **updates: Any) -> MeetingTodoDraft:
        """Validate a reviewer edit without changing the stored draft."""
        raw = self.control_plane.store.get_meeting_todo(record_id, todo_id)
        if raw is None:
            raise KeyError(f"{record_id}:{todo_id}")
        operation_id = f"meeting-{record_id}-{todo_id}"
        if self.control_plane.get_approval_for_operation(operation_id) is not None:
            raise ValueError("draft already has an approval; revise the approval proposal instead")
        allowed = {"title", "assignee_actor_id", "due_date", "visibility_scope", "discard", "resolve_conflict"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported draft fields: {','.join(sorted(unknown))}")
        updates = dict(updates)
        discard = updates.pop("discard", None)
        resolve_conflict = updates.pop("resolve_conflict", False)
        if discard is not None and not isinstance(discard, bool):
            raise ValueError("discard must be a boolean")
        if not isinstance(resolve_conflict, bool):
            raise ValueError("resolve_conflict must be a boolean")
        if discard is True:
            if updates or resolve_conflict:
                raise ValueError("discard cannot be combined with field edits")
            raw["status"] = "DISCARDED"
            raw["confirmation_reasons"] = ["DISCARDED_BY_REVIEWER"]
            return MeetingTodoDraft(**raw)
        if raw["status"] == "DISCARDED" and discard is None:
            if updates or resolve_conflict:
                raise ValueError("discarded draft requires explicit restoration")
            return MeetingTodoDraft(**raw)
        raw.update(updates)
        reasons = []
        has_conflict = "POSSIBLE_DUPLICATE_OR_CONFLICT" in raw.get("confirmation_reasons", [])
        if has_conflict and not resolve_conflict:
            reasons.append("POSSIBLE_DUPLICATE_OR_CONFLICT")
        if resolve_conflict and not has_conflict:
            raise ValueError("cannot resolve a draft without a conflict marker")
        if not str(raw.get("title") or "").strip():
            reasons.append("TITLE_MISSING")
        actor_id = raw.get("assignee_actor_id")
        if not actor_id:
            reasons.append("ASSIGNEE_UNMAPPED")
        else:
            actor = self.control_plane._actor(actor_id)
            record = self.control_plane.store.get_meeting_record(record_id)
            if actor.actor_type != ActorType.USER or not actor.active or actor.tenant_id != record["tenant_id"]:
                reasons.append("ASSIGNEE_INVALID")
        if not raw.get("due_date"):
            reasons.append("DUE_DATE_UNCONFIRMED")
        else:
            try:
                parsed_due = datetime.fromisoformat(str(raw["due_date"]))
                if parsed_due.date().isoformat() != str(raw["due_date"]):
                    reasons.append("DUE_DATE_INVALID")
            except ValueError:
                reasons.append("DUE_DATE_INVALID")
        if raw.get("visibility_scope") != "assignee":
            reasons.append("VISIBILITY_SCOPE_UNSUPPORTED")
        raw["confirmation_reasons"] = sorted(set(reasons))
        raw["status"] = "NEEDS_CONFIRMATION" if reasons else "READY_FOR_APPROVAL"
        return MeetingTodoDraft(**raw)

    def resolve_review_todo_ref(self, record_id: str, todo_ref: str) -> str:
        """Resolve an opaque review reference within one meeting record."""
        if not isinstance(todo_ref, str) or len(todo_ref) != 12:
            raise ValueError("invalid todo review reference")
        matches = [
            str(draft["todo_id"])
            for draft in self.control_plane.store.list_meeting_todos(record_id)
            if review_todo_ref(str(draft["todo_id"])) == todo_ref
        ]
        if not matches:
            raise KeyError("unknown todo review reference")
        if len(matches) > 1:
            raise ValueError("todo review reference collision")
        return matches[0]

    def create_task_proposals(self, record_id: str, task_run_id: str) -> list[tuple[OperationProposal, ApprovalRequest]]:
        record = self.control_plane.store.get_meeting_record(record_id)
        if record is None:
            raise KeyError(record_id)
        task = self.control_plane._task(task_run_id)
        if task.tenant_id != record["tenant_id"]:
            raise PermissionError("meeting record and task tenant differ")
        if record.get("task_run_id") and record["task_run_id"] != task_run_id:
            raise PermissionError("meeting record belongs to a different task")
        if record["submitted_by"] != task.requested_by:
            raise PermissionError("meeting submitter must match task requester")
        drafts = self.control_plane.store.list_meeting_todos(record_id)
        if not drafts:
            raise ValueError("meeting has no todo drafts")
        active_drafts = [draft for draft in drafts if draft["status"] != "DISCARDED"]
        if not active_drafts:
            raise ValueError("meeting has no confirmed todos")
        if any(d["status"] != "READY_FOR_APPROVAL" for d in active_drafts):
            raise ValueError("all todos need human confirmation before proposal creation")
        proposals: list[tuple[OperationProposal, ApprovalRequest]] = []
        for draft in active_drafts:
            args = {
                "title": draft["title"],
                "due_date": draft["due_date"],
                "assignee_actor_id": draft["assignee_actor_id"],
                "visibility_scope": draft.get("visibility_scope", "assignee"),
                "origin": {
                    "type": "meeting",
                    "record_id": record_id,
                    "document_id_hash": record["document_id_hash"],
                    "revision_id": record["revision_id"],
                    "evidence_block_ids": draft["evidence_block_ids"],
                },
            }
            operation_id = f"meeting-{record_id}-{draft['todo_id']}"
            existing = self.control_plane.get_approval_for_operation(operation_id)
            if existing is not None:
                if existing.status.value != "PENDING":
                    raise ValueError(f"operation {operation_id} already has status {existing.status.value}; revise before resubmitting")
                proposal, approval = existing.proposal, existing
            else:
                proposal = self.control_plane.create_operation_proposal(
                    task_run_id, f"meeting:{record_id}:{draft['todo_id']}", "task.create",
                    f"meeting:{record_id}:{draft['todo_id']}", args, MEETING_TASK_TOOL,
                    operation_id=operation_id,
                    eligible_approver_roles=frozenset(),
                    eligible_approver_ids=frozenset({record["submitted_by"]}),
                )
                approval = self.control_plane.get_approval_for_operation(proposal.operation_id)
                assert approval is not None
            proposals.append((proposal, approval))
        return proposals


class FakeFeishuTaskGateway:
    """Deterministic local stand-in for the Feishu task API."""

    requires_source_preflight = False

    def __init__(self) -> None:
        self.tasks: dict[str, str] = {}
        self.payloads: dict[str, dict[str, Any]] = {}
        self.notifications: list[tuple[str, str]] = []
        self.fail_notifications = False
        self.fail_create = False
        self.timeout_create = False
        self.timeout_after_create = False

    def create_task(self, payload: dict[str, Any], idempotency_key: str) -> str:
        if self.fail_create:
            raise RuntimeError("simulated task create failure")
        if self.timeout_create:
            raise TimeoutError("simulated remote timeout")
        if idempotency_key in self.tasks:
            return self.tasks[idempotency_key]
        remote_id = "fake_task_" + idempotency_key[:16]
        self.tasks[idempotency_key] = remote_id
        self.payloads[idempotency_key] = dict(payload)
        if self.timeout_after_create:
            raise TimeoutError("simulated response loss after remote create")
        return remote_id

    def query_task(self, idempotency_key: str, *, remote_task_id: str | None = None) -> str | None:
        return self.tasks.get(idempotency_key)

    def send_notification(self, actor_id: str, remote_task_id: str) -> None:
        if self.fail_notifications:
            raise RuntimeError("simulated notification failure")
        self.notifications.append((actor_id, remote_task_id))


class TaskLookupUncertain(RuntimeError):
    """A fixed, non-sensitive diagnostic; absence is never permission to retry."""


class FeishuTaskGateway:
    """Thin optional lark-oapi adapter for the approved task operation.

    The control plane passes only a sanitized proposal and an idempotency key.
    App credentials stay inside this adapter process. Feishu task membership
    generates the member notification, so ``send_notification`` records that
    the API-side notification was delegated instead of sending a second chat.
    """

    requires_source_preflight = True

    def __init__(self, client: Any | None = None, *, app_id: str | None = None,
                 app_secret: str | None = None, actor_open_ids: dict[str, str] | None = None,
                 max_lookup_pages: int = 20) -> None:
        if not 1 <= max_lookup_pages <= 20:
            raise ValueError("max_lookup_pages must be between 1 and 20")
        if client is None:
            if not app_id or not app_secret:
                raise ValueError("app_id and app_secret are required when client is not supplied")
            import lark_oapi as lark  # optional dependency, loaded only in real Feishu mode
            client = lark.Client.builder().app_id(app_id).app_secret(app_secret).timeout(12).build()
        self.client = client
        self.actor_open_ids = actor_open_ids or {}
        self._remote_by_key: dict[str, str] = {}
        self.max_lookup_pages = max_lookup_pages

    def create_task(self, payload: dict[str, Any], idempotency_key: str) -> str:
        import lark_oapi as lark
        builder = self._build_task_builder(lark, payload, idempotency_key)
        request = lark.api.task.v2.model.CreateTaskRequest.builder().request_body(builder.build()).build()
        response = self.client.task.v2.task.create(request)
        if getattr(response, "code", None) != 0:
            raise RuntimeError(f"FEISHU_TASK_CREATE_{getattr(response, 'code', 'UNKNOWN')}")
        remote = getattr(getattr(response, "data", None), "task", None)
        guid = getattr(remote, "guid", None)
        if not guid:
            raise TimeoutError("Feishu task create returned no remote guid")
        self._remote_by_key[idempotency_key] = str(guid)
        return str(guid)

    def _build_task_builder(self, lark: Any, payload: dict[str, Any], idempotency_key: str) -> Any:
        """Build an InputTask while keeping the SDK import at the adapter edge."""
        # The task API exposes ``client_token`` only on create.  Keep the same
        # opaque key in the task description as a structured marker so a fresh
        # worker process can locate a task after the create response was lost.
        # This marker contains no credential or user identity.
        builder = (
            lark.api.task.v2.model.InputTask.builder()
            .summary(str(payload.get("title") or "会议待办"))
            .description(json.dumps({
                "due_date": payload.get("due_date"),
                "origin": payload.get("origin"),
                "fde_idempotency_key": idempotency_key,
            }, ensure_ascii=False, sort_keys=True))
            .client_token(idempotency_key)
        )
        due_date = payload.get("due_date")
        if due_date:
            try:
                due_dt = datetime.combine(date.fromisoformat(str(due_date)), datetime.min.time(), tzinfo=timezone.utc)
                due = lark.api.task.v2.model.Due.builder().timestamp(str(int(due_dt.timestamp() * 1000))).is_all_day(True).build()
                builder = builder.due(due)
            except ValueError:
                raise ValueError("DUE_DATE_INVALID")
        actor_id = payload.get("assignee_actor_id")
        open_id = self.actor_open_ids.get(actor_id)
        if open_id:
            member = lark.api.task.v2.model.Member.builder().id(open_id).type("user").role("assignee").build()
            builder = builder.members([member])
        return builder

    def verify_source_revision(self, document_id: str, expected_revision: str) -> None:
        """Read the source revision immediately before a task write."""
        from .feishu_review import FeishuDocumentRevisionReader

        actual = FeishuDocumentRevisionReader(self.client).get_revision(document_id)
        if str(actual) != str(expected_revision):
            raise RuntimeError("SOURCE_REVISION_STALE")

    def verify_assignee_binding(self, actor_id: str, expected_external_hash: str | None) -> None:
        """Require a configured open_id and verify it against the stored hash."""
        open_id = self.actor_open_ids.get(actor_id)
        if not open_id:
            raise RuntimeError("ASSIGNEE_MAPPING_MISSING")
        if expected_external_hash and hashlib.sha256(open_id.encode("utf-8")).hexdigest() != expected_external_hash:
            raise RuntimeError("ASSIGNEE_MAPPING_MISMATCH")

    def query_task(self, idempotency_key: str, *, remote_task_id: str | None = None) -> str | None:
        import lark_oapi as lark
        guid = remote_task_id or self._remote_by_key.get(idempotency_key)
        if guid:
            request = lark.api.task.v2.model.GetTaskRequest.builder().task_guid(guid).build()
            response = self.client.task.v2.task.get(request)
            if getattr(response, "code", None) != 0:
                raise TaskLookupUncertain("REMOTE_GET_FAILED")
            task = getattr(getattr(response, "data", None), "task", None)
            if getattr(task, "guid", None) != guid:
                raise TaskLookupUncertain("REMOTE_GET_INVALID")
            return guid

        candidate = self.find_task_candidate(idempotency_key)
        if candidate:
            # A description is user-editable, not a trusted create receipt.
            # Never promote an UNKNOWN call solely on a copied marker.
            raise TaskLookupUncertain("REMOTE_CANDIDATE_REQUIRES_REVIEW")
        return None

    def get_task(self, remote_task_id: str) -> Any:
        import lark_oapi as lark

        request = lark.api.task.v2.model.GetTaskRequest.builder().task_guid(remote_task_id).build()
        response = self.client.task.v2.task.get(request)
        if getattr(response, "code", None) != 0:
            raise TaskLookupUncertain("REMOTE_GET_FAILED")
        task = getattr(getattr(response, "data", None), "task", None)
        if getattr(task, "guid", None) != remote_task_id:
            raise TaskLookupUncertain("REMOTE_GET_INVALID")
        return task

    def patch_task_due(self, remote_task_id: str, due_date: str) -> None:
        import lark_oapi as lark

        try:
            due_dt = datetime.combine(date.fromisoformat(due_date), datetime.min.time(), tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError("DUE_DATE_INVALID") from exc
        due = lark.api.task.v2.model.Due.builder().timestamp(str(int(due_dt.timestamp() * 1000))).is_all_day(True).build()
        task = lark.api.task.v2.model.InputTask.builder().due(due).build()
        body = lark.api.task.v2.model.PatchTaskRequestBody.builder().task(task).update_fields(["due"]).build()
        request = lark.api.task.v2.model.PatchTaskRequest.builder().task_guid(remote_task_id).request_body(body).build()
        response = self.client.task.v2.task.patch(request)
        if getattr(response, "code", None) != 0:
            raise RuntimeError(f"FEISHU_TASK_PATCH_{getattr(response, 'code', 'UNKNOWN')}")

    def find_task_candidate(self, idempotency_key: str) -> str | None:
        """Find one candidate in the visible list, without authorizing adoption."""
        import lark_oapi as lark

        # A new worker has no in-memory mapping.  The task list API has no
        # client-token filter, so search the exact structured marker written by
        # ``_build_task_builder``.  This is read-only and deliberately bounded
        # to avoid an accidental unbounded scan of a tenant's task history.
        page_token: str | None = None
        seen_tokens: set[str] = set()
        matches: set[str] = set()
        for _ in range(self.max_lookup_pages):
            builder = lark.api.task.v2.model.ListTaskRequest.builder().page_size(50)
            if page_token:
                builder = builder.page_token(page_token)
            request = builder.build()
            response = self.client.task.v2.task.list(request)
            if getattr(response, "code", None) != 0:
                raise TaskLookupUncertain("REMOTE_LIST_FAILED")
            data = getattr(response, "data", None)
            items = getattr(data, "items", None)
            has_more = getattr(data, "has_more", None)
            if not isinstance(items, list) or not isinstance(has_more, bool):
                raise TaskLookupUncertain("REMOTE_LIST_INVALID")
            for task in items:
                description = getattr(task, "description", None)
                if not description:
                    continue
                try:
                    marker = json.loads(description)
                except (TypeError, ValueError):
                    continue
                if isinstance(marker, dict) and marker.get("fde_idempotency_key") == idempotency_key:
                    task_guid = getattr(task, "guid", None)
                    if not isinstance(task_guid, str) or not task_guid:
                        raise TaskLookupUncertain("REMOTE_LIST_INVALID")
                    matches.add(task_guid)
                    if len(matches) > 1:
                        raise TaskLookupUncertain("REMOTE_MULTIPLE_MATCHES")
            if not has_more:
                return next(iter(matches), None)
            next_token = getattr(data, "page_token", None)
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise TaskLookupUncertain("REMOTE_PAGINATION_INVALID")
            seen_tokens.add(next_token)
            page_token = next_token
        raise TaskLookupUncertain("REMOTE_SCAN_LIMIT")

    def send_notification(self, actor_id: str, remote_task_id: str) -> None:
        # The task API sends the assignee notification when members is set.
        return None

    def delete_task(self, remote_task_id: str) -> bool:
        """Explicit cleanup helper for the dedicated test task only."""
        import lark_oapi as lark
        request = lark.api.task.v2.model.DeleteTaskRequest.builder().task_guid(remote_task_id).build()
        response = self.client.task.v2.task.delete(request)
        return getattr(response, "code", None) == 0


class MeetingOutboxWorker:
    def __init__(self, control_plane: ControlPlane, gateway: TaskGateway) -> None:
        self.control_plane = control_plane
        self.gateway = gateway

    def run_once(self, *, limit: int = 10) -> list[TaskExecutionResult]:
        """Consume a bounded batch of recoverable outbox rows.

        This is intentionally a single-pass primitive. Process supervision,
        scheduling and graceful shutdown belong to deployment code; the
        durable claim and side-effect fences remain in the store/control plane.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        results: list[TaskExecutionResult] = []
        for row in self.control_plane.store.recoverable_outbox()[:limit]:
            data = json.loads(row["data_json"])
            tool_call_id = data.get("tool_call_id")
            if not tool_call_id:
                self.control_plane.store.update_outbox_status(
                    row["outbox_id"], "FAILED", error="OUTBOX_TOOL_CALL_MISSING"
                )
                continue
            results.append(self.execute(str(tool_call_id)))
        return results

    def execute(self, tool_call_id: str) -> TaskExecutionResult:
        existing = self.control_plane.store.get_tool_call(tool_call_id)
        if existing is None:
            raise KeyError(tool_call_id)
        existing_call = ToolCallStatus(existing["status"])
        if existing_call == ToolCallStatus.SUCCEEDED:
            return TaskExecutionResult(tool_call_id, existing_call.value, existing.get("remote_ref"), "ALREADY_SENT")
        if existing_call == ToolCallStatus.FAILED:
            return TaskExecutionResult(tool_call_id, existing_call.value, warning=existing.get("error_code"))
        if existing_call in (ToolCallStatus.UNKNOWN, ToolCallStatus.RECONCILING):
            return TaskExecutionResult(tool_call_id, existing_call.value, warning="RECONCILIATION_REQUIRED")
        if existing_call == ToolCallStatus.DISPATCHED:
            return TaskExecutionResult(tool_call_id, existing_call.value, warning="DISPATCHED_AWAITING_RESULT")

        claim_token = _id("claim")
        claimed = self.control_plane.store.claim_outbox_for_tool_call(tool_call_id, claim_token)
        if claimed is None:
            latest = self.control_plane.store.get_tool_call(tool_call_id)
            if latest is None:
                raise KeyError(tool_call_id)
            latest_status = ToolCallStatus(latest["status"])
            warning = {
                ToolCallStatus.SUCCEEDED: "ALREADY_SENT",
                ToolCallStatus.UNKNOWN: "RECONCILIATION_REQUIRED",
                ToolCallStatus.RECONCILING: "RECONCILIATION_REQUIRED",
                ToolCallStatus.DISPATCHED: "DISPATCHED_AWAITING_RESULT",
            }.get(latest_status, "WORKER_BUSY")
            return TaskExecutionResult(tool_call_id, latest_status.value, latest.get("remote_ref"),
                                       "ALREADY_SENT" if latest_status == ToolCallStatus.SUCCEEDED else "NOT_SENT",
                                       warning)

        def finish(status: str, error: str | None = None) -> None:
            if not self.control_plane.store.finish_outbox(claimed["outbox_id"], claim_token, status, error=error):
                raise RuntimeError("outbox claim was lost before completion")

        prepared_call = ToolCall(**{**existing, "status": existing_call})
        approval_request = self.control_plane.approval_for_tool_call(prepared_call)
        if approval_request is None:
            self.control_plane.complete_tool_call(tool_call_id, False, error_code="APPROVAL_NOT_FOUND")
            finish("FAILED", "APPROVAL_NOT_FOUND")
            return TaskExecutionResult(tool_call_id, ToolCallStatus.FAILED.value, warning="APPROVAL_NOT_FOUND")
        try:
            self._preflight(approval_request)
        except Exception as exc:
            error_code = str(exc) if str(exc).isidentifier() else type(exc).__name__
            self.control_plane.complete_tool_call(tool_call_id, False, error_code=error_code)
            finish("FAILED", error_code)
            return TaskExecutionResult(tool_call_id, ToolCallStatus.FAILED.value, warning=error_code)

        try:
            call = self.control_plane.dispatch_tool_call(tool_call_id)
        except Exception as exc:
            finish("FAILED", type(exc).__name__)
            raise
        if call.status == ToolCallStatus.SUCCEEDED:
            finish("SUCCEEDED")
            return TaskExecutionResult(tool_call_id, call.status.value, call.remote_ref, "ALREADY_SENT")
        if call.status == ToolCallStatus.FAILED:
            finish("FAILED", call.error_code)
            return TaskExecutionResult(tool_call_id, call.status.value, warning=call.error_code)
        try:
            remote_id = self.gateway.create_task(dict(approval_request.proposal.arguments), call.write_idempotency_key)
        except (TimeoutError, ConnectionError, BrokenPipeError, EOFError, OSError) as exc:
            self.control_plane.mark_timeout(tool_call_id, error_code=type(exc).__name__)
            finish("UNKNOWN", type(exc).__name__)
            return TaskExecutionResult(tool_call_id, ToolCallStatus.UNKNOWN.value, warning="REMOTE_RESULT_UNKNOWN")
        except Exception as exc:
            self.control_plane.complete_tool_call(tool_call_id, False, error_code=type(exc).__name__)
            finish("FAILED", type(exc).__name__)
            return TaskExecutionResult(tool_call_id, ToolCallStatus.FAILED.value, warning=type(exc).__name__)
        self.control_plane.complete_tool_call(tool_call_id, True, remote_ref=remote_id)
        finish("SUCCEEDED")
        notification_status = "SENT"
        warning = None
        assignee = approval_request.proposal.arguments.get("assignee_actor_id")
        try:
            if assignee:
                self.gateway.send_notification(assignee, remote_id)
        except Exception as exc:
            notification_status = "FAILED"
            warning = f"NOTIFICATION_{type(exc).__name__}"
        return TaskExecutionResult(tool_call_id, ToolCallStatus.SUCCEEDED.value, remote_id, notification_status, warning)

    def _preflight(self, approval_request: Any) -> None:
        if not getattr(self.gateway, "requires_source_preflight", True):
            return
        args = approval_request.proposal.arguments
        origin = args.get("origin") or {}
        if origin.get("type") != "meeting" or not origin.get("record_id"):
            raise RuntimeError("SOURCE_BINDING_MISSING")
        record = self.control_plane.store.get_meeting_record(str(origin["record_id"]))
        if record is None:
            raise RuntimeError("SOURCE_RECORD_MISSING")
        task = self.control_plane._task(approval_request.task_run_id)
        if record.get("tenant_id") != task.tenant_id or record.get("submitted_by") != task.requested_by:
            raise RuntimeError("SOURCE_TENANT_BINDING_MISMATCH")
        if str(origin.get("revision_id")) != str(record.get("revision_id")):
            raise RuntimeError("SOURCE_REVISION_BINDING_MISMATCH")
        if origin.get("document_id_hash") != record.get("document_id_hash"):
            raise RuntimeError("SOURCE_DOCUMENT_BINDING_MISMATCH")
        source = self.control_plane.store.get_meeting_source(str(origin["record_id"]))
        if source is None or str(source.get("revision_id")) != str(record.get("revision_id")):
            raise RuntimeError("SOURCE_SNAPSHOT_MISSING")
        document_id = str(source.get("document_id") or "")
        if not document_id or hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:len(str(record["document_id_hash"]))] != str(record["document_id_hash"]):
            raise RuntimeError("SOURCE_DOCUMENT_HASH_MISMATCH")
        assignee_id = args.get("assignee_actor_id")
        if not assignee_id:
            raise RuntimeError("ASSIGNEE_MISSING")
        actor = self.control_plane._actor(str(assignee_id))
        if actor.tenant_id != task.tenant_id or actor.actor_type != ActorType.USER or not actor.active:
            raise RuntimeError("ASSIGNEE_INVALID")
        verify_assignee = getattr(self.gateway, "verify_assignee_binding", None)
        if verify_assignee is not None:
            verify_assignee(str(assignee_id), actor.external_ref_hash)
        verify_source = getattr(self.gateway, "verify_source_revision", None)
        if verify_source is not None:
            verify_source(document_id, str(record["revision_id"]))

    def reconcile(self, tool_call_id: str) -> TaskExecutionResult:
        data = self.control_plane.store.get_tool_call(tool_call_id)
        if data is None:
            raise KeyError(tool_call_id)
        if data["status"] not in {"DISPATCHED", "UNKNOWN", "RECONCILING"}:
            return TaskExecutionResult(tool_call_id, data["status"], data.get("remote_ref"),
                                       warning="RECONCILIATION_NOT_REQUIRED")
        outbox = self.control_plane.store.get_outbox_for_tool_call(tool_call_id)
        if outbox is not None and outbox["status"] == "CLAIMED" and (outbox["lease_until"] or 0) > time.time():
            return TaskExecutionResult(tool_call_id, data["status"], warning="WORKER_BUSY")
        try:
            remote_id = self.gateway.query_task(data["write_idempotency_key"], remote_task_id=data.get("remote_ref"))
            warning = "REMOTE_NOT_FOUND_IN_VISIBLE_SCOPE"
        except TaskLookupUncertain as exc:
            remote_id, warning = None, str(exc)
        except Exception:
            # Do not expose SDK exception messages: they may contain credentials.
            remote_id, warning = None, "REMOTE_QUERY_FAILED"
        if remote_id:
            call = self.control_plane.reconcile_tool_call(tool_call_id, remote_id, "SUCCEEDED")
            return TaskExecutionResult(tool_call_id, call.status.value, call.remote_ref)
        call = self.control_plane.reconcile_tool_call(tool_call_id, remote_status="UNKNOWN", error_code=warning)
        return TaskExecutionResult(tool_call_id, call.status.value, call.remote_ref,
                                   warning=warning if call.status != ToolCallStatus.SUCCEEDED else None)
