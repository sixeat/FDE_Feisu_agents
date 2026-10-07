"""Deterministic owner-decision and task-follow-up reporting contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from .models import IdentityContext


class OwnerDecision(StrEnum):
    ACCEPTED = "ACCEPTED"
    RETURNED = "RETURNED"


@dataclass(frozen=True)
class OwnerDecisionEvent:
    """Explicit owner action, with source_revision bound to the proposal hash.

    Authentication belongs to the adapter. Authority and task bindings are
    independently rechecked against durable control-plane records below.
    This is not a Feishu task-update webhook payload.
    """

    event_id: str
    tenant_id: str
    task_run_id: str
    remote_task_id: str
    owner_actor_id: str
    decision: OwnerDecision | str
    source_revision: str
    reason: str | None = None


class FollowUpStatus(StrEnum):
    WAITING_OWNER = "WAITING_OWNER"
    IN_PROGRESS = "IN_PROGRESS"
    DUE_SOON = "DUE_SOON"
    OVERDUE = "OVERDUE"
    WAITING_HUMAN = "WAITING_HUMAN"
    UNKNOWN = "UNKNOWN"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class RemoteTaskMapping:
    raw_status: str | None
    status: FollowUpStatus
    due_at: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RemoteTaskSnapshot:
    remote_task_id: str | None
    mapping: RemoteTaskMapping


@dataclass(frozen=True)
class FollowUpObservation:
    """One read-only remote observation kept for later reconciliation."""

    observation_id: str
    tenant_id: str
    task_run_id: str
    remote_task_id: str | None
    observed_at: str
    mapping: RemoteTaskMapping


@dataclass(frozen=True)
class DailyFollowUpDigest:
    """A deterministic, unsent daily summary for one tenant and business day."""

    tenant_id: str
    business_date: str
    counts: dict[str, int]
    attention_task_ids: tuple[str, ...]
    notification_key: str

    @property
    def attention_task_run_ids(self) -> tuple[str, ...]:
        """Backward-compatible alias for callers using the old field name."""
        return self.attention_task_ids


@dataclass(frozen=True)
class PersonalAssistantReport:
    task_run_id: str
    owner_actor_id: str
    recipient_actor_id: str
    channel: str
    decision: OwnerDecision
    status: FollowUpStatus
    headline: str
    next_action: str
    reason: str | None
    notification_key: str


def build_owner_decision_report(
    *,
    tenant_id: str,
    task_run_id: str,
    owner_actor_id: str,
    recipient_actor_id: str,
    decision: OwnerDecision | str,
    source_revision: str | int,
    channel: str = "assistant-report",
    reason: str | None = None,
) -> PersonalAssistantReport:
    """Build a report without changing remote task state or assigning work."""
    normalized = OwnerDecision(decision)
    if normalized is OwnerDecision.RETURNED and not reason:
        raise ValueError("returned owner decision requires a reason")
    if normalized is OwnerDecision.ACCEPTED:
        status = FollowUpStatus.IN_PROGRESS
        headline = "负责人已接受任务"
        next_action = "个人助手继续跟进进度，并在临近截止时间时提醒"
    else:
        status = FollowUpStatus.WAITING_HUMAN
        headline = "负责人已退回任务，等待人工处理"
        next_action = "请会议发起人或领域负责人补充信息、改派或取消"
    key_payload = {
        "tenant_id": tenant_id,
        "task_run_id": task_run_id,
        "owner_actor_id": owner_actor_id,
        "recipient_actor_id": recipient_actor_id,
        "channel": channel,
        "decision": normalized.value,
        "source_revision": str(source_revision),
    }
    notification_key = "followup:" + hashlib.sha256(
        json.dumps(key_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PersonalAssistantReport(
        task_run_id=task_run_id,
        owner_actor_id=owner_actor_id,
        recipient_actor_id=recipient_actor_id,
        channel=channel,
        decision=normalized,
        status=status,
        headline=headline,
        next_action=next_action,
        reason=reason,
        notification_key=notification_key,
    )


_REMOTE_STATUS_MAP = {
    "TODO": FollowUpStatus.WAITING_OWNER,
    "OPEN": FollowUpStatus.WAITING_OWNER,
    "PENDING": FollowUpStatus.WAITING_OWNER,
    "NOT_STARTED": FollowUpStatus.WAITING_OWNER,
    "IN_PROGRESS": FollowUpStatus.IN_PROGRESS,
    "DOING": FollowUpStatus.IN_PROGRESS,
    "STARTED": FollowUpStatus.IN_PROGRESS,
    "DONE": FollowUpStatus.COMPLETED,
    "COMPLETED": FollowUpStatus.COMPLETED,
    "COMPLETE": FollowUpStatus.COMPLETED,
    "FINISHED": FollowUpStatus.COMPLETED,
    "CANCELLED": FollowUpStatus.WAITING_HUMAN,
    "CANCELED": FollowUpStatus.WAITING_HUMAN,
    "DELETED": FollowUpStatus.WAITING_HUMAN,
}


def map_remote_task_status(
    remote_status: str | None,
    *,
    due_at: str | None = None,
    now: date | datetime | None = None,
    soon_window: timedelta = timedelta(days=1),
) -> RemoteTaskMapping:
    """Map an observed remote status without treating unknown values as success.

    The SDK exposes the task status as a string and Feishu may add values over
    time. Unknown values therefore remain ``UNKNOWN`` until a mapping is
    explicitly reviewed.
    """
    raw = None if remote_status is None else str(remote_status).strip().upper()
    base = _REMOTE_STATUS_MAP.get(raw or "")
    if base is None:
        return RemoteTaskMapping(raw_status=raw or None, status=FollowUpStatus.UNKNOWN,
                                 due_at=due_at, reason="REMOTE_STATUS_UNRECOGNIZED")
    if base not in {FollowUpStatus.WAITING_OWNER, FollowUpStatus.IN_PROGRESS} or not due_at:
        return RemoteTaskMapping(raw_status=raw, status=base, due_at=due_at)
    try:
        due_date = datetime.fromisoformat(str(due_at).replace("Z", "+00:00")).date()
    except ValueError:
        return RemoteTaskMapping(raw_status=raw, status=base, due_at=due_at, reason="DUE_DATE_INVALID")
    today = (now.date() if isinstance(now, datetime) else now) if now is not None else date.today()
    soon_limit = today + soon_window
    if due_date < today:
        return RemoteTaskMapping(raw_status=raw, status=FollowUpStatus.OVERDUE, due_at=due_at)
    if due_date <= soon_limit:
        return RemoteTaskMapping(raw_status=raw, status=FollowUpStatus.DUE_SOON, due_at=due_at)
    return RemoteTaskMapping(raw_status=raw, status=base, due_at=due_at)


def extract_remote_task_snapshot(
    task: Any,
    *,
    now: date | datetime | None = None,
    soon_window: timedelta = timedelta(days=1),
) -> RemoteTaskSnapshot:
    """Extract only stable fields from a Feishu SDK task or JSON fixture.

    The adapter accepts both generated SDK objects (attribute access) and
    sanitized dictionaries used by offline tests. It never treats a missing
    guid or status as a successful observation.
    """
    if task is None:
        return RemoteTaskSnapshot(None, RemoteTaskMapping(None, FollowUpStatus.UNKNOWN, reason="REMOTE_TASK_PAYLOAD_INVALID"))

    def read(value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    guid = read(task, "guid") or read(task, "task_id")
    status = read(task, "status")
    due = read(task, "due")
    timestamp = read(due, "timestamp") if due is not None else None
    due_at: str | None
    due_unit_invalid = False
    if timestamp in (None, ""):
        due_at = None
    else:
        try:
            milliseconds = float(timestamp)
            due_unit_invalid = abs(milliseconds) < 100_000_000_000
            due_at = (datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
                      .isoformat().replace("+00:00", "Z")) if not due_unit_invalid else str(timestamp)
        except (TypeError, ValueError, OverflowError):
            due_at = str(timestamp)
            due_unit_invalid = True
    mapping = map_remote_task_status(status, due_at=due_at, now=now, soon_window=soon_window)
    if due_unit_invalid:
        mapping = RemoteTaskMapping(mapping.raw_status, FollowUpStatus.UNKNOWN, due_at,
                                    "REMOTE_DUE_TIMESTAMP_UNIT_INVALID")
    # Feishu Task v2 documents only todo/done. Generic adapter aliases above
    # are not evidence that Feishu reports acceptance or in-progress states.
    if mapping.raw_status not in {"TODO", "DONE"}:
        mapping = RemoteTaskMapping(mapping.raw_status, FollowUpStatus.UNKNOWN, due_at,
                                    "REMOTE_STATUS_UNRECOGNIZED")
    if not guid:
        mapping = RemoteTaskMapping(mapping.raw_status, FollowUpStatus.UNKNOWN, mapping.due_at,
                                    "REMOTE_TASK_ID_MISSING")
    return RemoteTaskSnapshot(str(guid) if guid else None, mapping)


def persist_owner_decision_report(
    store: Any, report: PersonalAssistantReport, *, tenant_id: str, observation_id: str,
    remote_task_id: str | None = None,
) -> None:
    """Persist an observation; notification claiming remains a separate step."""
    store.save_follow_up_observation(
        observation_id,
        tenant_id=tenant_id,
        task_run_id=report.task_run_id,
        data={
            "tenant_id": tenant_id,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "task_run_id": report.task_run_id,
            "remote_task_id": remote_task_id,
            "owner_actor_id": report.owner_actor_id,
            "recipient_actor_id": report.recipient_actor_id,
            "channel": report.channel,
            "decision": report.decision.value,
            "status": report.status.value,
            "headline": report.headline,
            "next_action": report.next_action,
            "reason": report.reason,
            "notification_key": report.notification_key,
        },
    )


def _decision_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def confirmed_task_bindings(connection: Any, tenant_id: str, *,
                            task_run_id: str | None = None,
                            remote_task_id: str | None = None) -> list[dict[str, Any]]:
    """Resolve successful task creates against their currently approved version.

    Used by both visibility checks and the transactional owner-action path.
    Returned records are internal; adapters must project only permitted fields.
    """
    rows = connection.execute(
        "SELECT c.data_json AS call_json, a.data_json AS approval_json, t.data_json AS task_json "
        "FROM tool_calls c JOIN task_runs t ON t.task_run_id = json_extract(c.data_json, '$.task_run_id') "
        "JOIN approvals a ON a.task_run_id = t.task_run_id "
        "AND json_extract(a.data_json, '$.proposal.operation_id') = json_extract(c.data_json, '$.operation_id') "
        "WHERE t.tenant_id = ? AND c.status = 'SUCCEEDED' AND a.status = 'APPROVED' "
        "AND (? IS NULL OR t.task_run_id = ?) "
        "AND (? IS NULL OR json_extract(c.data_json, '$.remote_ref') = ?)",
        (tenant_id, task_run_id, task_run_id, remote_task_id, remote_task_id),
    ).fetchall()
    bindings = []
    for row in rows:
        call, proposal, task = (json.loads(row["call_json"]), json.loads(row["approval_json"])["proposal"],
                                json.loads(row["task_json"]))
        if (proposal["operation_type"] != "task.create" or not call.get("remote_ref")
                or call["write_idempotency_key"] != _decision_hash({
                    "operation_id": proposal["operation_id"], "proposal_hash": proposal["proposal_hash"]})):
            continue
        bindings.append({"call": call, "proposal": proposal, "task": task})
    return bindings


def remote_due_mismatch(approved_due_date: str | None, observation: dict[str, Any]) -> bool:
    """Compare a date-only approved deadline with the last remote observation."""
    if not approved_due_date:
        return False
    remote_due = observation.get("due_at")
    if not remote_due:
        return True
    try:
        return date.fromisoformat(str(remote_due)[:10]) != date.fromisoformat(str(approved_due_date))
    except ValueError:
        return True


def apply_owner_decision_event(
    store: Any,
    event: OwnerDecisionEvent,
    *,
    identity: IdentityContext,
) -> dict[str, Any]:
    """Record one authenticated owner's decision atomically, without I/O.

    One approved task version can receive one decision. Later corrections
    require a separate human-reviewed workflow, not a last-write-wins click.
    ``identity`` must come from the adapter's verified login/callback, never
    from the request body. Rejections do not consume another actor's event ID.
    """
    if not all((event.event_id, event.tenant_id, event.task_run_id,
                event.remote_task_id, event.source_revision, event.owner_actor_id)):
        raise ValueError("owner decision event identity is required")
    no_effect = {"remote_write": False, "notification_sent": False}

    def denied(reason: str) -> dict[str, Any]:
        return {"accepted": False, "reason": reason, **no_effect}

    if (identity.auth_mode != "user_oauth" or identity.tenant_id != event.tenant_id
            or identity.actor_id != event.owner_actor_id):
        return denied("OWNER_IDENTITY_MISMATCH")
    try:
        decision = OwnerDecision(event.decision)
    except ValueError:
        return denied("OWNER_DECISION_INVALID")
    reason = (event.reason or "").strip() or None
    if decision is OwnerDecision.RETURNED and not reason:
        return denied("RETURN_REASON_REQUIRED")
    payload_hash = _decision_hash({
        "tenant": event.tenant_id, "run": event.task_run_id, "remote": event.remote_task_id,
        "actor": identity.actor_id, "revision": event.source_revision,
        "decision": decision.value, "reason": reason,
    })
    event_key = "owner-decision:" + _decision_hash([event.tenant_id, "explicit-owner-action", event.event_id])
    action_key = "owner-action:" + _decision_hash([event.tenant_id, event.task_run_id,
                                            event.remote_task_id, event.source_revision])
    with store.owner_decision_transaction() as connection:
        actor = connection.execute("SELECT * FROM actors WHERE actor_id = ?", (identity.actor_id,)).fetchone()
        if (actor is None or not actor["active"] or actor["actor_type"] != "USER"
                or actor["tenant_id"] != identity.tenant_id
                or not identity.subject_ref_hash or actor["external_ref_hash"] != identity.subject_ref_hash):
            return denied("OWNER_IDENTITY_INVALID")
        row = connection.execute("SELECT data_json FROM task_runs WHERE task_run_id = ? AND tenant_id = ?",
                                 (event.task_run_id, event.tenant_id)).fetchone()
        if row is None:
            return denied("OWNER_TASK_NOT_FOUND")
        task = json.loads(row[0])
        matches = confirmed_task_bindings(connection, event.tenant_id, task_run_id=event.task_run_id,
                                         remote_task_id=event.remote_task_id)
        if len(matches) != 1:
            return denied("OWNER_TASK_BINDING_INVALID")
        proposal = matches[0]["proposal"]
        if proposal["arguments"].get("assignee_actor_id") != identity.actor_id:
            return denied("OWNER_NOT_ASSIGNED")
        if proposal["proposal_hash"] != event.source_revision:
            return denied("OWNER_ACTION_STALE")

        # Authenticate and resolve the current authority BEFORE reading a replay.
        prior = connection.execute("SELECT result_json FROM inbox_events WHERE event_key = ?", (event_key,)).fetchone()
        if prior:
            saved = json.loads(prior[0])
            if saved["payload_hash"] != payload_hash:
                return denied("OWNER_EVENT_PAYLOAD_MISMATCH")
            return {**saved["result"], "duplicate": True}
        existing = connection.execute("SELECT data_json FROM follow_up_observations WHERE observation_id = ?",
                                      (action_key,)).fetchone()
        if existing:
            saved = json.loads(existing[0])
            if saved["payload_hash"] != payload_hash:
                return denied("OWNER_DECISION_ALREADY_RECORDED")
            result = {"accepted": True, "status": saved["status"], "task_run_id": event.task_run_id,
                      "remote_task_id": event.remote_task_id, "duplicate": True, **no_effect}
            connection.execute("INSERT INTO inbox_events(event_key, result_json) VALUES (?, ?)",
                               (event_key, json.dumps({"payload_hash": payload_hash, "result": result})))
            return result
        # Known terminal or uncertain remote evidence requires reconciliation.
        remote_rows = connection.execute(
            "SELECT data_json FROM follow_up_observations WHERE tenant_id = ? AND task_run_id = ? "
            "AND json_extract(data_json, '$.remote_task_id') = ?",
            (event.tenant_id, event.task_run_id, event.remote_task_id),
        ).fetchall()
        remote = [json.loads(r[0]) for r in remote_rows if not json.loads(r[0]).get("decision")]
        if remote:
            latest = max(remote, key=lambda r: str(r.get("observed_at") or ""))
            if remote_due_mismatch(proposal["arguments"].get("due_date"), latest):
                return denied("OWNER_DUE_RECONCILIATION_REQUIRED")
            if latest.get("status") in {"COMPLETED", "WAITING_HUMAN", "UNKNOWN"}:
                return denied("OWNER_TASK_REQUIRES_RECONCILIATION")
        report = build_owner_decision_report(
            tenant_id=event.tenant_id,
            task_run_id=event.task_run_id, owner_actor_id=identity.actor_id,
            recipient_actor_id=task["requested_by"], decision=decision,
            source_revision=event.source_revision, reason=reason,
        )
        result = {
            "accepted": True, "status": report.status.value, "task_run_id": event.task_run_id,
            "remote_task_id": event.remote_task_id, **no_effect,
        }
        observation = {
            "observation_id": action_key, "tenant_id": event.tenant_id, "task_run_id": event.task_run_id,
            "remote_task_id": event.remote_task_id, "source_revision": event.source_revision,
            "owner_actor_id": identity.actor_id, "recipient_actor_id": task["requested_by"],
            "decision": decision.value, "status": report.status.value, "reason": reason,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "payload_hash": payload_hash,
        }
        connection.execute(
            "INSERT INTO follow_up_observations(observation_id, tenant_id, task_run_id, data_json) VALUES (?, ?, ?, ?)",
            (action_key, event.tenant_id, event.task_run_id, json.dumps(observation)),
        )
        connection.execute("INSERT INTO inbox_events(event_key, result_json) VALUES (?, ?)",
                           (event_key, json.dumps({"payload_hash": payload_hash, "result": result})))
        audit = {"audit_id": action_key, "event_type": "OWNER_DECISION_RECORDED", "tenant_id": event.tenant_id,
                 "actor_id": identity.actor_id, "task_run_id": event.task_run_id, "correlation_id": event_key,
                 "payload_hash": payload_hash, "outcome": decision.value}
        connection.execute(
            "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) VALUES (?, ?, ?, ?, ?)",
            (action_key, audit["event_type"], event.tenant_id, event.task_run_id, json.dumps(audit)),
        )
    return result


def build_remote_task_observation(
    *,
    tenant_id: str,
    task_run_id: str,
    snapshot: RemoteTaskSnapshot,
    observed_at: datetime | str | None = None,
) -> FollowUpObservation:
    """Build a durable observation without triggering any notification.

    The observation ID is stable for the same task state at the same instant,
    which makes repeated event delivery safe while preserving later changes.
    ``remote_task_id`` remains an internal relation and is never intended for
    user-facing logs.
    """
    if not tenant_id or not task_run_id:
        raise ValueError("tenant_id and task_run_id are required")
    if observed_at is None:
        observed_value = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    elif isinstance(observed_at, datetime):
        value = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
        observed_value = value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    else:
        observed_value = str(observed_at)
    key_payload = {
        "tenant_id": tenant_id,
        "task_run_id": task_run_id,
        "remote_task_id": snapshot.remote_task_id,
        "raw_status": snapshot.mapping.raw_status,
        "status": snapshot.mapping.status.value,
        "due_at": snapshot.mapping.due_at,
        "reason": snapshot.mapping.reason,
        "observed_at": observed_value,
    }
    observation_id = "remote-followup:" + hashlib.sha256(
        json.dumps(key_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FollowUpObservation(
        observation_id=observation_id,
        tenant_id=tenant_id,
        task_run_id=task_run_id,
        remote_task_id=snapshot.remote_task_id,
        observed_at=observed_value,
        mapping=snapshot.mapping,
    )


def persist_remote_task_observation(store: Any, observation: FollowUpObservation) -> None:
    """Persist one observation; sending remains a separate operation."""
    store.save_follow_up_observation(
        observation.observation_id,
        tenant_id=observation.tenant_id,
        task_run_id=observation.task_run_id,
        data={
            "observation_id": observation.observation_id,
            "tenant_id": observation.tenant_id,
            "task_run_id": observation.task_run_id,
            "remote_task_id": observation.remote_task_id,
            "observed_at": observation.observed_at,
            "raw_status": observation.mapping.raw_status,
            "status": observation.mapping.status.value,
            "due_at": observation.mapping.due_at,
            "reason": observation.mapping.reason,
        },
    )


def build_daily_follow_up_digest(
    *,
    tenant_id: str,
    business_date: date | str,
    observations: list[dict[str, Any]],
    recipient_actor_id: str,
    channel: str = "assistant-daily-summary",
) -> DailyFollowUpDigest:
    """Aggregate the latest observation for each remote task into a digest.

    This is a current-state snapshot, not a filter for events on business_date.
    Remote evidence and explicit owner decisions are reduced independently:
    a later remote TODO cannot erase an acceptance or return. Business date
    scopes the unsent notification key only.
    """
    if not tenant_id or not recipient_actor_id:
        raise ValueError("tenant_id and recipient_actor_id are required")
    day = business_date.isoformat() if isinstance(business_date, date) else str(business_date)
    latest: dict[str, dict[str, Any]] = {}
    owner_latest: dict[str, dict[str, Any]] = {}
    for item in observations:
        if not isinstance(item, dict) or item.get("tenant_id") not in (None, tenant_id):
            continue
        task_run_id = str(item.get("task_run_id") or "")
        if not task_run_id:
            continue
        remote_task_id = str(item.get("remote_task_id") or "")
        # A single TaskRun can create several remote tasks (for example, one
        # meeting todo per owner). Prefer the remote task as the observation
        # identity and fall back to TaskRun for older observations that lack it.
        observation_key = "remote:" + remote_task_id if remote_task_id else "run:" + task_run_id
        target = owner_latest if item.get("decision") in {"ACCEPTED", "RETURNED"} else latest
        current = target.get(observation_key)
        if current is None or str(item.get("observed_at") or "") >= str(current.get("observed_at") or ""):
            target[observation_key] = item
    for key, owner in owner_latest.items():
        remote = latest.get(key)
        if remote is None:
            latest[key] = owner
        elif remote.get("status") == "UNKNOWN":
            continue
        elif owner["decision"] == "RETURNED":
            latest[key] = {**remote, "status": FollowUpStatus.WAITING_HUMAN.value}
        elif remote.get("status") == FollowUpStatus.WAITING_OWNER.value:
            latest[key] = {**remote, "status": FollowUpStatus.IN_PROGRESS.value}
    counts = {status.value: 0 for status in FollowUpStatus}
    attention: list[str] = []
    for observation_key in sorted(latest):
        item = latest[observation_key]
        status = str(item.get("status") or FollowUpStatus.UNKNOWN.value)
        if status not in counts:
            status = FollowUpStatus.UNKNOWN.value
        counts[status] += 1
        if status in {FollowUpStatus.DUE_SOON.value, FollowUpStatus.OVERDUE.value,
                      FollowUpStatus.WAITING_HUMAN.value, FollowUpStatus.UNKNOWN.value}:
            attention.append(str(item.get("remote_task_id") or item.get("task_run_id")))
    key_payload = {
        "tenant_id": tenant_id,
        "business_date": day,
        "recipient_actor_id": recipient_actor_id,
        "channel": channel,
    }
    notification_key = "followup-daily:" + hashlib.sha256(
        json.dumps(key_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return DailyFollowUpDigest(
        tenant_id=tenant_id,
        business_date=day,
        counts=counts,
        attention_task_ids=tuple(attention),
        notification_key=notification_key,
    )


__all__ = [
    "FollowUpStatus",
    "FollowUpObservation",
    "DailyFollowUpDigest",
    "OwnerDecision",
    "OwnerDecisionEvent",
    "PersonalAssistantReport",
    "build_owner_decision_report",
    "RemoteTaskMapping",
    "RemoteTaskSnapshot",
    "extract_remote_task_snapshot",
    "map_remote_task_status",
    "persist_owner_decision_report",
    "apply_owner_decision_event",
    "build_remote_task_observation",
    "persist_remote_task_observation",
    "build_daily_follow_up_digest",
]
