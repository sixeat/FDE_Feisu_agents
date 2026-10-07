"""Organizer-approved repair of a created Feishu task's all-day deadline."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Any

from .follow_up import (
    build_remote_task_observation, confirmed_task_bindings, extract_remote_task_snapshot,
    persist_remote_task_observation, remote_due_mismatch,
)
from .models import IdentityContext, RiskLevel, ToolDefinition


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _all_day(task: Any) -> bool:
    due = task.get("due") if isinstance(task, dict) else getattr(task, "due", None)
    return (due.get("is_all_day") if isinstance(due, dict) else getattr(due, "is_all_day", None)) is True


def _audit(connection: Any, correction: dict[str, Any], event: str, outcome: str) -> None:
    data = {
        "audit_id": "audit_" + uuid.uuid4().hex, "event_type": event,
        "tenant_id": correction["tenant_id"], "actor_id": correction["requested_by"],
        "task_run_id": correction["task_run_id"],
        "correlation_id": correction["correction_id"],
        "payload_hash": correction["proposal_hash"], "outcome": outcome,
    }
    connection.execute(
        "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) VALUES (?, ?, ?, ?, ?)",
        (data["audit_id"], event, data["tenant_id"], data["task_run_id"], json.dumps(data)),
    )


def latest_remote_observation(store: Any, task_run_id: str, remote_id: str) -> dict[str, Any] | None:
    rows = [row for row in store.list_follow_up_observations(task_run_id)
            if row.get("remote_task_id") == remote_id and not row.get("decision")]
    return max(enumerate(rows), key=lambda entry: (entry[1].get("observed_at", ""), entry[0]))[1] if rows else None


def get_correction(store: Any, correction_id: str) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT data_json FROM task_due_corrections WHERE correction_id = ?", (correction_id,),
    ).fetchone()
    return json.loads(row[0]) if row else None


def correction_for_task(store: Any, tenant_id: str, remote_id: str) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT data_json FROM task_due_corrections WHERE tenant_id = ? AND remote_task_id = ? "
        "ORDER BY rowid DESC LIMIT 1", (tenant_id, remote_id),
    ).fetchone()
    return json.loads(row[0]) if row else None


def propose_due_correction(store: Any, binding: dict[str, Any], identity: IdentityContext) -> dict[str, Any]:
    task, call, proposal = binding["task"], binding["call"], binding["proposal"]
    if identity.auth_mode != "user_oauth" or identity.tenant_id != task["tenant_id"] or identity.actor_id != task["requested_by"]:
        raise PermissionError("MEETING_ORGANIZER_REQUIRED")
    due_date = proposal["arguments"].get("due_date")
    if not due_date:
        raise ValueError("APPROVED_DUE_MISSING")
    date.fromisoformat(due_date)
    remote = latest_remote_observation(store, task["task_run_id"], call["remote_ref"])
    if (remote is None or remote.get("raw_status") != "TODO" or remote.get("status") == "UNKNOWN"
            or not remote_due_mismatch(due_date, remote)):
        raise ValueError("REMOTE_DUE_REVIEW_REQUIRED")
    try:
        date.fromisoformat(str(remote.get("due_at"))[:10])
    except ValueError as exc:
        raise ValueError("REMOTE_DUE_REVIEW_REQUIRED") from exc
    with store.owner_decision_transaction() as connection:
        actor = connection.execute("SELECT * FROM actors WHERE actor_id = ?", (identity.actor_id,)).fetchone()
        if not actor or not actor["active"] or actor["tenant_id"] != identity.tenant_id or actor["external_ref_hash"] != identity.subject_ref_hash:
            raise PermissionError("MEETING_ORGANIZER_REQUIRED")
        active = connection.execute(
            "SELECT data_json FROM task_due_corrections WHERE tenant_id = ? AND remote_task_id = ? "
            "AND status IN ('PENDING', 'APPROVED', 'DISPATCHED', 'UNKNOWN')",
            (identity.tenant_id, call["remote_ref"]),
        ).fetchone()
        if active:
            current = json.loads(active[0])
            if current["observation_id"] != remote["observation_id"]:
                if current["status"] != "PENDING":
                    raise ValueError("REMOTE_OBSERVATION_CHANGED")
                current["status"] = "INVALIDATED"
                connection.execute("UPDATE task_due_corrections SET status = ?, data_json = ? WHERE correction_id = ?",
                                   ("INVALIDATED", json.dumps(current), current["correction_id"]))
                _audit(connection, current, "TASK_DUE_CORRECTION_INVALIDATED", "INVALIDATED")
            else:
                return current
        current = latest_remote_observation(store, task["task_run_id"], call["remote_ref"])
        if current != remote:
            raise ValueError("REMOTE_OBSERVATION_CHANGED")
        correction = {
            "correction_id": "due_" + uuid.uuid4().hex,
            "tenant_id": identity.tenant_id, "task_run_id": task["task_run_id"],
            "remote_task_id": call["remote_ref"], "source_revision": proposal["proposal_hash"],
            "requested_by": identity.actor_id, "assignee_actor_id": proposal["arguments"].get("assignee_actor_id"),
            "approved_due_date": due_date, "observed_due_at": remote.get("due_at"),
            "observation_id": remote["observation_id"], "status": "PENDING",
        }
        correction["proposal_hash"] = _hash(correction)
        connection.execute(
            "INSERT INTO task_due_corrections(correction_id, tenant_id, task_run_id, remote_task_id, status, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (correction["correction_id"], identity.tenant_id, task["task_run_id"], call["remote_ref"],
             "PENDING", json.dumps(correction)),
        )
        _audit(connection, correction, "TASK_DUE_CORRECTION_PROPOSED", "PENDING")
    return correction


def approve_due_correction(store: Any, correction_id: str, proposal_hash: str,
                           identity: IdentityContext) -> dict[str, Any]:
    with store.owner_decision_transaction() as connection:
        row = connection.execute(
            "SELECT data_json FROM task_due_corrections WHERE correction_id = ?", (correction_id,),
        ).fetchone()
        if not row:
            raise KeyError(correction_id)
        correction = json.loads(row[0])
        actor = connection.execute("SELECT * FROM actors WHERE actor_id = ?", (identity.actor_id,)).fetchone()
        if (identity.auth_mode != "user_oauth" or not actor or not actor["active"]
                or actor["tenant_id"] != correction["tenant_id"]
                or actor["external_ref_hash"] != identity.subject_ref_hash
                or identity.actor_id != correction["requested_by"]):
            raise PermissionError("MEETING_ORGANIZER_REQUIRED")
        if correction["proposal_hash"] != proposal_hash or correction["status"] != "PENDING":
            raise ValueError("CORRECTION_STALE")
        observation = latest_remote_observation(store, correction["task_run_id"], correction["remote_task_id"])
        if not observation or observation["observation_id"] != correction["observation_id"]:
            raise ValueError("REMOTE_OBSERVATION_CHANGED")
        correction["status"] = "APPROVED"
        connection.execute("UPDATE task_due_corrections SET status = ?, data_json = ? WHERE correction_id = ?",
                           ("APPROVED", json.dumps(correction), correction_id))
        _audit(connection, correction, "TASK_DUE_CORRECTION_APPROVED", "APPROVED")
    return correction


class TaskDueCorrectionWorker:
    """At-most-once patch dispatch; uncertain outcomes require read-only reconciliation."""

    def __init__(self, control_plane: Any, gateway: Any) -> None:
        self.cp = control_plane
        self.gateway = gateway

    def _set_status(self, correction: dict[str, Any], status: str, event: str) -> dict[str, Any]:
        with self.cp.store.owner_decision_transaction() as connection:
            row = connection.execute("SELECT status FROM task_due_corrections WHERE correction_id = ?",
                                     (correction["correction_id"],)).fetchone()
            if row is None or row[0] != correction["status"]:
                raise RuntimeError("CORRECTION_STATE_CHANGED")
            correction = {**correction, "status": status}
            connection.execute("UPDATE task_due_corrections SET status = ?, data_json = ? WHERE correction_id = ?",
                               (status, json.dumps(correction), correction["correction_id"]))
            _audit(connection, correction, event, status)
        return correction

    def execute(self, correction_id: str) -> dict[str, Any]:
        correction = get_correction(self.cp.store, correction_id)
        if correction is None or correction["status"] != "APPROVED":
            raise ValueError("CORRECTION_NOT_APPROVED")
        bindings = confirmed_task_bindings(
            self.cp.store.connection, correction["tenant_id"],
            task_run_id=correction["task_run_id"], remote_task_id=correction["remote_task_id"],
        )
        if len(bindings) != 1 or bindings[0]["proposal"]["proposal_hash"] != correction["source_revision"]:
            return self._set_status(correction, "FAILED", "TASK_DUE_CORRECTION_BINDING_FAILED")
        permission = self.cp.authorize_operation(correction["task_run_id"], ToolDefinition(
            "task.patch_due", frozenset({"task.write"}), RiskLevel.HIGH, True,
        ))
        if not permission.allowed:
            return self._set_status(correction, "FAILED", "TASK_DUE_CORRECTION_PERMISSION_FAILED")
        correction = self._set_status(correction, "DISPATCHED", "TASK_DUE_CORRECTION_DISPATCHED")
        try:
            before = extract_remote_task_snapshot(self.gateway.get_task(correction["remote_task_id"]))
            if (before.remote_task_id != correction["remote_task_id"] or before.mapping.raw_status != "TODO"
                    or before.mapping.due_at != correction["observed_due_at"]):
                return self._set_status(correction, "UNKNOWN", "TASK_DUE_CORRECTION_PRECHECK_CHANGED")
        except Exception:
            return self._set_status(correction, "UNKNOWN", "TASK_DUE_CORRECTION_PRECHECK_UNKNOWN")
        try:
            self.gateway.patch_task_due(correction["remote_task_id"], correction["approved_due_date"])
        except Exception:
            return self._set_status(correction, "UNKNOWN", "TASK_DUE_CORRECTION_PATCH_UNKNOWN")
        return self.reconcile(correction_id)

    def reconcile(self, correction_id: str) -> dict[str, Any]:
        correction = get_correction(self.cp.store, correction_id)
        if correction is None or correction["status"] not in {"DISPATCHED", "UNKNOWN"}:
            raise ValueError("CORRECTION_NOT_UNCERTAIN")
        try:
            remote_task = self.gateway.get_task(correction["remote_task_id"])
            snapshot = extract_remote_task_snapshot(remote_task)
            if snapshot.remote_task_id != correction["remote_task_id"]:
                return correction
            if (snapshot.mapping.raw_status == "TODO" and _all_day(remote_task) and snapshot.mapping.due_at
                    and date.fromisoformat(snapshot.mapping.due_at[:10]) == date.fromisoformat(correction["approved_due_date"])):
                persist_remote_task_observation(self.cp.store, build_remote_task_observation(
                    tenant_id=correction["tenant_id"], task_run_id=correction["task_run_id"], snapshot=snapshot,
                ))
                return self._set_status(correction, "SUCCEEDED", "TASK_DUE_CORRECTION_VERIFIED")
        except Exception:
            pass
        if correction["status"] == "DISPATCHED":
            return self._set_status(correction, "UNKNOWN", "TASK_DUE_CORRECTION_RECONCILIATION_UNKNOWN")
        return correction
