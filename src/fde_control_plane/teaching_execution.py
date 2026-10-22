"""Durable, read-only teaching source extraction for one authorized member."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import ActorType, Delegation, IdentityContext, TaskRunStatus, TaskStep
from .runtime import RunRequest, RuntimeAdapter, RuntimeResult
from .teaching import SourceChanged, TeachingService
from .teaching_agent import TeachingContractError, validate_teaching_source_output


class TeachingSourceExecutionService:
    def __init__(self, teaching: TeachingService, runtime: RuntimeAdapter,
                 *, allowed_agent_id: str | None = None) -> None:
        self.teaching = teaching
        self.cp = teaching.cp
        self.store = teaching.store
        self.runtime = runtime
        self.allowed_agent_id = allowed_agent_id

    def _authorized_agent(self, identity: IdentityContext, agent_id: str, version: int) -> str:
        if self.allowed_agent_id is not None and agent_id != self.allowed_agent_id:
            raise PermissionError("teaching extraction Agent is not configured")
        member = self.teaching._member(identity)
        binding = self.store.get_binding(member.actor_id, identity.tenant_id)
        if binding is None:
            raise PermissionError("member has no personal assistant binding")
        assistant_id = binding["assistant_actor_id"]
        assistant = self.cp._actor(assistant_id)
        agent = self.cp._actor(agent_id)
        agent_version = self.cp._agent_version(agent_id, version)
        required = {"doc.read"}
        if (assistant.actor_type != ActorType.PERSONAL_ASSISTANT
                or agent.actor_type != ActorType.BUSINESS_AGENT
                or any(not actor.active or actor.tenant_id != identity.tenant_id
                       for actor in (member, assistant, agent))
                or not agent_version.active
                or any(not required.issubset(actor.capabilities)
                       for actor in (member, assistant, agent))
                or not required.issubset(agent_version.capabilities)):
            raise PermissionError("read-only teaching extraction is not authorized")
        route = self.store.get_agent_route(identity.tenant_id, agent_id)
        if route is not None and self.store.select_agent_version(
                identity.tenant_id, agent_id, identity.actor_id) != version:
            raise PermissionError("teaching Agent version is not routed to this member")
        return assistant_id

    def _persist_events(self, task_run_id: str, trace_id: str, result: RuntimeResult) -> None:
        events = result.events
        sequences = [event.sequence for event in events]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            raise ValueError("RUNTIME_EVENT_SEQUENCE_INVALID")
        for event in events:
            if (event.task_run_id != task_run_id or event.trace_id != trace_id
                    or event.sequence <= 0):
                raise ValueError("RUNTIME_EVENT_OWNER_INVALID")
            data = {"task_run_id": task_run_id, "trace_id": trace_id,
                    "sequence": event.sequence, "event_type": event.event_type,
                    "occurred_at": event.occurred_at, "summary": event.summary}
            key = f"{task_run_id}:{event.sequence}"
            prior = self.store.get_runtime_event(key)
            if prior is not None and prior != data:
                raise ValueError("RUNTIME_EVENT_CONFLICT")
            if prior is None:
                self.store.save_runtime_event(key, task_run_id, event.sequence, data)

    def _check_current(self, connection: Any, identity: IdentityContext,
                       course_id: str, source: dict[str, Any], assistant_id: str,
                       agent_id: str, version: int) -> None:
        actor = connection.execute(
            "SELECT active, external_ref_hash, capabilities_json FROM actors "
            "WHERE tenant_id = ? AND actor_id = ?", (identity.tenant_id, identity.actor_id),
        ).fetchone()
        member = connection.execute(
            "SELECT role, active FROM teaching_course_members WHERE tenant_id = ? "
            "AND course_id = ? AND actor_id = ?",
            (identity.tenant_id, course_id, identity.actor_id),
        ).fetchone()
        bound = connection.execute(
            "SELECT 1 FROM assistant_bindings WHERE tenant_id = ? AND member_actor_id = ? "
            "AND assistant_actor_id = ? AND is_default = 1",
            (identity.tenant_id, identity.actor_id, assistant_id),
        ).fetchone()
        agents = connection.execute(
            "SELECT actor_id, active, capabilities_json FROM actors WHERE tenant_id = ? "
            "AND actor_id IN (?, ?)", (identity.tenant_id, assistant_id, agent_id),
        ).fetchall()
        agent_version = connection.execute(
            "SELECT active, capabilities_json FROM agent_versions WHERE agent_id = ? AND version = ?",
            (agent_id, version),
        ).fetchone()
        row = connection.execute(
            "SELECT revision_id, content_hash, kind, submitted_by, data_reviewed "
            "FROM teaching_sources WHERE tenant_id = ? AND course_id = ? AND source_id = ?",
            (identity.tenant_id, course_id, source["source_id"]),
        ).fetchone()
        if (actor is None or not actor["active"] or actor["external_ref_hash"] != identity.subject_ref_hash
                or "doc.read" not in json.loads(actor["capabilities_json"])
                or member is None or not member["active"] or bound is None
                or len(agents) != 2 or any(not agent["active"]
                   or "doc.read" not in json.loads(agent["capabilities_json"]) for agent in agents)
                or agent_version is None or not agent_version["active"]
                or "doc.read" not in json.loads(agent_version["capabilities_json"])):
            raise PermissionError("teaching extraction authorization was revoked")
        if (row is None or not row["data_reviewed"]
                or any(row[key] != source[key] for key in ("revision_id", "content_hash", "kind"))):
            raise SourceChanged("teaching extraction source changed")
        if member["role"] != "LEAD" and row["submitted_by"] != identity.actor_id:
            shared = connection.execute(
                "SELECT active FROM teaching_source_readers WHERE tenant_id = ? AND course_id = ? "
                "AND source_id = ? AND actor_id = ?",
                (identity.tenant_id, course_id, source["source_id"], identity.actor_id),
            ).fetchone()
            if shared is None or not shared["active"]:
                raise PermissionError("teaching source sharing was revoked")
        route = connection.execute(
            "SELECT stable_version, canary_version, volunteer_ids_json, status FROM agent_routes "
            "WHERE tenant_id = ? AND agent_id = ?", (identity.tenant_id, agent_id),
        ).fetchone()
        if route is not None:
            selected = (route["canary_version"] if route["status"] == "CANARY"
                        and identity.actor_id in json.loads(route["volunteer_ids_json"])
                        else route["stable_version"])
            if route["status"] == "ARCHIVED" or selected != version:
                raise PermissionError("teaching Agent routing changed")

    @staticmethod
    def _result_summary(task_run_id: str, status: str, *, item_count: int = 0,
                        deduplicated: bool = False) -> dict[str, Any]:
        return {"task_run_id": task_run_id, "status": status,
                "item_count": item_count, "deduplicated": deduplicated,
                "allowed_tools": []}

    def execute(self, identity: IdentityContext, course_id: str, source_id: str,
                agent_id: str, agent_version: int, *, deadline_seconds: int = 120) -> dict[str, Any]:
        if deadline_seconds < 1:
            raise ValueError("runtime deadline must be positive")
        source = self.teaching.read_source(identity, course_id, source_id)
        if source["kind"] not in {"SYLLABUS", "WEEKLY_PLAN", "LESSON_PLAN"}:
            raise ValueError("source kind cannot be extracted as a teaching outline")
        assistant_id = self._authorized_agent(identity, agent_id, agent_version)
        ref = {key: source[key] for key in ("source_id", "revision_id", "content_hash", "kind")}
        fingerprint = {"tenant_id": identity.tenant_id, "course_id": course_id,
                       "requested_by": identity.actor_id, "agent_id": agent_id,
                       "agent_version": agent_version, "source": ref}
        digest = hashlib.sha256(self.store._json(fingerprint).encode()).hexdigest()
        task_id = "teaching-extract-" + secrets.token_hex(12)
        task = self.cp.create_task_run(
            identity.actor_id, assistant_id, agent_id, agent_version,
            input_ref_hash=digest, task_run_id=task_id, identity=identity,
            dispatch_key=f"teaching:extract:{digest}",
        )
        task_id = task.task_run_id
        prior = self.store.connection.execute(
            "SELECT 1 FROM teaching_extraction_attempts WHERE task_run_id = ?", (task_id,),
        ).fetchone()
        if prior is not None:
            result = self.store.connection.execute(
                "SELECT data_json FROM teaching_extraction_results WHERE task_run_id = ?", (task_id,),
            ).fetchone()
            return self._result_summary(task_id, task.status.value,
                                        item_count=len(json.loads(result["data_json"])["items"]) if result else 0,
                                        deduplicated=True)
        if task.status != TaskRunStatus.QUEUED:
            # A crash before the attempt marker is not safe to infer as an unstarted run.
            return self._result_summary(task_id, task.status.value, deduplicated=True)
        try:
            with self.store.teaching_transaction() as connection:
                self._check_current(connection, identity, course_id, source,
                                    assistant_id, agent_id, agent_version)
                claimed = connection.execute(
                    "INSERT OR IGNORE INTO teaching_extraction_attempts(task_run_id, tenant_id, course_id, source_id, "
                    "source_revision, source_hash, requested_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (task_id, identity.tenant_id, course_id, source_id, source["revision_id"],
                     source["content_hash"], identity.actor_id),
                ).rowcount == 1
        except (PermissionError, SourceChanged):
            self.cp.transition_task(task_id, TaskRunStatus.PLANNING)
            self.cp.transition_task(task_id, TaskRunStatus.FAILED, "TEACHING_INPUT_REVOKED")
            return self._result_summary(task_id, "FAILED")
        if not claimed:
            return self._result_summary(task_id, self.cp._task(task_id).status.value,
                                        deduplicated=True)
        self.cp.transition_task(task_id, TaskRunStatus.PLANNING)
        step_id = f"teaching-extract-step-{task_id}"
        self.cp.create_task_step(TaskStep(step_id, task_id, "teaching_source.extract", sequence=1))
        delegation_id = f"teaching-extract-delegation-{task_id}"
        self.cp.delegate(Delegation(delegation_id, task_id, assistant_id, agent_id,
                                    frozenset({"doc.read"})))
        self.cp.transition_task(task_id, TaskRunStatus.RUNNING)
        # The claim was committed before any lifecycle work or model call.
        trace_id = "teaching-trace-" + secrets.token_hex(12)
        request = RunRequest(
            task_run_id=task_id, trace_id=trace_id, actor_ref=identity.actor_id,
            agent_version=f"{agent_id}@{agent_version}", input_ref=ref | {"blocks": source["blocks"]},
            tool_policy_snapshot={"allowed_tools": []},
            deadline=(datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)).isoformat(),
        )
        try:
            result = self.runtime.run_once(request)
        except Exception:
            result = RuntimeResult("FAILED", None, None, error_code="RUNTIME_ADAPTER_EXCEPTION")
        try:
            self._persist_events(task_id, trace_id, result)
        except ValueError:
            self.cp.transition_task(task_id, TaskRunStatus.FAILED, "RUNTIME_EVENT_INVALID")
            return self._result_summary(task_id, "FAILED")
        if result.status == "UNKNOWN":
            self.cp.transition_task(task_id, TaskRunStatus.RECONCILING,
                                    result.error_code or "RUNTIME_RESULT_UNKNOWN")
            return self._result_summary(task_id, "RECONCILING")
        if result.status != "SUCCEEDED" or result.payload is None:
            self.cp.transition_task(task_id, TaskRunStatus.FAILED,
                                    result.error_code or "RUNTIME_RESULT_FAILED")
            return self._result_summary(task_id, "FAILED")
        try:
            validated = validate_teaching_source_output(source, result.payload)
            current = self.teaching.read_source(identity, course_id, source_id)
            if (current["revision_id"] != source["revision_id"]
                    or current["content_hash"] != source["content_hash"]):
                raise SourceChanged("source changed during teaching extraction")
            self._authorized_agent(identity, agent_id, agent_version)
            items = [{key: item[key] for key in
                      ("item_id", "kind", "week_number", "block_index", "start_offset", "end_offset")}
                     | {"quote_hash": hashlib.sha256(item["quote"].encode()).hexdigest()}
                     for item in validated["items"]]
            stored = {"source": ref, "items": items}
            output_hash = hashlib.sha256(self.store._json(stored).encode()).hexdigest()
            event = self.teaching._audit(identity, "TEACHING_EXTRACTION_CREATED",
                                         {"task_run_id": task_id, "output_hash": output_hash})
            with self.store.teaching_transaction() as connection:
                self._check_current(connection, identity, course_id, source,
                                    assistant_id, agent_id, agent_version)
                connection.execute(
                    "INSERT INTO teaching_extraction_results(task_run_id, tenant_id, course_id, source_id, "
                    "output_hash, data_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, identity.tenant_id, course_id, source_id,
                     output_hash, self.store._json(stored)),
                )
                self.store.teaching_audit_in_transaction(connection, event)
        except (TeachingContractError, SourceChanged, PermissionError):
            self.cp.transition_task(task_id, TaskRunStatus.FAILED, "TEACHING_OUTPUT_REJECTED")
            return self._result_summary(task_id, "FAILED")
        self.store.save_task_step(TaskStep(step_id, task_id, "teaching_source.extract", "SUCCEEDED", 1))
        self.cp.transition_task(task_id, TaskRunStatus.WAITING_REVIEW)
        return self._result_summary(task_id, "WAITING_REVIEW", item_count=len(items))

    def read_candidates(self, identity: IdentityContext, course_id: str,
                        task_run_id: str) -> dict[str, Any]:
        row = self.store.connection.execute(
            "SELECT * FROM teaching_extraction_results WHERE tenant_id = ? AND course_id = ? "
            "AND task_run_id = ?", (identity.tenant_id, course_id, task_run_id),
        ).fetchone()
        if row is None:
            raise KeyError("teaching extraction result is not in this course")
        stored = json.loads(row["data_json"])
        if hashlib.sha256(self.store._json(stored).encode()).hexdigest() != row["output_hash"]:
            raise SourceChanged("teaching extraction result integrity check failed")
        source = self.teaching.read_source(identity, course_id, row["source_id"])
        if any(source[key] != stored["source"][key] for key in stored["source"]):
            raise SourceChanged("teaching extraction source changed")
        items = []
        for item in stored["items"]:
            text = source["blocks"][item["block_index"] - 1]["text"]
            quote = text[item["start_offset"]:item["end_offset"]]
            if hashlib.sha256(quote.encode()).hexdigest() != item["quote_hash"]:
                raise SourceChanged("teaching extraction citation changed")
            items.append({key: value for key, value in item.items() if key != "quote_hash"} | {"quote": quote})
        return {"task_run_id": task_run_id, "status": self.cp._task(task_run_id).status.value,
                "source": stored["source"], "items": items}

    def reconcile_stale(self, identity: IdentityContext, course_id: str,
                        task_run_id: str, *, grace_seconds: int = 300) -> dict[str, Any]:
        """Move an expired incomplete attempt to review or manual reconciliation."""
        if grace_seconds < 0:
            raise ValueError("reconciliation grace must be non-negative")
        row = self.store.connection.execute(
            "SELECT * FROM teaching_extraction_attempts WHERE tenant_id = ? AND course_id = ? "
            "AND task_run_id = ?", (identity.tenant_id, course_id, task_run_id),
        ).fetchone()
        if row is None or row["requested_by"] != identity.actor_id:
            raise KeyError("teaching extraction attempt is not available to this member")
        self.teaching.read_source(identity, course_id, row["source_id"])
        task = self.cp._task(task_run_id)
        self._authorized_agent(identity, task.agent_id, task.agent_version)
        started = datetime.fromisoformat(row["started_at"].replace(" ", "T")).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - started < timedelta(seconds=grace_seconds):
            return self._result_summary(task_run_id, task.status.value)
        result = self.store.connection.execute(
            "SELECT data_json FROM teaching_extraction_results WHERE task_run_id = ?",
            (task_run_id,),
        ).fetchone()
        if task.status in {TaskRunStatus.QUEUED, TaskRunStatus.PLANNING, TaskRunStatus.RUNNING}:
            if task.status == TaskRunStatus.QUEUED:
                self.cp.transition_task(task_run_id, TaskRunStatus.PLANNING)
            if task.status in {TaskRunStatus.QUEUED, TaskRunStatus.PLANNING}:
                self.cp.transition_task(task_run_id, TaskRunStatus.RUNNING)
            self.cp.transition_task(
                task_run_id, TaskRunStatus.WAITING_REVIEW if result else TaskRunStatus.RECONCILING,
                None if result else "TEACHING_RUNTIME_INTERRUPTED",
            )
        return self._result_summary(task_run_id, self.cp._task(task_run_id).status.value,
                                    item_count=len(json.loads(result["data_json"])["items"]) if result else 0)


__all__ = ["TeachingSourceExecutionService"]
