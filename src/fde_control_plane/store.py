"""Small durable SQLite store for the M1 control-plane kernel.

The store deliberately keeps persistence boring: JSON columns preserve the
domain objects while unique keys provide the idempotency fences needed by the
service layer. No runtime credential is accepted by this module as a special
field; callers pass already-sanitized payloads.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any, Iterable


class SQLiteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._owner_decision_lock = RLock()
        self._session_lock = RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit keeps read-only lookups from holding a snapshot. Atomic
        # state transitions below explicitly use BEGIN IMMEDIATE.
        self.connection = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS actors (
                actor_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                actor_type TEXT NOT NULL, capabilities_json TEXT NOT NULL,
                roles_json TEXT NOT NULL, external_ref_hash TEXT,
                active INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS member_access_requests (
                request_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                subject_ref_hash TEXT NOT NULL,
                status TEXT NOT NULL, actor_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT,
                UNIQUE(tenant_id, subject_ref_hash)
            );
            CREATE TABLE IF NOT EXISTS member_profiles (
                actor_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                display_name TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS member_default_grants (
                actor_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_member_access_requests_pending
                ON member_access_requests(tenant_id, status, created_at);
            CREATE TABLE IF NOT EXISTS member_status_changes (
                audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, active INTEGER NOT NULL,
                decided_by TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS member_reviewer_role_changes (
                audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, reviewer INTEGER NOT NULL,
                decided_by TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS agent_versions (
                agent_id TEXT NOT NULL, version INTEGER NOT NULL,
                capabilities_json TEXT NOT NULL, active INTEGER NOT NULL,
                skill_version TEXT, PRIMARY KEY (agent_id, version)
            );
            CREATE TABLE IF NOT EXISTS agent_version_snapshots (
                agent_id TEXT NOT NULL, version INTEGER NOT NULL, tenant_id TEXT NOT NULL,
                draft_id TEXT NOT NULL, config_json TEXT NOT NULL, config_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (agent_id, version)
            );
            CREATE TABLE IF NOT EXISTS agent_evaluations (
                evaluation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, draft_id TEXT NOT NULL,
                input_hash TEXT NOT NULL, evaluator_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL, cases_json TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_agent_evaluations_draft
                ON agent_evaluations(tenant_id, draft_id, created_at);
            CREATE TABLE IF NOT EXISTS agent_eval_datasets (
                tenant_id TEXT NOT NULL, dataset_id TEXT NOT NULL, created_by TEXT NOT NULL,
                cases_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, dataset_id)
            );
            CREATE TABLE IF NOT EXISTS agent_routes (
                agent_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                stable_version INTEGER NOT NULL, canary_version INTEGER,
                volunteer_ids_json TEXT NOT NULL, status TEXT NOT NULL,
                canary_epoch INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS agent_volunteer_consents (
                tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                member_id TEXT NOT NULL, candidate_version INTEGER NOT NULL,
                active INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, agent_id, member_id)
            );
            CREATE TABLE IF NOT EXISTS agent_canary_reviews (
                task_run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                agent_version INTEGER NOT NULL, reviewer_id TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('PASS', 'FAIL')),
                evidence_ref TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_agent_canary_reviews_agent
                ON agent_canary_reviews(tenant_id, agent_id, agent_version);
            CREATE TABLE IF NOT EXISTS agent_promotion_evidence (
                audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                stable_version INTEGER NOT NULL, candidate_version INTEGER NOT NULL,
                canary_epoch INTEGER NOT NULL, approver_id TEXT NOT NULL,
                observation_seconds INTEGER NOT NULL, evidence_json TEXT NOT NULL,
                evidence_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_agent_promotion_evidence_agent
                ON agent_promotion_evidence(tenant_id, agent_id, created_at);
            CREATE TRIGGER IF NOT EXISTS agent_version_config_immutable
            BEFORE UPDATE ON agent_versions
            WHEN NEW.agent_id IS NOT OLD.agent_id OR NEW.version IS NOT OLD.version
              OR NEW.capabilities_json IS NOT OLD.capabilities_json
              OR NEW.skill_version IS NOT OLD.skill_version
            BEGIN SELECT RAISE(ABORT, 'immutable agent version config'); END;
            CREATE TRIGGER IF NOT EXISTS agent_version_no_delete
            BEFORE DELETE ON agent_versions
            BEGIN SELECT RAISE(ABORT, 'immutable agent version'); END;
            CREATE TRIGGER IF NOT EXISTS agent_snapshot_no_update
            BEFORE UPDATE ON agent_version_snapshots
            BEGIN SELECT RAISE(ABORT, 'immutable agent snapshot'); END;
            CREATE TRIGGER IF NOT EXISTS agent_snapshot_no_delete
            BEFORE DELETE ON agent_version_snapshots
            BEGIN SELECT RAISE(ABORT, 'immutable agent snapshot'); END;
            CREATE TRIGGER IF NOT EXISTS agent_eval_dataset_no_update
            BEFORE UPDATE ON agent_eval_datasets
            BEGIN SELECT RAISE(ABORT, 'immutable evaluation dataset'); END;
            CREATE TRIGGER IF NOT EXISTS agent_eval_dataset_no_delete
            BEFORE DELETE ON agent_eval_datasets
            BEGIN SELECT RAISE(ABORT, 'immutable evaluation dataset'); END;
            CREATE TRIGGER IF NOT EXISTS agent_evaluation_no_update
            BEFORE UPDATE ON agent_evaluations
            BEGIN SELECT RAISE(ABORT, 'immutable evaluation'); END;
            CREATE TRIGGER IF NOT EXISTS agent_evaluation_no_delete
            BEFORE DELETE ON agent_evaluations
            BEGIN SELECT RAISE(ABORT, 'immutable evaluation'); END;
            CREATE TRIGGER IF NOT EXISTS agent_canary_review_no_update
            BEFORE UPDATE ON agent_canary_reviews
            BEGIN SELECT RAISE(ABORT, 'immutable canary review'); END;
            CREATE TRIGGER IF NOT EXISTS agent_canary_review_no_delete
            BEFORE DELETE ON agent_canary_reviews
            BEGIN SELECT RAISE(ABORT, 'immutable canary review'); END;
            CREATE TRIGGER IF NOT EXISTS agent_promotion_evidence_no_update
            BEFORE UPDATE ON agent_promotion_evidence
            BEGIN SELECT RAISE(ABORT, 'immutable promotion evidence'); END;
            CREATE TRIGGER IF NOT EXISTS agent_promotion_evidence_no_delete
            BEFORE DELETE ON agent_promotion_evidence
            BEGIN SELECT RAISE(ABORT, 'immutable promotion evidence'); END;
            CREATE TABLE IF NOT EXISTS agent_pauses (
                audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                expected_version INTEGER, decided_by TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS agent_resumes (
                audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                expected_version INTEGER NOT NULL, decided_by TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS agent_config_drafts (
                draft_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                created_by TEXT NOT NULL, status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_agent_config_drafts_tenant
                ON agent_config_drafts(tenant_id, created_at);
            CREATE TABLE IF NOT EXISTS agent_config_reviews (
                review_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL, reviewer_id TEXT NOT NULL,
                input_hash TEXT NOT NULL, status TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_agent_config_reviews_draft
                ON agent_config_reviews(tenant_id, draft_id, created_at);
            CREATE TABLE IF NOT EXISTS agent_config_approvals (
                approval_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL, approver_id TEXT NOT NULL,
                review_id TEXT NOT NULL, input_hash TEXT NOT NULL,
                agent_id TEXT NOT NULL, version INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, draft_id, input_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_agent_config_approvals_draft
                ON agent_config_approvals(tenant_id, draft_id, created_at);
            CREATE TABLE IF NOT EXISTS assistant_bindings (
                binding_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                member_actor_id TEXT NOT NULL, assistant_actor_id TEXT NOT NULL,
                is_default INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state_hash TEXT PRIMARY KEY, session_ref_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_sessions (
                session_hash TEXT PRIMARY KEY, data_json TEXT NOT NULL,
                expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS user_oauth_tokens (
                actor_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                subject_ref_hash TEXT NOT NULL, scopes_json TEXT NOT NULL,
                token_ciphertext TEXT NOT NULL, expires_at INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS task_runs (
                task_run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_steps (
                step_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delegations (
                delegation_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dispatches (
                dispatch_key TEXT PRIMARY KEY, task_run_id TEXT NOT NULL UNIQUE,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_events (
                event_key TEXT PRIMARY KEY, task_run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, data_json TEXT NOT NULL,
                UNIQUE(task_run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS wake_events (
                wake_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                task_run_id TEXT NOT NULL, trigger_type TEXT NOT NULL,
                source_revision TEXT NOT NULL, status TEXT NOT NULL,
                claim_token TEXT NOT NULL, data_json TEXT NOT NULL,
                result_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS follow_up_observations (
                observation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                task_run_id TEXT NOT NULL, data_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS task_due_corrections (
                correction_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                task_run_id TEXT NOT NULL, remote_task_id TEXT NOT NULL,
                status TEXT NOT NULL, data_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_due_correction
                ON task_due_corrections(tenant_id, remote_task_id)
                WHERE status IN ('PENDING', 'APPROVED', 'DISPATCHED', 'UNKNOWN');
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                notification_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                recipient_actor_id TEXT NOT NULL, channel TEXT NOT NULL,
                status TEXT NOT NULL, claim_token TEXT NOT NULL,
                data_json TEXT NOT NULL, result_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS meeting_records (
                record_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                document_id_hash TEXT NOT NULL, revision_id TEXT NOT NULL,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meeting_todo_drafts (
                draft_id TEXT PRIMARY KEY, record_id TEXT NOT NULL,
                todo_id TEXT NOT NULL, data_json TEXT NOT NULL,
                UNIQUE(record_id, todo_id)
            );
            CREATE TABLE IF NOT EXISTS meeting_sources (
                record_id TEXT PRIMARY KEY, data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL,
                action_version INTEGER NOT NULL, proposal_hash TEXT NOT NULL,
                status TEXT NOT NULL, data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approval_deliveries (
                approval_id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
                recipient_actor_id TEXT NOT NULL, proposal_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approval_delivery_attempts (
                approval_id TEXT PRIMARY KEY, recipient_actor_id TEXT NOT NULL,
                proposal_hash TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS assistant_card_deliveries (
                record_id TEXT PRIMARY KEY, recipient_actor_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                message_id TEXT, review_snapshot TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS assistant_message_inbox (
                message_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                status TEXT NOT NULL, data_json TEXT NOT NULL,
                claim_token TEXT, lease_until REAL, result_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_assistant_message_inbox_status
                ON assistant_message_inbox(tenant_id, status, created_at);
            CREATE TABLE IF NOT EXISTS meeting_card_review_inbox (
                message_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                status TEXT NOT NULL, data_json TEXT NOT NULL,
                claim_token TEXT, lease_until REAL, result_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_meeting_card_review_inbox_status
                ON meeting_card_review_inbox(tenant_id, status, created_at);
            CREATE TABLE IF NOT EXISTS approval_actions (
                action_key TEXT PRIMARY KEY, approval_id TEXT NOT NULL,
                action_version INTEGER NOT NULL, decision TEXT NOT NULL,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS inbox_events (
                event_key TEXT PRIMARY KEY, result_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS outbox (
                outbox_id TEXT PRIMARY KEY, write_idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, data_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claim_token TEXT, lease_until REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS tool_calls (
                tool_call_id TEXT PRIMARY KEY, write_idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                audit_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
                tenant_id TEXT NOT NULL, task_run_id TEXT,
                data_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS audit_triage (
                audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACKNOWLEDGED', 'RESOLVED')),
                decided_by TEXT NOT NULL, resolution TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS teaching_courses (
                tenant_id TEXT NOT NULL, course_id TEXT NOT NULL,
                title TEXT NOT NULL, term TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, course_id)
            );
            CREATE TABLE IF NOT EXISTS teaching_course_members (
                tenant_id TEXT NOT NULL, course_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('LEAD', 'TEACHER')),
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, course_id, actor_id),
                FOREIGN KEY (tenant_id, course_id) REFERENCES teaching_courses(tenant_id, course_id)
            );
            CREATE INDEX IF NOT EXISTS idx_teaching_members_actor
                ON teaching_course_members(tenant_id, actor_id, active);
            CREATE TABLE IF NOT EXISTS teaching_sources (
                source_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, course_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('SYLLABUS', 'WEEKLY_PLAN', 'LESSON_PLAN', 'MATERIAL')),
                document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                content_hash TEXT NOT NULL, block_count INTEGER NOT NULL,
                submitted_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tenant_id, course_id) REFERENCES teaching_courses(tenant_id, course_id)
            );
            CREATE INDEX IF NOT EXISTS idx_teaching_sources_course
                ON teaching_sources(tenant_id, course_id, created_at);
            CREATE TRIGGER IF NOT EXISTS teaching_sources_no_update
            BEFORE UPDATE ON teaching_sources BEGIN SELECT RAISE(ABORT, 'immutable teaching source'); END;
            CREATE TRIGGER IF NOT EXISTS teaching_sources_no_delete
            BEFORE DELETE ON teaching_sources BEGIN SELECT RAISE(ABORT, 'immutable teaching source'); END;
            CREATE TABLE IF NOT EXISTS teaching_source_readers (
                tenant_id TEXT NOT NULL, course_id TEXT NOT NULL,
                source_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                granted_by TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, course_id, source_id, actor_id),
                FOREIGN KEY (source_id) REFERENCES teaching_sources(source_id),
                FOREIGN KEY (tenant_id, course_id, actor_id)
                    REFERENCES teaching_course_members(tenant_id, course_id, actor_id)
            );
            CREATE INDEX IF NOT EXISTS idx_teaching_source_readers_actor
                ON teaching_source_readers(tenant_id, course_id, actor_id, active);
            CREATE TABLE IF NOT EXISTS teaching_outlines (
                outline_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, course_id TEXT NOT NULL,
                source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
                source_hash TEXT NOT NULL, outline_hash TEXT NOT NULL,
                data_json TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_id) REFERENCES teaching_sources(source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_teaching_outlines_source
                ON teaching_outlines(tenant_id, course_id, source_id, created_at);
            CREATE TRIGGER IF NOT EXISTS teaching_outlines_no_update
            BEFORE UPDATE ON teaching_outlines BEGIN SELECT RAISE(ABORT, 'immutable teaching outline'); END;
            CREATE TRIGGER IF NOT EXISTS teaching_outlines_no_delete
            BEFORE DELETE ON teaching_outlines BEGIN SELECT RAISE(ABORT, 'immutable teaching outline'); END;
            CREATE INDEX IF NOT EXISTS idx_audit_triage_tenant
                ON audit_triage(tenant_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_run_id);
            CREATE INDEX IF NOT EXISTS idx_tool_calls_task ON tool_calls(json_extract(data_json, '$.task_run_id'));
            """
        )
        # The project already has deployed SQLite files. Add worker columns
        # idempotently instead of requiring a destructive database rebuild.
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(outbox)").fetchall()}
        migrations = {
            "claim_token": "ALTER TABLE outbox ADD COLUMN claim_token TEXT",
            "lease_until": "ALTER TABLE outbox ADD COLUMN lease_until REAL",
            "attempt_count": "ALTER TABLE outbox ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
            "last_error": "ALTER TABLE outbox ADD COLUMN last_error TEXT",
        }
        for name, statement in migrations.items():
            if name not in columns:
                self.connection.execute(statement)
        card_columns = {row["name"] for row in self.connection.execute(
            "PRAGMA table_info(assistant_card_deliveries)").fetchall()}
        if "review_snapshot" not in card_columns:
            self.connection.execute("ALTER TABLE assistant_card_deliveries ADD COLUMN review_snapshot TEXT")
        route_columns = {row["name"] for row in self.connection.execute(
            "PRAGMA table_info(agent_routes)").fetchall()}
        if "canary_epoch" not in route_columns:
            self.connection.execute("ALTER TABLE agent_routes ADD COLUMN canary_epoch INTEGER NOT NULL DEFAULT 0")
        self.connection.commit()

    @staticmethod
    def _json(value: Any) -> str:
        def normalize(item: Any) -> Any:
            if isinstance(item, dict):
                return {str(k): normalize(v) for k, v in item.items()}
            if isinstance(item, (set, frozenset, tuple)):
                return [normalize(v) for v in item]
            if hasattr(item, "value"):
                return item.value
            return item
        return json.dumps(normalize(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else json.loads(row["data_json"])

    def save_actor(self, actor: Any) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO actors VALUES (?, ?, ?, ?, ?, ?, ?)",
            (actor.actor_id, actor.tenant_id, str(actor.actor_type), self._json(sorted(actor.capabilities)),
             self._json(sorted(actor.roles)), actor.external_ref_hash, int(actor.active)),
        )
        self.connection.commit()

    def get_actor(self, actor_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM actors WHERE actor_id = ?", (actor_id,)).fetchone()

    def find_actor_by_external_hash(self, tenant_id: str, external_ref_hash: str) -> sqlite3.Row | None:
        rows = self.connection.execute(
            "SELECT * FROM actors WHERE tenant_id = ? AND external_ref_hash = ? LIMIT 2",
            (tenant_id, external_ref_hash),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def ensure_oauth_member(self, tenant_id: str, subject_ref_hash: str, actor_id: str,
                            display_name: str | None, event: Any,
                            default_assistant_actor_id: str | None = None,
                            default_member_capabilities: frozenset[str] = frozenset()) -> sqlite3.Row:
        """Provision a plain member and default assistant in one transaction."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM actors WHERE tenant_id = ? AND external_ref_hash = ?",
                (tenant_id, subject_ref_hash),
            ).fetchall()
            if len(existing) > 1:
                raise PermissionError("OAuth identity has multiple member bindings")
            if existing:
                if existing[0]["actor_type"] != "USER" or not existing[0]["active"]:
                    raise PermissionError("OAuth identity is not an active member")
                if default_assistant_actor_id:
                    self._ensure_default_assistant_binding(existing[0]["actor_id"], tenant_id, default_assistant_actor_id)
                if default_member_capabilities:
                    self._grant_auto_member_defaults(existing[0], tenant_id, default_member_capabilities, event)
                if display_name:
                    self._upsert_member_profile(existing[0]["actor_id"], tenant_id, display_name)
                self.connection.commit()
                return existing[0]
            pending = self.connection.execute(
                "SELECT * FROM member_access_requests WHERE tenant_id = ? AND subject_ref_hash = ?",
                (tenant_id, subject_ref_hash),
            ).fetchone()
            if pending is not None and pending["status"] != "PENDING":
                raise PermissionError("OAuth member application has an incompatible status")
            self.connection.execute(
                "INSERT INTO actors(actor_id, tenant_id, actor_type, capabilities_json, roles_json, external_ref_hash, active) "
                "VALUES (?, ?, 'USER', ?, '[]', ?, 1)",
                (actor_id, tenant_id, self._json(sorted(default_member_capabilities)), subject_ref_hash),
            )
            if default_member_capabilities:
                self.connection.execute(
                    "INSERT INTO member_default_grants(actor_id, tenant_id, capabilities_json) VALUES (?, ?, ?)",
                    (actor_id, tenant_id, self._json(sorted(default_member_capabilities))),
                )
            if default_assistant_actor_id:
                self._ensure_default_assistant_binding(actor_id, tenant_id, default_assistant_actor_id)
            if display_name:
                self._upsert_member_profile(actor_id, tenant_id, display_name)
            if pending is not None:
                self.connection.execute(
                    "UPDATE member_access_requests SET status = 'APPROVED', actor_id = ?, "
                    "decided_at = CURRENT_TIMESTAMP WHERE request_id = ? AND status = 'PENDING'",
                    (actor_id, pending["request_id"]),
                )
            else:
                self.connection.execute(
                    "INSERT INTO member_access_requests(request_id, tenant_id, subject_ref_hash, status, actor_id, decided_at) "
                    "VALUES (?, ?, ?, 'APPROVED', ?, CURRENT_TIMESTAMP)",
                    ("member-join-" + actor_id, tenant_id, subject_ref_hash, actor_id),
                )
            self.connection.execute(
                "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (event.audit_id, event.event_type, event.tenant_id, event.task_run_id,
                 self._json(asdict(event))),
            )
            self.connection.commit()
            return self.get_actor(actor_id)
        except Exception:
            self.connection.rollback()
            raise

    def _grant_auto_member_defaults(self, member: sqlite3.Row, tenant_id: str,
                                    capabilities: frozenset[str], event: Any) -> None:
        member_id = member["actor_id"]
        prior_join = self.connection.execute(
            "SELECT 1 FROM member_access_requests WHERE actor_id = ? AND tenant_id = ? "
            "AND request_id = ? AND status = 'APPROVED'",
            (member_id, tenant_id, "member-join-" + member_id),
        ).fetchone()
        already_granted = self.connection.execute(
            "SELECT 1 FROM member_default_grants WHERE actor_id = ? AND tenant_id = ?",
            (member_id, tenant_id),
        ).fetchone()
        if prior_join is None or already_granted is not None:
            return
        if json.loads(member["capabilities_json"]) == []:
            self.connection.execute(
                "UPDATE actors SET capabilities_json = ? WHERE actor_id = ? AND tenant_id = ?",
                (self._json(sorted(capabilities)), member_id, tenant_id),
            )
            grant_event = {**asdict(event), "event_type": "MEMBER_DEFAULT_CAPABILITIES_GRANTED",
                           "actor_id": member_id}
            self.connection.execute(
                "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
                "VALUES (?, ?, ?, NULL, ?)",
                (event.audit_id, grant_event["event_type"], tenant_id, self._json(grant_event)),
            )
        self.connection.execute(
            "INSERT INTO member_default_grants(actor_id, tenant_id, capabilities_json) VALUES (?, ?, ?)",
            (member_id, tenant_id, self._json(sorted(capabilities))),
        )

    def _ensure_default_assistant_binding(self, member_actor_id: str, tenant_id: str,
                                          assistant_actor_id: str) -> None:
        assistant = self.connection.execute(
            "SELECT actor_type, active FROM actors WHERE actor_id = ? AND tenant_id = ?",
            (assistant_actor_id, tenant_id),
        ).fetchone()
        if assistant is None or assistant["actor_type"] != "PERSONAL_ASSISTANT" or not assistant["active"]:
            raise PermissionError("configured default assistant is unavailable")
        binding = self.connection.execute(
            "SELECT assistant_actor_id FROM assistant_bindings "
            "WHERE tenant_id = ? AND member_actor_id = ? AND is_default = 1",
            (tenant_id, member_actor_id),
        ).fetchall()
        if len(binding) > 1:
            raise PermissionError("member has multiple default assistants")
        if binding:
            return
        self.connection.execute(
            "INSERT INTO assistant_bindings(binding_id, tenant_id, member_actor_id, assistant_actor_id, is_default) "
            "VALUES (?, ?, ?, ?, 1)",
            ("binding-" + member_actor_id, tenant_id, member_actor_id, assistant_actor_id),
        )

    def ensure_default_assistant_members(self, tenant_id: str, assistant_actor_id: str,
                                         capabilities: frozenset[str]) -> dict[str, int]:
        """Idempotently bring existing active members onto the tenant default assistant."""
        self.connection.execute("BEGIN IMMEDIATE")
        bound = granted = 0
        try:
            assistant = self.connection.execute(
                "SELECT actor_type, active FROM actors WHERE actor_id = ? AND tenant_id = ?",
                (assistant_actor_id, tenant_id),
            ).fetchone()
            if assistant is None or assistant["actor_type"] != "PERSONAL_ASSISTANT" or not assistant["active"]:
                raise PermissionError("configured default assistant is unavailable")
            members = self.connection.execute(
                "SELECT * FROM actors WHERE tenant_id = ? AND actor_type = 'USER' AND active = 1",
                (tenant_id,),
            ).fetchall()
            for member in members:
                member_id = member["actor_id"]
                binding = self.connection.execute(
                    "SELECT 1 FROM assistant_bindings WHERE tenant_id = ? AND member_actor_id = ? AND is_default = 1",
                    (tenant_id, member_id),
                ).fetchone()
                if binding is None:
                    self._ensure_default_assistant_binding(member_id, tenant_id, assistant_actor_id)
                    self._record_system_audit(
                        "MEMBER_DEFAULT_ASSISTANT_BOUND", tenant_id, member_id,
                        {"assistant_actor_id": assistant_actor_id},
                    )
                    bound += 1
                if not capabilities:
                    continue
                grant = self.connection.execute(
                    "SELECT 1 FROM member_default_grants WHERE actor_id = ? AND tenant_id = ?",
                    (member_id, tenant_id),
                ).fetchone()
                auto_joined = self.connection.execute(
                    "SELECT 1 FROM member_access_requests WHERE actor_id = ? AND tenant_id = ? "
                    "AND request_id = ? AND status = 'APPROVED'",
                    (member_id, tenant_id, "member-join-" + member_id),
                ).fetchone()
                current = frozenset(json.loads(member["capabilities_json"]))
                if auto_joined is not None and grant is None and not current:
                    self.connection.execute(
                        "UPDATE actors SET capabilities_json = ? WHERE actor_id = ? AND tenant_id = ?",
                        (self._json(sorted(capabilities)), member_id, tenant_id),
                    )
                    self.connection.execute(
                        "INSERT INTO member_default_grants(actor_id, tenant_id, capabilities_json) VALUES (?, ?, ?)",
                        (member_id, tenant_id, self._json(sorted(capabilities))),
                    )
                    self._record_system_audit(
                        "MEMBER_DEFAULT_CAPABILITIES_GRANTED", tenant_id, member_id,
                        {"capabilities": sorted(capabilities)},
                    )
                    granted += 1
            self.connection.commit()
            return {"bound": bound, "granted": granted}
        except Exception:
            self.connection.rollback()
            raise

    def _record_system_audit(self, event_type: str, tenant_id: str, actor_id: str,
                             payload: dict[str, Any]) -> None:
        audit_id = f"system-{event_type.lower()}-{actor_id}"
        data = {"audit_id": audit_id, "event_type": event_type, "tenant_id": tenant_id,
                "actor_id": actor_id, "task_run_id": None, "correlation_id": None,
                "payload_hash": hashlib.sha256(self._json(payload).encode()).hexdigest(),
                "outcome": "APPLIED", **payload}
        self.connection.execute(
            "INSERT OR IGNORE INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
            "VALUES (?, ?, ?, NULL, ?)",
            (audit_id, event_type, tenant_id, self._json(data)),
        )

    def _upsert_member_profile(self, actor_id: str, tenant_id: str, display_name: str) -> None:
        self.connection.execute(
            "INSERT INTO member_profiles(actor_id, tenant_id, display_name) VALUES (?, ?, ?) "
            "ON CONFLICT(actor_id) DO UPDATE SET display_name = excluded.display_name, "
            "updated_at = CURRENT_TIMESTAMP",
            (actor_id, tenant_id, display_name),
        )

    def request_member_access(self, tenant_id: str, subject_ref_hash: str, request_id: str) -> sqlite3.Row:
        """Record only a verified same-tenant OAuth subject hash, never its token or open_id."""
        self.connection.execute(
            "INSERT OR IGNORE INTO member_access_requests(request_id, tenant_id, subject_ref_hash, status) "
            "VALUES (?, ?, ?, 'PENDING')", (request_id, tenant_id, subject_ref_hash),
        )
        return self.connection.execute(
            "SELECT * FROM member_access_requests WHERE tenant_id = ? AND subject_ref_hash = ?",
            (tenant_id, subject_ref_hash),
        ).fetchone()

    def list_pending_member_access(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT request_id, created_at FROM member_access_requests "
            "WHERE tenant_id = ? AND status = 'PENDING' ORDER BY created_at, request_id",
            (tenant_id,),
        ).fetchall()

    def approve_member_access(self, tenant_id: str, request_id: str, actor_id: str) -> dict[str, Any]:
        """Bind one OAuth subject as a plain member; approval is atomic and replay safe."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            request = self.connection.execute(
                "SELECT * FROM member_access_requests WHERE tenant_id = ? AND request_id = ?",
                (tenant_id, request_id),
            ).fetchone()
            if request is None:
                raise ValueError("REQUEST_NOT_FOUND")
            if request["status"] == "APPROVED":
                self.connection.commit()
                return {"request_id": request_id, "actor_id": request["actor_id"], "duplicate": True}
            if request["status"] != "PENDING":
                raise ValueError("REQUEST_NOT_PENDING")
            if self.connection.execute(
                "SELECT 1 FROM actors WHERE tenant_id = ? AND external_ref_hash = ?",
                (tenant_id, request["subject_ref_hash"]),
            ).fetchone() is not None:
                raise ValueError("MEMBER_ALREADY_BOUND")
            self.connection.execute(
                "INSERT INTO actors(actor_id, tenant_id, actor_type, capabilities_json, roles_json, external_ref_hash, active) "
                "VALUES (?, ?, 'USER', '[]', '[]', ?, 1)",
                (actor_id, tenant_id, request["subject_ref_hash"]),
            )
            self.connection.execute(
                "UPDATE member_access_requests SET status = 'APPROVED', actor_id = ?, "
                "decided_at = CURRENT_TIMESTAMP WHERE tenant_id = ? AND request_id = ?",
                (actor_id, tenant_id, request_id),
            )
            self.connection.commit()
            return {"request_id": request_id, "actor_id": actor_id, "duplicate": False}
        except Exception:
            self.connection.rollback()
            raise

    def save_oauth_state(self, state_hash: str, session_ref_hash: str, expires_at: int, now: int) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,))
            self.connection.execute(
                "INSERT INTO oauth_states(state_hash, session_ref_hash, expires_at) VALUES (?, ?, ?)",
                (state_hash, session_ref_hash, expires_at),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def consume_oauth_state(self, state_hash: str, session_ref_hash: str, now: int) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT session_ref_hash, expires_at FROM oauth_states WHERE state_hash = ?", (state_hash,)
            ).fetchone()
            valid = row is not None and row["expires_at"] >= now and row["session_ref_hash"] == session_ref_hash
            if valid or (row is not None and row["expires_at"] < now):
                self.connection.execute("DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return valid

    def save_web_session(self, session_hash: str, data: Any, expires_at: int, now: int) -> None:
        with self._session_lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute("DELETE FROM web_sessions WHERE expires_at <= ? OR revoked = 1", (now,))
                self.connection.execute(
                    "INSERT INTO web_sessions(session_hash, data_json, expires_at, revoked) VALUES (?, ?, ?, 0)",
                    (session_hash, self._json(data), expires_at),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def get_web_session(self, session_hash: str, now: int) -> dict[str, Any] | None:
        with self._session_lock:
            row = self.connection.execute(
                "SELECT data_json, expires_at, revoked FROM web_sessions WHERE session_hash = ?",
                (session_hash,),
            ).fetchone()
            if row is None or row["revoked"] or row["expires_at"] <= now:
                if row is not None and row["expires_at"] <= now:
                    self.connection.execute("DELETE FROM web_sessions WHERE session_hash = ?", (session_hash,))
                return None
            return json.loads(row["data_json"])

    def revoke_web_session(self, session_hash: str) -> None:
        with self._session_lock:
            self.connection.execute(
                "UPDATE web_sessions SET revoked = 1 WHERE session_hash = ?", (session_hash,)
            )
        self.connection.commit()

    def save_agent_version(self, version: Any) -> None:
        existing = self.get_agent_version(version.agent_id, version.version)
        if existing is not None:
            if (json.loads(existing["capabilities_json"]) != sorted(version.capabilities)
                    or existing["skill_version"] != version.skill_version):
                raise ValueError("agent version snapshot is immutable")
            self.connection.execute(
                "UPDATE agent_versions SET active = ? WHERE agent_id = ? AND version = ?",
                (int(version.active), version.agent_id, version.version),
            )
            self.connection.commit()
            return
        self.connection.execute(
            "INSERT INTO agent_versions VALUES (?, ?, ?, ?, ?)",
            (version.agent_id, version.version, self._json(sorted(version.capabilities)), int(version.active), version.skill_version),
        )
        self.connection.commit()

    def deactivate_other_agent_versions(self, agent_id: str, except_version: int) -> None:
        self.connection.execute(
            "UPDATE agent_versions SET active = 0 WHERE agent_id = ? AND version <> ?",
            (agent_id, except_version),
        )
        self.connection.commit()

    def get_agent_version(self, agent_id: str, version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM agent_versions WHERE agent_id = ? AND version = ?", (agent_id, version)
        ).fetchone()

    def list_agent_actors(self, tenant_id: str) -> list[sqlite3.Row]:
        """List non-user actors for the tenant without exposing external identities."""
        return self.connection.execute(
            "SELECT actor_id, tenant_id, actor_type, capabilities_json, roles_json, active "
            "FROM actors WHERE tenant_id = ? AND actor_type IN "
            "('PERSONAL_ASSISTANT', 'BUSINESS_AGENT', 'MANAGEMENT_AGENT') "
            "ORDER BY actor_type, actor_id",
            (tenant_id,),
        ).fetchall()

    def pause_agent(self, tenant_id: str, agent_id: str, expected_version: int | None,
                    reason: str, event: Any) -> bool:
        """Pause only the version the administrator reviewed, with its audit record."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            updated = self.connection.execute(
                "UPDATE actors SET active = 0 WHERE tenant_id = ? AND actor_id = ? "
                "AND actor_type IN ('PERSONAL_ASSISTANT', 'BUSINESS_AGENT', 'MANAGEMENT_AGENT') "
                "AND active = 1 AND ((? IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM agent_versions WHERE agent_id = ? AND active = 1)) "
                "OR EXISTS (SELECT 1 FROM agent_versions WHERE agent_id = ? AND version = ? AND active = 1))",
                (tenant_id, agent_id, expected_version, agent_id, agent_id, expected_version),
            )
            if updated.rowcount != 1:
                self.connection.rollback()
                return False
            self.connection.execute(
                "INSERT INTO agent_pauses(audit_id, tenant_id, agent_id, expected_version, decided_by, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event.audit_id, tenant_id, agent_id, expected_version, event.actor_id, reason),
            )
            self.connection.execute(
                "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (event.audit_id, event.event_type, event.tenant_id, event.task_run_id,
                 self._json(asdict(event))),
            )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def latest_agent_pauses(self, tenant_id: str) -> dict[str, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT agent_id, reason, created_at FROM agent_pauses WHERE tenant_id = ? "
            "ORDER BY rowid DESC", (tenant_id,),
        ).fetchall()
        return {row["agent_id"]: row for row in reversed(rows)}

    def resume_agent(self, tenant_id: str, agent_id: str, expected_version: int,
                     reason: str, event: Any) -> bool:
        """Resume a paused agent only at the version the administrator reviewed."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            updated = self.connection.execute(
                "UPDATE actors SET active = 1 WHERE tenant_id = ? AND actor_id = ? "
                "AND actor_type IN ('PERSONAL_ASSISTANT', 'BUSINESS_AGENT', 'MANAGEMENT_AGENT') "
                "AND active = 0 AND EXISTS (SELECT 1 FROM agent_versions "
                "WHERE agent_id = ? AND version = ? AND active = 1)",
                (tenant_id, agent_id, agent_id, expected_version),
            )
            if updated.rowcount != 1:
                self.connection.rollback()
                return False
            self.connection.execute(
                "INSERT INTO agent_resumes(audit_id, tenant_id, agent_id, expected_version, decided_by, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event.audit_id, tenant_id, agent_id, expected_version, event.actor_id, reason),
            )
            self.connection.execute(
                "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (event.audit_id, event.event_type, event.tenant_id, event.task_run_id,
                 self._json(asdict(event))),
            )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def list_agent_versions(self, agent_ids: Iterable[str]) -> list[sqlite3.Row]:
        ids = list(agent_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return self.connection.execute(
            f"SELECT agent_id, version, capabilities_json, active, skill_version "
            f"FROM agent_versions WHERE agent_id IN ({placeholders}) ORDER BY agent_id, version DESC",
            ids,
        ).fetchall()

    def get_agent_route(self, tenant_id: str, agent_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM agent_routes WHERE tenant_id = ? AND agent_id = ?",
            (tenant_id, agent_id),
        ).fetchone()

    def agent_volunteer_consent(self, tenant_id: str, agent_id: str, member_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM agent_volunteer_consents WHERE tenant_id = ? AND agent_id = ? AND member_id = ?",
            (tenant_id, agent_id, member_id),
        ).fetchone()

    def set_agent_volunteer_consent(self, tenant_id: str, agent_id: str, member_id: str,
                                    candidate_version: int, enroll: bool, event: Any) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            route = self.get_agent_route(tenant_id, agent_id)
            member = self.connection.execute(
                "SELECT 1 FROM actors WHERE actor_id = ? AND tenant_id = ? "
                "AND actor_type = 'USER' AND active = 1", (member_id, tenant_id),
            ).fetchone()
            snapshot = self.get_agent_snapshot(tenant_id, agent_id, candidate_version)
            current = self.agent_volunteer_consent(tenant_id, agent_id, member_id)
            if (route is None or route["status"] == "ARCHIVED" or member is None or snapshot is None
                    or candidate_version <= route["stable_version"]
                    or (route["status"] == "CANARY" and route["canary_version"] != candidate_version)
                    or (not enroll and (current is None or not current["active"]
                                        or current["candidate_version"] != candidate_version))
                    or (current is not None and bool(current["active"]) == enroll
                        and current["candidate_version"] == candidate_version)):
                self.connection.rollback()
                return False
            self.connection.execute(
                "INSERT INTO agent_volunteer_consents(tenant_id, agent_id, member_id, candidate_version, active) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(tenant_id, agent_id, member_id) DO UPDATE SET "
                "candidate_version = excluded.candidate_version, active = excluded.active, "
                "updated_at = CURRENT_TIMESTAMP",
                (tenant_id, agent_id, member_id, candidate_version, int(enroll)),
            )
            if not enroll and route["status"] == "CANARY":
                volunteers = [item for item in json.loads(route["volunteer_ids_json"]) if item != member_id]
                if volunteers:
                    self.connection.execute(
                        "UPDATE agent_routes SET volunteer_ids_json = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE agent_id = ?", (self._json(volunteers), agent_id),
                    )
                else:
                    self.connection.execute(
                        "UPDATE agent_routes SET canary_version = NULL, volunteer_ids_json = '[]', "
                        "status = 'ACTIVE', updated_at = CURRENT_TIMESTAMP WHERE agent_id = ?", (agent_id,),
                    )
                    self.connection.execute(
                        "UPDATE agent_versions SET active = 0 WHERE agent_id = ? AND version = ?",
                        (agent_id, candidate_version),
                    )
            self._insert_agent_audit(event)
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def select_agent_version(self, tenant_id: str, agent_id: str, member_id: str) -> int | None:
        route = self.get_agent_route(tenant_id, agent_id)
        if route is None:
            row = self.connection.execute(
                "SELECT version FROM agent_versions WHERE agent_id = ? AND active = 1 "
                "ORDER BY version DESC LIMIT 1", (agent_id,),
            ).fetchone()
            return int(row["version"]) if row else None
        if route["status"] == "ARCHIVED":
            return None
        if route["status"] == "CANARY" and member_id in json.loads(route["volunteer_ids_json"]):
            return int(route["canary_version"])
        return int(route["stable_version"])

    def get_agent_snapshot(self, tenant_id: str, agent_id: str, version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM agent_version_snapshots WHERE tenant_id = ? AND agent_id = ? AND version = ?",
            (tenant_id, agent_id, version),
        ).fetchone()

    def agent_canary_runs(self, tenant_id: str, agent_id: str) -> list[dict[str, Any]]:
        """Return only metadata for runs in the current canary window."""
        route = self.get_agent_route(tenant_id, agent_id)
        if route is None or route["status"] != "CANARY":
            return []
        rows = self.connection.execute(
            "SELECT data_json FROM task_runs WHERE tenant_id = ?", (tenant_id,),
        ).fetchall()
        runs = []
        for row in rows:
            run = json.loads(row["data_json"])
            if (run.get("agent_id") == agent_id and run.get("agent_version") == route["canary_version"]
                    and run.get("release_mode") == "CANARY"
                    and run.get("canary_epoch") == route["canary_epoch"]):
                runs.append(run)
        return runs

    def list_agent_canary_reviews(self, tenant_id: str, agent_id: str,
                                  task_run_ids: list[str]) -> dict[str, sqlite3.Row]:
        if not task_run_ids:
            return {}
        result: dict[str, sqlite3.Row] = {}
        for start in range(0, len(task_run_ids), 500):
            chunk = task_run_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT task_run_id, outcome, reviewer_id, evidence_ref, created_at "
                f"FROM agent_canary_reviews WHERE tenant_id = ? AND agent_id = ? "
                f"AND task_run_id IN ({placeholders})",
                (tenant_id, agent_id, *chunk),
            ).fetchall()
            result.update({row["task_run_id"]: row for row in rows})
        return result

    def save_agent_canary_review(self, *, tenant_id: str, agent_id: str, task_run_id: str,
                                 expected_version: int, reviewer_id: str, outcome: str,
                                 evidence_ref: str, event: Any) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            route = self.get_agent_route(tenant_id, agent_id)
            snapshot = self.get_agent_snapshot(tenant_id, agent_id, expected_version)
            run = self.get_task_run(task_run_id)
            draft = (self.get_agent_config_draft(snapshot["draft_id"], tenant_id)
                     if snapshot is not None else None)
            current_ids = {item["task_run_id"] for item in self.agent_canary_runs(tenant_id, agent_id)}
            if (route is None or route["status"] != "CANARY"
                    or route["canary_version"] != expected_version or draft is None
                    or draft["created_by"] == reviewer_id or run is None
                    or task_run_id not in current_ids or run.get("status") != "WAITING_REVIEW"
                    or self.connection.execute(
                        "SELECT 1 FROM agent_canary_reviews WHERE task_run_id = ?", (task_run_id,),
                    ).fetchone() is not None):
                self.connection.rollback()
                return False
            self.connection.execute(
                "INSERT INTO agent_canary_reviews(task_run_id, tenant_id, agent_id, agent_version, "
                "reviewer_id, outcome, evidence_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_run_id, tenant_id, agent_id, expected_version, reviewer_id, outcome, evidence_ref),
            )
            self._insert_agent_audit(event)
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def agent_promotion_readiness(self, tenant_id: str, agent_id: str,
                                  approver_id: str) -> dict[str, Any]:
        """Explain the same fail-closed evidence gate used by promotion."""
        route = self.get_agent_route(tenant_id, agent_id)
        if route is None or route["status"] != "CANARY":
            return {"can_promote": False, "blockers": ["NO_ACTIVE_CANARY"]}
        if route["canary_version"] is None:
            return {"can_promote": False, "blockers": ["CANARY_VERSION_MISSING"]}
        actor = self.connection.execute(
            "SELECT active FROM actors WHERE actor_id = ? AND tenant_id = ?", (agent_id, tenant_id),
        ).fetchone()
        observed = self.connection.execute(
            "SELECT (strftime('%s', 'now') - strftime('%s', updated_at)) AS seconds "
            "FROM agent_routes WHERE tenant_id = ? AND agent_id = ?", (tenant_id, agent_id),
        ).fetchone()
        seconds = max(0, int(observed["seconds"])) if observed is not None and observed["seconds"] is not None else 0
        runs = self.agent_canary_runs(tenant_id, agent_id)
        reviews = self.list_agent_canary_reviews(tenant_id, agent_id,
                                                 [run["task_run_id"] for run in runs])
        missing = failed_reviews = self_reviews = failed_runs = incomplete_runs = 0
        for run in runs:
            if run.get("status") != "WAITING_REVIEW":
                incomplete_runs += 1
            if run.get("failure_reason") or run.get("status") in {"FAILED", "RECONCILING"}:
                failed_runs += 1
            review = reviews.get(run["task_run_id"])
            if review is None:
                missing += 1
            else:
                failed_reviews += review["outcome"] != "PASS"
                self_reviews += review["reviewer_id"] == approver_id
        blockers = []
        if actor is None or not actor["active"]:
            blockers.append("AGENT_INACTIVE")
        if seconds < 60:
            blockers.append("OBSERVATION_PERIOD")
        if len(runs) < 2:
            blockers.append("SAMPLE_COUNT")
        if incomplete_runs:
            blockers.append("RUN_NOT_WAITING_REVIEW")
        if failed_runs:
            blockers.append("RUN_FAILURE")
        if missing:
            blockers.append("REVIEW_MISSING")
        if failed_reviews:
            blockers.append("REVIEW_FAILED")
        if self_reviews:
            blockers.append("SELF_REVIEW_PROMOTION")
        if self.get_agent_snapshot(tenant_id, agent_id, int(route["canary_version"])) is None:
            blockers.append("CANDIDATE_SNAPSHOT_MISSING")
        return {"can_promote": not blockers, "blockers": blockers,
                "candidate_version": int(route["canary_version"]),
                "canary_epoch": int(route["canary_epoch"]),
                "observation_seconds": seconds, "required_observation_seconds": 60,
                "sample_count": len(runs), "required_sample_count": 2,
                "unreviewed_count": missing, "failed_review_count": failed_reviews,
                "failed_run_count": failed_runs, "incomplete_run_count": incomplete_runs,
                "self_review_count": self_reviews}

    def list_agent_promotion_evidence(self, tenant_id: str, agent_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT audit_id, stable_version, candidate_version, canary_epoch, approver_id, "
            "observation_seconds, evidence_json, evidence_hash, created_at "
            "FROM agent_promotion_evidence WHERE tenant_id = ? AND agent_id = ? ORDER BY rowid DESC",
            (tenant_id, agent_id),
        ).fetchall()
        items = []
        for row in rows:
            snapshot = json.loads(row["evidence_json"])
            integrity_ok = (hashlib.sha256(row["evidence_json"].encode()).hexdigest() == row["evidence_hash"]
                            and snapshot.get("agent_id") == agent_id
                            and all(snapshot.get(key) == row[key] for key in (
                                "audit_id", "stable_version", "candidate_version", "canary_epoch",
                                "approver_id", "observation_seconds",
                            )))
            items.append({"audit_id": row["audit_id"], "stable_version": row["stable_version"],
                          "candidate_version": row["candidate_version"], "canary_epoch": row["canary_epoch"],
                          "approver_id": row["approver_id"], "observation_seconds": row["observation_seconds"],
                          "evidence_hash": row["evidence_hash"], "created_at": row["created_at"],
                          "integrity_ok": integrity_ok, "runs": snapshot["runs"]})
        return items

    def latest_agent_evaluation(self, tenant_id: str, draft_id: str, input_hash: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM agent_evaluations WHERE tenant_id = ? AND draft_id = ? AND input_hash = ? "
            "ORDER BY rowid DESC LIMIT 1", (tenant_id, draft_id, input_hash),
        ).fetchone()

    def get_agent_eval_dataset(self, tenant_id: str, dataset_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM agent_eval_datasets WHERE tenant_id = ? AND dataset_id = ?",
            (tenant_id, dataset_id),
        ).fetchone()

    def save_agent_eval_dataset(self, tenant_id: str, dataset_id: str, created_by: str,
                                cases: list[dict[str, Any]], event: Any) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT INTO agent_eval_datasets(tenant_id, dataset_id, created_by, cases_json) "
                "VALUES (?, ?, ?, ?)", (tenant_id, dataset_id, created_by, self._json(cases)),
            )
            self._insert_agent_audit(event)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def save_agent_evaluation(self, *, evaluation_id: str, tenant_id: str, draft_id: str,
                              input_hash: str, evaluator_id: str, dataset_id: str,
                              cases: list[dict[str, Any]], status: str, event: Any) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT INTO agent_evaluations(evaluation_id, tenant_id, draft_id, input_hash, "
                "evaluator_id, dataset_id, cases_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (evaluation_id, tenant_id, draft_id, input_hash, evaluator_id, dataset_id,
                 self._json(cases), status),
            )
            self._insert_agent_audit(event)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _insert_agent_audit(self, event: Any) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.audit_id, event.event_type, event.tenant_id, event.task_run_id,
             self._json(asdict(event))),
        )

    def save_agent_config_draft(self, draft_id: str, tenant_id: str, created_by: str,
                                status: str, data: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO agent_config_drafts(draft_id, tenant_id, created_by, status, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (draft_id, tenant_id, created_by, status, self._json(data)),
        )
        self.connection.commit()

    def list_agent_config_drafts(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT draft_id, tenant_id, created_by, status, data_json, created_at "
            "FROM agent_config_drafts WHERE tenant_id = ? ORDER BY created_at DESC, rowid DESC",
            (tenant_id,),
        ).fetchall()

    def get_agent_config_draft(self, draft_id: str, tenant_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT draft_id, tenant_id, created_by, status, data_json, created_at "
            "FROM agent_config_drafts WHERE draft_id = ? AND tenant_id = ?",
            (draft_id, tenant_id),
        ).fetchone()

    def save_agent_config_review(self, review_id: str, draft_id: str, tenant_id: str,
                                 reviewer_id: str, input_hash: str, status: str,
                                 report: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO agent_config_reviews(review_id, draft_id, tenant_id, reviewer_id, input_hash, status, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (review_id, draft_id, tenant_id, reviewer_id, input_hash, status, self._json(report)),
        )
        self.connection.commit()

    def get_latest_agent_config_review(self, draft_id: str, tenant_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT review_id, draft_id, tenant_id, reviewer_id, input_hash, status, report_json, created_at "
            "FROM agent_config_reviews WHERE draft_id = ? AND tenant_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (draft_id, tenant_id),
        ).fetchone()

    def get_agent_config_approval(self, draft_id: str, tenant_id: str,
                                  input_hash: str | None = None) -> sqlite3.Row | None:
        if input_hash is None:
            return self.connection.execute(
                "SELECT * FROM agent_config_approvals WHERE draft_id = ? AND tenant_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (draft_id, tenant_id),
            ).fetchone()
        return self.connection.execute(
            "SELECT * FROM agent_config_approvals WHERE draft_id = ? AND tenant_id = ? AND input_hash = ?",
            (draft_id, tenant_id, input_hash),
        ).fetchone()

    def publish_agent_config_draft(self, *, draft_id: str, tenant_id: str, approver_id: str,
                                   review_id: str, input_hash: str, agent_id: str,
                                   actor_type: str, capabilities: list[str],
                                   skill_version: str | None, approval_id: str, event: Any) -> dict[str, Any]:
        """Atomically publish a prechecked draft as an immutable AgentVersion."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_agent_config_approval(draft_id, tenant_id, input_hash)
            if existing is not None:
                self.connection.commit()
                return {"approval_id": existing["approval_id"], "agent_id": existing["agent_id"],
                        "version": int(existing["version"]), "status": existing["status"], "duplicate": True}
            draft = self.connection.execute(
                "SELECT status, data_json FROM agent_config_drafts WHERE draft_id = ? AND tenant_id = ?",
                (draft_id, tenant_id),
            ).fetchone()
            if draft is None:
                raise ValueError("DRAFT_NOT_FOUND")
            if draft["status"] == "PUBLISHED":
                raise ValueError("DRAFT_ALREADY_PUBLISHED")
            data = json.loads(draft["data_json"])
            if hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest() != input_hash:
                raise ValueError("PRECHECK_VERSION_STALE")
            review = self.connection.execute(
                "SELECT review_id, input_hash, status FROM agent_config_reviews "
                "WHERE review_id = ? AND draft_id = ? AND tenant_id = ?",
                (review_id, draft_id, tenant_id),
            ).fetchone()
            if review is None or review["input_hash"] != input_hash:
                raise ValueError("PRECHECK_VERSION_STALE")
            if review["status"] != "READY_FOR_ADMIN":
                raise ValueError("PRECHECK_NOT_READY")
            base_version = data.get("base_version")
            actor = self.connection.execute("SELECT * FROM actors WHERE actor_id = ?", (agent_id,)).fetchone()
            if base_version is None:
                name_conflict = self.connection.execute(
                    "SELECT 1 FROM actors WHERE tenant_id = ? AND lower(actor_id) = lower(?)",
                    (tenant_id, agent_id),
                ).fetchone()
                if actor is not None or name_conflict is not None:
                    raise ValueError("AGENT_NAME_CONFLICT")
                version = 1
                self.connection.execute(
                    "INSERT INTO actors(actor_id, tenant_id, actor_type, capabilities_json, roles_json, external_ref_hash, active) "
                    "VALUES (?, ?, ?, ?, ?, NULL, 1)",
                    (agent_id, tenant_id, actor_type, self._json(sorted(capabilities)), self._json([])),
                )
                active = 1
            else:
                route = self.get_agent_route(tenant_id, agent_id)
                if (actor is None or actor["tenant_id"] != tenant_id or actor["actor_type"] != actor_type
                        or not actor["active"] or route is None or route["status"] != "ACTIVE"
                        or int(route["stable_version"]) != base_version):
                    raise ValueError("AGENT_BASE_VERSION_STALE")
                evaluation = self.latest_agent_evaluation(tenant_id, draft_id, input_hash)
                if evaluation is None or evaluation["status"] != "PASSED":
                    raise ValueError("AGENT_EVALUATION_REQUIRED")
                if evaluation["evaluator_id"] == approver_id:
                    raise ValueError("SELF_EVALUATION_APPROVAL")
                latest = self.connection.execute(
                    "SELECT MAX(version) AS version FROM agent_versions WHERE agent_id = ?", (agent_id,),
                ).fetchone()
                version = int(latest["version"]) + 1
                active = 0
            self.connection.execute(
                "INSERT INTO agent_versions(agent_id, version, capabilities_json, active, skill_version) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, version, self._json(sorted(capabilities)), active, skill_version),
            )
            self.connection.execute(
                "INSERT INTO agent_version_snapshots(agent_id, version, tenant_id, draft_id, config_json, config_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, version, tenant_id, draft_id, self._json(data), input_hash),
            )
            if version == 1:
                self.connection.execute(
                    "INSERT INTO agent_routes(agent_id, tenant_id, stable_version, canary_version, "
                    "volunteer_ids_json, status) VALUES (?, ?, 1, NULL, '[]', 'ACTIVE')",
                    (agent_id, tenant_id),
                )
            self.connection.execute(
                "UPDATE agent_config_drafts SET status = 'PUBLISHED' WHERE draft_id = ? AND tenant_id = ?",
                (draft_id, tenant_id),
            )
            self.connection.execute(
                "INSERT INTO agent_config_approvals(approval_id, draft_id, tenant_id, approver_id, review_id, "
                "input_hash, agent_id, version, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISHED')",
                (approval_id, draft_id, tenant_id, approver_id, review_id, input_hash, agent_id, version),
            )
            self._insert_agent_audit(event)
            self.connection.commit()
            return {"approval_id": approval_id, "agent_id": agent_id, "version": version,
                    "status": "PUBLISHED", "duplicate": False}
        except Exception:
            self.connection.rollback()
            raise

    def transition_agent_route(self, *, tenant_id: str, agent_id: str, action: str,
                               expected_version: int, target_version: int | None,
                               volunteer_ids: list[str], event: Any) -> bool:
        """Move a reviewed agent route and its audit record in one transaction."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            route = self.get_agent_route(tenant_id, agent_id)
            actor = self.connection.execute(
                "SELECT active FROM actors WHERE actor_id = ? AND tenant_id = ?", (agent_id, tenant_id),
            ).fetchone()
            if route is None or actor is None or route["status"] == "ARCHIVED":
                self.connection.rollback()
                return False
            stable = int(route["stable_version"])
            if stable != expected_version:
                self.connection.rollback()
                return False
            if action == "CANARY":
                if not actor["active"] or route["status"] != "ACTIVE" or target_version is None or target_version <= stable:
                    self.connection.rollback()
                    return False
                snapshot = self.get_agent_snapshot(tenant_id, agent_id, target_version)
                if snapshot is None:
                    self.connection.rollback()
                    return False
                evaluation = self.latest_agent_evaluation(tenant_id, snapshot["draft_id"], snapshot["config_hash"])
                if evaluation is None or evaluation["status"] != "PASSED":
                    self.connection.rollback()
                    return False
                for member_id in volunteer_ids:
                    member = self.connection.execute(
                        "SELECT 1 FROM actors WHERE actor_id = ? AND tenant_id = ? "
                        "AND actor_type = 'USER' AND active = 1", (member_id, tenant_id),
                    ).fetchone()
                    consent = self.agent_volunteer_consent(tenant_id, agent_id, member_id)
                    if (member is None or consent is None or not consent["active"]
                            or consent["candidate_version"] != target_version):
                        self.connection.rollback()
                        return False
                self.connection.execute(
                    "UPDATE agent_versions SET active = 1 WHERE agent_id = ? AND version = ?",
                    (agent_id, target_version),
                )
                self.connection.execute(
                    "UPDATE agent_routes SET canary_version = ?, volunteer_ids_json = ?, "
                    "status = 'CANARY', canary_epoch = canary_epoch + 1, "
                    "updated_at = CURRENT_TIMESTAMP WHERE agent_id = ?",
                    (target_version, self._json(volunteer_ids), agent_id),
                )
            elif action == "PROMOTE":
                if not actor["active"] or route["status"] != "CANARY" or target_version != route["canary_version"]:
                    self.connection.rollback()
                    return False
                readiness = self.agent_promotion_readiness(tenant_id, agent_id, event.actor_id)
                if not readiness["can_promote"]:
                    self.connection.rollback()
                    return False
                samples = self.agent_canary_runs(tenant_id, agent_id)
                reviews = self.list_agent_canary_reviews(
                    tenant_id, agent_id, [run["task_run_id"] for run in samples],
                )
                evidence = {"audit_id": event.audit_id, "agent_id": agent_id,
                            "stable_version": stable, "candidate_version": target_version,
                            "canary_epoch": route["canary_epoch"], "approver_id": event.actor_id,
                            "observation_seconds": readiness["observation_seconds"],
                            "runs": [{"task_run_id": run["task_run_id"], "status": run["status"],
                                      "reviewer_id": reviews[run["task_run_id"]]["reviewer_id"],
                                      "outcome": reviews[run["task_run_id"]]["outcome"],
                                      "evidence_ref": reviews[run["task_run_id"]]["evidence_ref"]}
                                     for run in sorted(samples, key=lambda item: item["task_run_id"])]}
                evidence_json = self._json(evidence)
                self.connection.execute(
                    "INSERT INTO agent_promotion_evidence(audit_id, tenant_id, agent_id, stable_version, "
                    "candidate_version, canary_epoch, approver_id, observation_seconds, evidence_json, "
                    "evidence_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event.audit_id, tenant_id, agent_id, stable, target_version, route["canary_epoch"],
                     event.actor_id, readiness["observation_seconds"], evidence_json,
                     hashlib.sha256(evidence_json.encode()).hexdigest()),
                )
                snapshot = self.get_agent_snapshot(tenant_id, agent_id, target_version)
                config = json.loads(snapshot["config_json"])
                self.connection.execute(
                    "UPDATE actors SET capabilities_json = ? WHERE actor_id = ?",
                    (self._json(config["capabilities"]), agent_id),
                )
                self.connection.execute(
                    "UPDATE agent_versions SET active = CASE WHEN version = ? THEN 1 ELSE 0 END WHERE agent_id = ?",
                    (target_version, agent_id),
                )
                self.connection.execute(
                    "UPDATE agent_routes SET stable_version = ?, canary_version = NULL, "
                    "volunteer_ids_json = '[]', status = 'ACTIVE', updated_at = CURRENT_TIMESTAMP WHERE agent_id = ?",
                    (target_version, agent_id),
                )
            elif action == "ROLLBACK":
                if target_version is None or target_version == stable:
                    if route["status"] != "CANARY" or target_version != stable:
                        self.connection.rollback()
                        return False
                elif route["status"] != "ACTIVE" or target_version >= stable:
                    self.connection.rollback()
                    return False
                snapshot = self.get_agent_snapshot(tenant_id, agent_id, target_version)
                if snapshot is None:
                    self.connection.rollback()
                    return False
                config = json.loads(snapshot["config_json"])
                self.connection.execute(
                    "UPDATE actors SET capabilities_json = ? WHERE actor_id = ?",
                    (self._json(config["capabilities"]), agent_id),
                )
                self.connection.execute(
                    "UPDATE agent_versions SET active = CASE WHEN version = ? THEN 1 ELSE 0 END WHERE agent_id = ?",
                    (target_version, agent_id),
                )
                self.connection.execute(
                    "UPDATE agent_routes SET stable_version = ?, canary_version = NULL, "
                    "volunteer_ids_json = '[]', status = 'ACTIVE', updated_at = CURRENT_TIMESTAMP WHERE agent_id = ?",
                    (target_version, agent_id),
                )
            elif action == "ARCHIVE":
                self.connection.execute("UPDATE actors SET active = 0 WHERE actor_id = ?", (agent_id,))
                self.connection.execute("UPDATE agent_versions SET active = 0 WHERE agent_id = ?", (agent_id,))
                self.connection.execute(
                    "UPDATE agent_routes SET canary_version = NULL, volunteer_ids_json = '[]', "
                    "status = 'ARCHIVED', updated_at = CURRENT_TIMESTAMP WHERE agent_id = ?", (agent_id,),
                )
            else:
                raise ValueError("UNKNOWN_AGENT_ACTION")
            if action in {"PROMOTE", "ROLLBACK", "ARCHIVE"}:
                self.connection.execute(
                    "UPDATE agent_volunteer_consents SET active = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = ? AND agent_id = ?", (tenant_id, agent_id),
                )
            self._insert_agent_audit(event)
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def agent_run_diagnostics(self, tenant_id: str, agent_id: str) -> dict[str, Any]:
        """Aggregate persisted runs without returning member input or output."""
        rows = self.connection.execute(
            "SELECT data_json FROM task_runs WHERE tenant_id = ?", (tenant_id,),
        ).fetchall()
        by_version: dict[int, dict[str, Any]] = {}
        run_versions: dict[str, int] = {}
        for row in rows:
            run = json.loads(row["data_json"])
            if run.get("agent_id") != agent_id:
                continue
            version = int(run["agent_version"])
            run_versions[str(run["task_run_id"])] = version
            item = by_version.setdefault(version, {"total": 0, "statuses": {}, "failures": 0,
                                                   "runtime_events": {}})
            item["total"] += 1
            status = str(run.get("status", "UNKNOWN"))
            item["statuses"][status] = item["statuses"].get(status, 0) + 1
            if status in {"FAILED", "RECONCILING"} or run.get("failure_reason"):
                item["failures"] += 1
        if run_versions:
            events = self.connection.execute(
                "SELECT runtime_events.task_run_id, runtime_events.data_json FROM runtime_events "
                "JOIN task_runs ON task_runs.task_run_id = runtime_events.task_run_id "
                "WHERE task_runs.tenant_id = ?", (tenant_id,),
            ).fetchall()
            for event in events:
                version = run_versions.get(event["task_run_id"])
                if version is None:
                    continue
                event_type = str(json.loads(event["data_json"]).get("event_type", "UNKNOWN"))
                counts = by_version[version]["runtime_events"]
                counts[event_type] = counts.get(event_type, 0) + 1
        return {"versions": by_version, "total": sum(item["total"] for item in by_version.values())}

    def save_binding(self, binding: Any) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO assistant_bindings VALUES (?, ?, ?, ?, ?)",
            (binding.binding_id, binding.tenant_id, binding.member_actor_id, binding.assistant_actor_id, int(binding.default)),
        )
        self.connection.commit()

    def get_binding(self, member_actor_id: str, tenant_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM assistant_bindings WHERE member_actor_id = ? AND tenant_id = ? AND is_default = 1 LIMIT 1",
            (member_actor_id, tenant_id),
        ).fetchone()

    def save_user_oauth_token(self, *, actor_id: str, tenant_id: str,
                              subject_ref_hash: str, scopes: frozenset[str],
                              token_ciphertext: str, expires_at: int) -> None:
        self.connection.execute(
            "INSERT INTO user_oauth_tokens "
            "(actor_id, tenant_id, subject_ref_hash, scopes_json, token_ciphertext, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(actor_id) DO UPDATE SET "
            "tenant_id = excluded.tenant_id, subject_ref_hash = excluded.subject_ref_hash, "
            "scopes_json = excluded.scopes_json, token_ciphertext = excluded.token_ciphertext, "
            "expires_at = excluded.expires_at, updated_at = CURRENT_TIMESTAMP",
            (actor_id, tenant_id, subject_ref_hash, self._json(sorted(scopes)), token_ciphertext, expires_at),
        )

    def get_user_oauth_token(self, actor_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM user_oauth_tokens WHERE actor_id = ?", (actor_id,),
        ).fetchone()

    def delete_user_oauth_token(self, actor_id: str) -> None:
        self.connection.execute("DELETE FROM user_oauth_tokens WHERE actor_id = ?", (actor_id,))

    def save_task_run(self, task_run: Any) -> None:
        data = asdict(task_run)
        for key in ("status",):
            if hasattr(data.get(key), "value"):
                data[key] = data[key].value
        self.connection.execute(
            "INSERT OR REPLACE INTO task_runs VALUES (?, ?, ?)",
            (task_run.task_run_id, task_run.tenant_id, self._json(data)),
        )
        self.connection.commit()

    def get_task_run(self, task_run_id: str) -> dict[str, Any] | None:
        return self._decode(self.connection.execute("SELECT data_json FROM task_runs WHERE task_run_id = ?", (task_run_id,)).fetchone())

    def get_dispatch(self, dispatch_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT dispatch_key, task_run_id, data_json FROM dispatches WHERE dispatch_key = ?", (dispatch_key,)
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["data_json"])
        data["dispatch_key"] = row["dispatch_key"]
        data["task_run_id"] = row["task_run_id"]
        return data

    def save_task_run_with_dispatch(self, task_run: Any, dispatch_key: str, dispatch_data: dict[str, Any]) -> bool:
        """Atomically claim a dispatch key and insert its first TaskRun."""
        data = asdict(task_run)
        if hasattr(data.get("status"), "value"):
            data["status"] = data["status"].value
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO dispatches(dispatch_key, task_run_id, data_json) VALUES (?, ?, ?)",
                (dispatch_key, task_run.task_run_id, self._json(dispatch_data)),
            )
            self.connection.execute(
                "INSERT INTO task_runs(task_run_id, tenant_id, data_json) VALUES (?, ?, ?)",
                (task_run.task_run_id, task_run.tenant_id, self._json(data)),
            )
            self.connection.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self.connection.execute("ROLLBACK")
            return False
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def save_task_step(self, step: Any) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO task_steps VALUES (?, ?, ?)", (step.step_id, step.task_run_id, self._json(asdict(step)))
        )
        self.connection.commit()

    def list_task_steps(self, task_run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM task_steps WHERE task_run_id = ? ORDER BY rowid", (task_run_id,)
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def save_delegation(self, delegation: Any) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO delegations VALUES (?, ?, ?)",
            (delegation.delegation_id, delegation.task_run_id, self._json(asdict(delegation))),
        )
        self.connection.commit()

    def list_delegations(self, task_run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM delegations WHERE task_run_id = ? ORDER BY rowid", (task_run_id,)
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def save_runtime_event(self, event_key: str, task_run_id: str, sequence: int, data: dict[str, Any]) -> bool:
        try:
            self.connection.execute(
                "INSERT INTO runtime_events(event_key, task_run_id, sequence, data_json) VALUES (?, ?, ?, ?)",
                (event_key, task_run_id, sequence, self._json(data)),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_runtime_event(self, event_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT data_json FROM runtime_events WHERE event_key = ?", (event_key,)
        ).fetchone()
        return None if row is None else json.loads(row["data_json"])

    def list_runtime_events(self, task_run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM runtime_events WHERE task_run_id = ? ORDER BY sequence", (task_run_id,)
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def claim_wake(
        self,
        wake_key: str,
        *,
        tenant_id: str,
        task_run_id: str,
        trigger_type: str,
        source_revision: str | int | None,
        claim_token: str,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically admit one wake-up for a task/trigger/revision tuple.

        A duplicate event is intentionally a no-op. Callers should load the
        existing wake row and report its persisted result instead of running
        the assistant or sending another notification.
        """
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO wake_events(wake_key, tenant_id, task_run_id, trigger_type, source_revision, status, claim_token, data_json) "
                "VALUES (?, ?, ?, ?, ?, 'CLAIMED', ?, ?)",
                (wake_key, tenant_id, task_run_id, trigger_type, "" if source_revision is None else str(source_revision),
                 claim_token, self._json(data or {})),
            )
            self.connection.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self.connection.execute("ROLLBACK")
            return False
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def get_wake(self, wake_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM wake_events WHERE wake_key = ?", (wake_key,)
        ).fetchone()

    def complete_wake(
        self,
        wake_key: str,
        claim_token: str,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        if status not in {"COMPLETED", "BLOCKED", "FAILED"}:
            raise ValueError("unsupported wake terminal status")
        cursor = self.connection.execute(
            "UPDATE wake_events SET status = ?, result_json = ?, completed_at = CURRENT_TIMESTAMP "
            "WHERE wake_key = ? AND claim_token = ? AND status = 'CLAIMED'",
            (status, self._json(result or {}), wake_key, claim_token),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    @contextmanager
    def owner_decision_transaction(self):
        """Isolate owner actions from other request connections and commits.

        File databases get a short-lived connection; memory stores serialize
        owner actions on their sole connection. Helpers that commit must not
        be called within this transaction.
        """
        with self._owner_decision_lock:
            connection = self.connection
            if self.path != ":memory:":
                connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                if connection is not self.connection:
                    connection.close()

    def save_follow_up_observation(
        self, observation_id: str, *, tenant_id: str, task_run_id: str, data: dict[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO follow_up_observations(observation_id, tenant_id, task_run_id, data_json) "
            "VALUES (?, ?, ?, ?)",
            (observation_id, tenant_id, task_run_id, self._json(data)),
        )
        self.connection.commit()

    def list_follow_up_observations(self, task_run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM follow_up_observations WHERE task_run_id = ? ORDER BY rowid",
            (task_run_id,),
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def list_follow_up_observations_for_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM follow_up_observations WHERE tenant_id = ? ORDER BY rowid",
            (tenant_id,),
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def claim_notification(
        self,
        notification_key: str,
        *,
        tenant_id: str,
        recipient_actor_id: str,
        channel: str,
        claim_token: str,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically reserve one notification without sending it."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO notification_deliveries(notification_key, tenant_id, recipient_actor_id, channel, status, claim_token, data_json) "
                "VALUES (?, ?, ?, ?, 'CLAIMED', ?, ?)",
                (notification_key, tenant_id, recipient_actor_id, channel, claim_token, self._json(data or {})),
            )
            self.connection.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self.connection.execute("ROLLBACK")
            return False
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def get_notification(self, notification_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM notification_deliveries WHERE notification_key = ?", (notification_key,)
        ).fetchone()

    def complete_notification(
        self,
        notification_key: str,
        claim_token: str,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        if status not in {"SENT", "FAILED", "UNKNOWN"}:
            raise ValueError("unsupported notification terminal status")
        cursor = self.connection.execute(
            "UPDATE notification_deliveries SET status = ?, result_json = ?, completed_at = CURRENT_TIMESTAMP "
            "WHERE notification_key = ? AND claim_token = ? AND status = 'CLAIMED'",
            (status, self._json(result or {}), notification_key, claim_token),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def save_meeting_record(self, record: Any) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO meeting_records(record_id, tenant_id, document_id_hash, revision_id, data_json) VALUES (?, ?, ?, ?, ?)",
            (record.record_id, record.tenant_id, record.document_id_hash, record.revision_id, self._json(asdict(record))),
        )
        self.connection.commit()

    def get_meeting_record(self, record_id: str) -> dict[str, Any] | None:
        return self._decode(self.connection.execute("SELECT data_json FROM meeting_records WHERE record_id = ?", (record_id,)).fetchone())

    def list_member_meetings(self, tenant_id: str, member_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM meeting_records WHERE tenant_id = ? "
            "AND json_extract(data_json, '$.submitted_by') = ? ORDER BY rowid DESC",
            (tenant_id, member_id),
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def save_meeting_source(self, record_id: str, data: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO meeting_sources VALUES (?, ?)", (record_id, self._json(data)),
        )
        self.connection.commit()

    def get_meeting_source(self, record_id: str) -> dict[str, Any] | None:
        return self._decode(self.connection.execute(
            "SELECT data_json FROM meeting_sources WHERE record_id = ?", (record_id,),
        ).fetchone())

    def list_bound_members(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT a.actor_id, p.display_name FROM actors AS a "
            "LEFT JOIN member_profiles AS p ON p.actor_id = a.actor_id AND p.tenant_id = a.tenant_id "
            "WHERE a.tenant_id = ? AND a.actor_type = 'USER' "
            "AND a.active = 1 AND a.external_ref_hash IS NOT NULL", (tenant_id,),
        ).fetchall()
        return [{"actor_id": row["actor_id"], "name": row["display_name"]} for row in rows]

    def list_member_directory(self, tenant_id: str) -> list[sqlite3.Row]:
        """Return local member identities without OAuth subject hashes."""
        return self.connection.execute(
            "SELECT a.actor_id, a.roles_json, a.active, r.decided_at AS joined_at, p.display_name "
            "FROM actors AS a LEFT JOIN member_access_requests AS r "
            "ON r.tenant_id = a.tenant_id AND r.actor_id = a.actor_id AND r.status = 'APPROVED' "
            "LEFT JOIN member_profiles AS p ON p.actor_id = a.actor_id AND p.tenant_id = a.tenant_id "
            "WHERE a.tenant_id = ? AND a.actor_type = 'USER' AND a.external_ref_hash IS NOT NULL "
            "ORDER BY a.actor_id", (tenant_id,),
        ).fetchall()

    def set_member_active(self, tenant_id: str, actor_id: str, expected_active: bool,
                          active: bool, reason: str, event: Any) -> bool:
        """Change one member's active state and audit it in one transaction."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            updated = self.connection.execute(
                "UPDATE actors SET active = ? WHERE tenant_id = ? AND actor_id = ? "
                "AND actor_type = 'USER' AND external_ref_hash IS NOT NULL AND active = ?",
                (int(active), tenant_id, actor_id, int(expected_active)),
            )
            if updated.rowcount != 1:
                self.connection.rollback()
                return False
            self.connection.execute(
                "INSERT INTO member_status_changes(audit_id, tenant_id, actor_id, active, decided_by, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event.audit_id, tenant_id, actor_id, int(active), event.actor_id, reason),
            )
            self.connection.execute(
                "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (event.audit_id, event.event_type, event.tenant_id, event.task_run_id,
                 self._json(asdict(event))),
            )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def latest_member_status_changes(self, tenant_id: str) -> dict[str, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT actor_id, active, reason, created_at FROM member_status_changes "
            "WHERE tenant_id = ? ORDER BY rowid DESC", (tenant_id,),
        ).fetchall()
        return {row["actor_id"]: row for row in reversed(rows)}

    def set_member_reviewer_role(self, tenant_id: str, actor_id: str, expected_reviewer: bool,
                                 reviewer: bool, reason: str, event: Any) -> bool:
        """Compare and change an active member's reviewer role with its audit record."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT roles_json FROM actors WHERE tenant_id = ? AND actor_id = ? "
                "AND actor_type = 'USER' AND external_ref_hash IS NOT NULL AND active = 1",
                (tenant_id, actor_id),
            ).fetchone()
            if row is None:
                self.connection.rollback()
                return False
            roles = set(json.loads(row["roles_json"]))
            if "admin" in roles or ("agent_reviewer" in roles) != expected_reviewer \
                    or expected_reviewer == reviewer:
                self.connection.rollback()
                return False
            if reviewer:
                roles.add("agent_reviewer")
            else:
                roles.remove("agent_reviewer")
            self.connection.execute(
                "UPDATE actors SET roles_json = ? WHERE tenant_id = ? AND actor_id = ?",
                (self._json(sorted(roles)), tenant_id, actor_id),
            )
            self.connection.execute(
                "INSERT INTO member_reviewer_role_changes(audit_id, tenant_id, actor_id, reviewer, decided_by, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event.audit_id, tenant_id, actor_id, int(reviewer), event.actor_id, reason),
            )
            self.connection.execute(
                "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (event.audit_id, event.event_type, event.tenant_id, event.task_run_id,
                 self._json(asdict(event))),
            )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def latest_member_reviewer_role_changes(self, tenant_id: str) -> dict[str, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT actor_id, reviewer, reason, created_at FROM member_reviewer_role_changes "
            "WHERE tenant_id = ? ORDER BY rowid DESC", (tenant_id,),
        ).fetchall()
        return {row["actor_id"]: row for row in reversed(rows)}

    def save_meeting_todo(self, draft: Any) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO meeting_todo_drafts(draft_id, record_id, todo_id, data_json) VALUES (?, ?, ?, ?)",
            (draft.draft_id, draft.record_id, draft.todo_id, self._json(asdict(draft))),
        )
        self.connection.commit()

    def save_meeting_todos(self, drafts: Iterable[Any]) -> None:
        """Persist one reviewed draft set together after all edits pass validation."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.executemany(
                "INSERT OR REPLACE INTO meeting_todo_drafts(draft_id, record_id, todo_id, data_json) VALUES (?, ?, ?, ?)",
                (
                    (draft.draft_id, draft.record_id, draft.todo_id, self._json(asdict(draft)))
                    for draft in drafts
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def get_meeting_todo(self, record_id: str, todo_id: str) -> dict[str, Any] | None:
        return self._decode(self.connection.execute(
            "SELECT data_json FROM meeting_todo_drafts WHERE record_id = ? AND todo_id = ?", (record_id, todo_id)
        ).fetchone())

    def list_meeting_todos(self, record_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM meeting_todo_drafts WHERE record_id = ? ORDER BY rowid", (record_id,)
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def save_approval(self, approval: Any, data: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO approvals VALUES (?, ?, ?, ?, ?, ?)",
            (approval.approval_id, approval.task_run_id, approval.proposal.action_version,
             approval.proposal.proposal_hash, str(approval.status), self._json(data)),
        )
        self.connection.commit()

    def get_approval_action(self, action_key: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT result_json FROM approval_actions WHERE action_key = ?", (action_key,)).fetchone()
        return None if row is None else json.loads(row["result_json"])

    def save_approval_and_effects(
        self,
        approval: Any,
        approval_data: dict[str, Any],
        action_key: str,
        action_result: dict[str, Any],
        event_key: str,
        call_data: dict[str, Any] | None = None,
        outbox_data: tuple[str, str, str, dict[str, Any]] | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Atomically commit an approval decision, action fence, inbox row and side effect."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "INSERT INTO approval_actions(action_key, approval_id, action_version, decision, result_json) VALUES (?, ?, ?, ?, ?)",
                    (action_key, approval.approval_id, approval.proposal.action_version, approval.status.value, self._json(action_result)),
                )
                self.connection.execute(
                    "INSERT OR REPLACE INTO approvals VALUES (?, ?, ?, ?, ?, ?)",
                    (approval.approval_id, approval.task_run_id, approval.proposal.action_version,
                     approval.proposal.proposal_hash, approval.status.value, self._json(approval_data)),
                )
                if call_data is not None:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO tool_calls(tool_call_id, write_idempotency_key, status, data_json) VALUES (?, ?, ?, ?)",
                        (call_data["tool_call_id"], call_data["write_idempotency_key"], call_data["status"], self._json(call_data)),
                    )
                if outbox_data is not None:
                    outbox_id, write_key, status, data = outbox_data
                    self.connection.execute(
                        "INSERT OR IGNORE INTO outbox(outbox_id, write_idempotency_key, status, data_json) VALUES (?, ?, ?, ?)",
                        (outbox_id, write_key, status, self._json(data)),
                    )
                self.connection.execute(
                    "INSERT OR IGNORE INTO inbox_events(event_key, result_json) VALUES (?, ?)",
                    (event_key, self._json(action_result)),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            return True, action_result
        except sqlite3.IntegrityError:
            prior = self.get_approval_action(action_key)
            return False, prior

    def get_approval(self, approval_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()

    def save_approval_delivery(self, approval_id: str, message_id: str,
                               recipient_actor_id: str, proposal_hash: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO approval_deliveries VALUES (?, ?, ?, ?)",
            (approval_id, message_id, recipient_actor_id, proposal_hash),
        )
        self.connection.commit()
        prior = self.get_approval_delivery(approval_id)
        if prior is None or (prior["message_id"], prior["recipient_actor_id"], prior["proposal_hash"]) != (
            message_id, recipient_actor_id, proposal_hash
        ):
            raise ValueError("approval delivery conflicts with prior message")

    def get_approval_delivery(self, approval_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM approval_deliveries WHERE approval_id = ?", (approval_id,)
        ).fetchone()

    def claim_approval_delivery(self, approval_id: str, recipient_actor_id: str,
                                proposal_hash: str, idempotency_key: str) -> bool:
        # Commit the send fence before contacting Feishu, across worker processes.
        result = self.connection.execute(
            "INSERT OR IGNORE INTO approval_delivery_attempts "
            "(approval_id, recipient_actor_id, proposal_hash, idempotency_key, status) "
            "VALUES (?, ?, ?, ?, 'SENDING')",
            (approval_id, recipient_actor_id, proposal_hash, idempotency_key),
        )
        return result.rowcount == 1

    def get_approval_delivery_attempt(self, approval_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM approval_delivery_attempts WHERE approval_id = ?", (approval_id,)
        ).fetchone()

    def finish_approval_delivery_attempt(self, approval_id: str, status: str) -> None:
        if status not in {"SENT", "UNKNOWN"}:
            raise ValueError("unsupported card delivery result")
        self.connection.execute(
            "UPDATE approval_delivery_attempts SET status = ? WHERE approval_id = ? AND status = 'SENDING'",
            (status, approval_id),
        )

    def claim_assistant_card_delivery(self, record_id: str, recipient_actor_id: str,
                                      idempotency_key: str, review_snapshot: str | None = None) -> bool:
        """Persist the send fence before contacting Feishu."""
        row = self.connection.execute(
            "INSERT OR IGNORE INTO assistant_card_deliveries "
            "(record_id, recipient_actor_id, idempotency_key, status, review_snapshot) "
            "VALUES (?, ?, ?, 'SENDING', ?)",
            (record_id, recipient_actor_id, idempotency_key, review_snapshot),
        )
        return row.rowcount == 1

    def get_assistant_card_delivery(self, record_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM assistant_card_deliveries WHERE record_id = ?", (record_id,),
        ).fetchone()

    def get_assistant_card_delivery_by_message(self, message_id: str) -> sqlite3.Row | None:
        rows = self.connection.execute(
            "SELECT * FROM assistant_card_deliveries WHERE message_id = ? LIMIT 2", (message_id,),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def finish_assistant_card_delivery(self, record_id: str, *, status: str,
                                       message_id: str | None = None) -> None:
        if status not in {"SENT", "UNKNOWN"} or (status == "SENT" and not message_id):
            raise ValueError("invalid assistant card delivery result")
        updated = self.connection.execute(
            "UPDATE assistant_card_deliveries SET status = ?, message_id = ? "
            "WHERE record_id = ? AND status = 'SENDING'",
            (status, message_id, record_id),
        )
        if updated.rowcount != 1:
            raise ValueError("assistant card delivery is not pending")

    def enqueue_assistant_message(self, message_hash: str, tenant_id: str,
                                  data: dict[str, Any]) -> bool:
        row = self.connection.execute(
            "INSERT OR IGNORE INTO assistant_message_inbox "
            "(message_hash, tenant_id, status, data_json) VALUES (?, ?, 'PENDING', ?)",
            (message_hash, tenant_id, self._json(data)),
        )
        return row.rowcount == 1

    def claim_assistant_message(self, tenant_id: str, claim_token: str,
                                *, lease_seconds: int = 300) -> dict[str, Any] | None:
        now = time.time()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM assistant_message_inbox WHERE tenant_id = ? AND "
                "(status = 'PENDING' OR (status = 'CLAIMED' AND lease_until < ?)) "
                "ORDER BY created_at, rowid LIMIT 1", (tenant_id, now),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            self.connection.execute(
                "UPDATE assistant_message_inbox SET status = 'CLAIMED', claim_token = ?, lease_until = ? "
                "WHERE message_hash = ?",
                (claim_token, now + lease_seconds, row["message_hash"]),
            )
            self.connection.execute("COMMIT")
            return {"message_hash": row["message_hash"], "data": json.loads(row["data_json"]),
                    "reclaimed": row["status"] == "CLAIMED"}
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def finish_assistant_message(self, message_hash: str, claim_token: str,
                                 status: str, result: dict[str, Any]) -> bool:
        if status not in {"DONE", "REJECTED", "UNKNOWN"}:
            raise ValueError("invalid assistant inbox result")
        row = self.connection.execute(
            "UPDATE assistant_message_inbox SET status = ?, claim_token = NULL, "
            "lease_until = NULL, result_json = ?, "
            "data_json = CASE WHEN ? IN ('DONE', 'REJECTED') THEN '{}' ELSE data_json END "
            "WHERE message_hash = ? AND claim_token = ? AND status = 'CLAIMED'",
            (status, self._json(result), status, message_hash, claim_token),
        )
        return row.rowcount == 1

    def enqueue_meeting_card_review(self, message_id: str, tenant_id: str,
                                    data: dict[str, Any]) -> bool:
        row = self.connection.execute(
            "INSERT OR IGNORE INTO meeting_card_review_inbox "
            "(message_id, tenant_id, status, data_json) VALUES (?, ?, 'PENDING', ?)",
            (message_id, tenant_id, self._json(data)),
        )
        return row.rowcount == 1

    def claim_meeting_card_review(self, tenant_id: str, claim_token: str,
                                  *, lease_seconds: int = 300) -> dict[str, Any] | None:
        now = time.time()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM meeting_card_review_inbox WHERE tenant_id = ? AND "
                "(status = 'PENDING' OR (status = 'CLAIMED' AND lease_until < ?)) "
                "ORDER BY created_at, rowid LIMIT 1", (tenant_id, now),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            self.connection.execute(
                "UPDATE meeting_card_review_inbox SET status = 'CLAIMED', claim_token = ?, lease_until = ? "
                "WHERE message_id = ?", (claim_token, now + lease_seconds, row["message_id"]),
            )
            self.connection.execute("COMMIT")
            return {"message_id": row["message_id"], "data": json.loads(row["data_json"])}
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def finish_meeting_card_review(self, message_id: str, claim_token: str,
                                   status: str, result: dict[str, Any]) -> bool:
        if status not in {"DONE", "REJECTED"}:
            raise ValueError("invalid meeting card review result")
        row = self.connection.execute(
            "UPDATE meeting_card_review_inbox SET status = ?, claim_token = NULL, "
            "lease_until = NULL, result_json = ?, data_json = '{}' "
            "WHERE message_id = ? AND claim_token = ? AND status = 'CLAIMED'",
            (status, self._json(result), message_id, claim_token),
        )
        return row.rowcount == 1

    def list_approvals(self, task_run_id: str | None = None) -> list[sqlite3.Row]:
        if task_run_id is None:
            return self.connection.execute("SELECT * FROM approvals ORDER BY rowid").fetchall()
        return self.connection.execute("SELECT * FROM approvals WHERE task_run_id = ? ORDER BY rowid", (task_run_id,)).fetchall()

    def record_inbox_event(self, event_key: str, result: dict[str, Any]) -> bool:
        try:
            self.connection.execute("INSERT INTO inbox_events(event_key, result_json) VALUES (?, ?)", (event_key, self._json(result)))
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_inbox_event(self, event_key: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT result_json FROM inbox_events WHERE event_key = ?", (event_key,)).fetchone()
        return None if row is None else json.loads(row["result_json"])

    def save_outbox(self, outbox_id: str, write_key: str, status: str, data: dict[str, Any]) -> bool:
        try:
            self.connection.execute(
                "INSERT INTO outbox(outbox_id, write_idempotency_key, status, data_json) VALUES (?, ?, ?, ?)",
                (outbox_id, write_key, status, self._json(data)),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_outbox_by_key(self, write_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM outbox WHERE write_idempotency_key = ?", (write_key,)).fetchone()

    def get_outbox_for_tool_call(self, tool_call_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM outbox WHERE json_extract(data_json, '$.tool_call_id') = ?",
            (tool_call_id,),
        ).fetchone()

    def claim_outbox_for_tool_call(
        self,
        tool_call_id: str,
        claim_token: str,
        *,
        lease_seconds: float = 60.0,
        now: float | None = None,
    ) -> sqlite3.Row | None:
        """Atomically claim an outbox item before a side-effecting call.

        An expired claim is recoverable only while the associated tool call is
        still PREPARED. Once the call is DISPATCHED, the remote result may be
        unknown and the item must go through reconciliation instead of being
        dispatched again.
        """
        now = time.time() if now is None else float(now)
        lease_until = now + max(1.0, float(lease_seconds))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM outbox WHERE json_extract(data_json, '$.tool_call_id') = ?",
                (tool_call_id,),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            call = self.connection.execute(
                "SELECT json_extract(data_json, '$.status') AS status FROM tool_calls WHERE tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
            call_status = None if call is None else call["status"]
            claimable_status = row["status"] == "READY" or (
                row["status"] == "CLAIMED"
                and (row["lease_until"] is None or float(row["lease_until"]) <= now)
            )
            if not claimable_status or call_status not in (None, "PREPARED"):
                self.connection.execute("COMMIT")
                return None
            updated = self.connection.execute(
                "UPDATE outbox SET status = 'CLAIMED', claim_token = ?, lease_until = ?, "
                "attempt_count = attempt_count + 1 WHERE outbox_id = ? AND "
                "(status = 'READY' OR (status = 'CLAIMED' AND (lease_until IS NULL OR lease_until <= ?)))",
                (claim_token, lease_until, row["outbox_id"], now),
            )
            if updated.rowcount != 1:
                self.connection.execute("COMMIT")
                return None
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return self.connection.execute("SELECT * FROM outbox WHERE outbox_id = ?", (row["outbox_id"],)).fetchone()

    def finish_outbox(self, outbox_id: str, claim_token: str, status: str, *, error: str | None = None) -> bool:
        if status not in {"SUCCEEDED", "FAILED", "UNKNOWN", "RECONCILING"}:
            raise ValueError("unsupported outbox terminal status")
        result = self.connection.execute(
            "UPDATE outbox SET status = ?, claim_token = NULL, lease_until = NULL, last_error = ? "
            "WHERE outbox_id = ? AND claim_token = ? AND status = 'CLAIMED'",
            (status, error, outbox_id, claim_token),
        )
        self.connection.commit()
        return result.rowcount == 1

    def recoverable_outbox(self, *, now: float | None = None) -> list[sqlite3.Row]:
        """Return READY or expired CLAIMED rows for a worker inspection pass."""
        now = time.time() if now is None else float(now)
        return self.connection.execute(
            "SELECT * FROM outbox WHERE status = 'READY' OR (status = 'CLAIMED' AND "
            "(lease_until IS NULL OR lease_until <= ?)) ORDER BY created_at, rowid",
            (now,),
        ).fetchall()

    def update_outbox(self, outbox_id: str, status: str, data: dict[str, Any]) -> None:
        self.connection.execute("UPDATE outbox SET status = ?, data_json = ? WHERE outbox_id = ?", (status, self._json(data), outbox_id))
        self.connection.commit()

    def update_outbox_status(self, outbox_id: str, status: str, *, error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE outbox SET status = ?, claim_token = NULL, lease_until = NULL, last_error = ? WHERE outbox_id = ?",
            (status, error, outbox_id),
        )
        self.connection.commit()

    def save_tool_call(self, call: Any) -> bool:
        try:
            data = asdict(call)
            if hasattr(data.get("status"), "value"):
                data["status"] = data["status"].value
            self.connection.execute(
                "INSERT INTO tool_calls(tool_call_id, write_idempotency_key, status, data_json) VALUES (?, ?, ?, ?)",
                (call.tool_call_id, call.write_idempotency_key, str(call.status), self._json(data)),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def save_tool_call_and_outbox(self, call: Any, outbox_id: str, outbox_status: str, outbox_data: dict[str, Any]) -> bool:
        data = asdict(call)
        if hasattr(data.get("status"), "value"):
            data["status"] = data["status"].value
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "INSERT INTO tool_calls(tool_call_id, write_idempotency_key, status, data_json) VALUES (?, ?, ?, ?)",
                    (call.tool_call_id, call.write_idempotency_key, str(call.status), self._json(data)),
                )
                self.connection.execute(
                    "INSERT INTO outbox(outbox_id, write_idempotency_key, status, data_json) VALUES (?, ?, ?, ?)",
                    (outbox_id, call.write_idempotency_key, outbox_status, self._json(outbox_data)),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            return True
        except sqlite3.IntegrityError:
            return False

    def get_tool_call(self, tool_call_id: str) -> dict[str, Any] | None:
        return self._decode(self.connection.execute("SELECT data_json FROM tool_calls WHERE tool_call_id = ?", (tool_call_id,)).fetchone())

    def get_tool_call_by_key(self, write_key: str) -> dict[str, Any] | None:
        return self._decode(self.connection.execute("SELECT data_json FROM tool_calls WHERE write_idempotency_key = ?", (write_key,)).fetchone())

    def list_tool_calls(self, task_run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM tool_calls WHERE json_extract(data_json, '$.task_run_id') = ? ORDER BY rowid",
            (task_run_id,),
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def update_tool_call(self, call: Any) -> None:
        data = asdict(call)
        if hasattr(data.get("status"), "value"):
            data["status"] = data["status"].value
        self.connection.execute(
            "UPDATE tool_calls SET status = ?, data_json = ? WHERE tool_call_id = ?",
            (str(call.status), self._json(data), call.tool_call_id),
        )
        self.connection.commit()

    def reconcile_tool_call_result(self, tool_call_id: str, remote_status: str,
                                   remote_ref: str | None, error_code: str | None) -> dict[str, Any]:
        """Commit call + outbox together; late failed lookups cannot undo success."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            call = self.get_tool_call(tool_call_id)
            if call is None:
                raise KeyError(tool_call_id)
            outbox = self.get_outbox_for_tool_call(tool_call_id)
            busy = outbox is not None and outbox["status"] == "CLAIMED" and (outbox["lease_until"] or 0) > time.time()
            if busy or call["status"] not in {"DISPATCHED", "UNKNOWN", "RECONCILING"}:
                return call
            if remote_status == "SUCCEEDED" and not remote_ref:
                raise ValueError("successful reconciliation requires remote reference")
            call["status"] = remote_status if remote_status in {"SUCCEEDED", "FAILED"} else "RECONCILING"
            if remote_status == "SUCCEEDED":
                call["remote_ref"] = remote_ref
            call["error_code"] = error_code
            self.connection.execute(
                "UPDATE tool_calls SET status = ?, data_json = ? WHERE tool_call_id = ?",
                (call["status"], self._json(call), tool_call_id),
            )
            if outbox is not None:
                self.connection.execute(
                    "UPDATE outbox SET status = ?, claim_token = NULL, lease_until = NULL, last_error = ? WHERE outbox_id = ?",
                    (call["status"], error_code, outbox["outbox_id"]),
                )
            return call

    def mark_tool_call_dispatched(self, tool_call_id: str) -> bool:
        """Atomically cross the local dispatch fence exactly once."""
        payload = self.get_tool_call(tool_call_id)
        if payload is None:
            raise KeyError(tool_call_id)
        # The JSON snapshot is the source of truth for the domain object.
        payload = dict(payload)
        payload["status"] = "DISPATCHED"
        payload["attempts"] = int(payload.get("attempts", 0)) + 1
        result = self.connection.execute(
            "UPDATE tool_calls SET status = 'DISPATCHED', data_json = ? "
            "WHERE tool_call_id = ? AND status = 'PREPARED'",
            (self._json(payload), tool_call_id),
        )
        self.connection.commit()
        return result.rowcount == 1

    def save_audit(self, event: Any) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) VALUES (?, ?, ?, ?, ?)",
            (event.audit_id, event.event_type, event.tenant_id, event.task_run_id, self._json(asdict(event))),
        )
        self.connection.commit()

    @contextmanager
    def teaching_transaction(self):
        """Keep a teaching state change and its audit record atomic."""
        with self._owner_decision_lock:
            connection = self.connection
            if self.path != ":memory:":
                connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                if connection is not self.connection:
                    connection.close()

    def teaching_audit_in_transaction(self, connection: sqlite3.Connection, event: Any) -> None:
        connection.execute(
            "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.audit_id, event.event_type, event.tenant_id, event.task_run_id, self._json(asdict(event))),
        )

    def get_teaching_course(self, tenant_id: str, course_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM teaching_courses WHERE tenant_id = ? AND course_id = ?",
            (tenant_id, course_id),
        ).fetchone()

    def get_teaching_member(self, tenant_id: str, course_id: str, actor_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM teaching_course_members WHERE tenant_id = ? AND course_id = ? AND actor_id = ?",
            (tenant_id, course_id, actor_id),
        ).fetchone()

    def list_teaching_courses(self, tenant_id: str, actor_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT c.*, m.role FROM teaching_courses c JOIN teaching_course_members m "
            "ON m.tenant_id = c.tenant_id AND m.course_id = c.course_id "
            "WHERE c.tenant_id = ? AND m.actor_id = ? AND m.active = 1 ORDER BY c.course_id",
            (tenant_id, actor_id),
        ).fetchall()

    def list_teaching_members(self, tenant_id: str, course_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT actor_id, role FROM teaching_course_members "
            "WHERE tenant_id = ? AND course_id = ? AND active = 1 ORDER BY role, actor_id",
            (tenant_id, course_id),
        ).fetchall()

    def get_teaching_source(self, tenant_id: str, course_id: str, source_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM teaching_sources WHERE tenant_id = ? AND course_id = ? AND source_id = ?",
            (tenant_id, course_id, source_id),
        ).fetchone()

    def list_teaching_sources(self, tenant_id: str, course_id: str, actor_id: str,
                              *, lead: bool) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT s.source_id, s.kind, s.revision_id, s.block_count, s.submitted_by, s.created_at "
            "FROM teaching_sources AS s WHERE s.tenant_id = ? AND s.course_id = ? "
            "AND (? = 1 OR s.submitted_by = ? OR EXISTS ("
            "SELECT 1 FROM teaching_source_readers AS r WHERE r.tenant_id = s.tenant_id "
            "AND r.course_id = s.course_id AND r.source_id = s.source_id "
            "AND r.actor_id = ? AND r.active = 1)) ORDER BY s.created_at, s.source_id",
            (tenant_id, course_id, int(lead), actor_id, actor_id),
        ).fetchall()

    def get_teaching_source_reader(self, tenant_id: str, course_id: str, source_id: str,
                                   actor_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT active FROM teaching_source_readers WHERE tenant_id = ? AND course_id = ? "
            "AND source_id = ? AND actor_id = ?",
            (tenant_id, course_id, source_id, actor_id),
        ).fetchone()

    def list_teaching_source_readers(self, tenant_id: str, course_id: str,
                                     source_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT actor_id, granted_by, updated_at FROM teaching_source_readers "
            "WHERE tenant_id = ? AND course_id = ? AND source_id = ? AND active = 1 ORDER BY actor_id",
            (tenant_id, course_id, source_id),
        ).fetchall()

    def get_teaching_outline(self, tenant_id: str, course_id: str, source_id: str,
                             outline_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM teaching_outlines WHERE tenant_id = ? AND course_id = ? "
            "AND source_id = ? AND outline_id = ?",
            (tenant_id, course_id, source_id, outline_id),
        ).fetchone()

    def list_teaching_outlines(self, tenant_id: str, course_id: str, source_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT outline_id, source_revision, outline_hash, created_by, created_at "
            "FROM teaching_outlines WHERE tenant_id = ? AND course_id = ? AND source_id = ? "
            "ORDER BY created_at, outline_id",
            (tenant_id, course_id, source_id),
        ).fetchall()

    def list_audits(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT data_json FROM audit_events ORDER BY rowid").fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def list_tenant_audits(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return newest audit rows for one tenant, including database timestamps."""
        bounded_limit = max(1, min(int(limit), 200))
        rows = self.connection.execute(
            "SELECT data_json, created_at FROM audit_events "
            "WHERE tenant_id = ? ORDER BY rowid DESC LIMIT ?",
            (tenant_id, bounded_limit),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = json.loads(row["data_json"])
            item["created_at"] = row["created_at"]
            items.append(item)
        return items

    def audit_operations_summary(self, tenant_id: str) -> dict[str, dict[str, int]]:
        """Count tenant incidents and tool results without exposing event contents."""
        incident_rows = self.connection.execute(
            "SELECT COALESCE(triage.status, 'OPEN') AS status, COUNT(*) AS total "
            "FROM audit_events AS event LEFT JOIN audit_triage AS triage "
            "ON triage.audit_id = event.audit_id AND triage.tenant_id = event.tenant_id "
            "WHERE event.tenant_id = ? AND event.event_type != 'AUDIT_TRIAGE_UPDATED' "
            "AND json_extract(event.data_json, '$.outcome') IN "
            "('ERROR', 'FAILED', 'UNKNOWN', 'BLOCKED', 'DENIED', 'INVALIDATED', 'REJECTED', 'STALE') "
            "GROUP BY COALESCE(triage.status, 'OPEN')", (tenant_id,),
        ).fetchall()
        tool_rows = self.connection.execute(
            "SELECT call.status AS status, COUNT(*) AS total FROM tool_calls AS call "
            "JOIN task_runs AS task ON task.task_run_id = json_extract(call.data_json, '$.task_run_id') "
            "WHERE task.tenant_id = ? GROUP BY call.status", (tenant_id,),
        ).fetchall()
        return {
            "incidents": {row["status"]: row["total"] for row in incident_rows},
            "tool_calls": {row["status"]: row["total"] for row in tool_rows},
        }

    def get_audit_triage(self, tenant_id: str, audit_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT status, decided_by, resolution, updated_at FROM audit_triage "
            "WHERE tenant_id = ? AND audit_id = ?", (tenant_id, audit_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_audit_triage(self, tenant_id: str, audit_id: str, decided_by: str,
                         expected_status: str, status: str, resolution: str | None,
                         audit_event: Any) -> str:
        """Change triage state once; a stale browser cannot overwrite another administrator."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            audit = self.connection.execute(
                "SELECT 1 FROM audit_events WHERE tenant_id = ? AND audit_id = ?",
                (tenant_id, audit_id),
            ).fetchone()
            if audit is None:
                self.connection.rollback()
                return "NOT_FOUND"
            current = self.get_audit_triage(tenant_id, audit_id)
            current_status = current["status"] if current is not None else "OPEN"
            if current_status != expected_status:
                self.connection.rollback()
                return "STALE"
            if status == "ACKNOWLEDGED" and current_status == "OPEN":
                self.connection.execute(
                    "INSERT INTO audit_triage(audit_id, tenant_id, status, decided_by, resolution) "
                    "VALUES (?, ?, ?, ?, NULL)", (audit_id, tenant_id, status, decided_by),
                )
            elif status == "RESOLVED" and current_status == "ACKNOWLEDGED":
                self.connection.execute(
                    "UPDATE audit_triage SET status = ?, decided_by = ?, resolution = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE tenant_id = ? AND audit_id = ?",
                    (status, decided_by, resolution, tenant_id, audit_id),
                )
            else:
                self.connection.rollback()
                return "INVALID_TRANSITION"
            self.connection.execute(
                "INSERT INTO audit_events(audit_id, event_type, tenant_id, task_run_id, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (audit_event.audit_id, audit_event.event_type, tenant_id, audit_event.task_run_id,
                 self._json(asdict(audit_event))),
            )
            self.connection.commit()
            return status
        except Exception:
            self.connection.rollback()
            raise
