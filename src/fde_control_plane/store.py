"""Small durable SQLite store for the M1 control-plane kernel.

The store deliberately keeps persistence boring: JSON columns preserve the
domain objects while unique keys provide the idempotency fences needed by the
service layer. No runtime credential is accepted by this module as a special
field; callers pass already-sanitized payloads.
"""

from __future__ import annotations

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
            CREATE TABLE IF NOT EXISTS agent_versions (
                agent_id TEXT NOT NULL, version INTEGER NOT NULL,
                capabilities_json TEXT NOT NULL, active INTEGER NOT NULL,
                skill_version TEXT, PRIMARY KEY (agent_id, version)
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
        self.connection.execute(
            "INSERT OR REPLACE INTO agent_versions VALUES (?, ?, ?, ?, ?)",
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
                                   skill_version: str | None, approval_id: str) -> dict[str, Any]:
        """Atomically publish one prechecked draft as an immutable v1 AgentVersion."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_agent_config_approval(draft_id, tenant_id, input_hash)
            if existing is not None:
                self.connection.commit()
                return {"approval_id": existing["approval_id"], "agent_id": existing["agent_id"],
                        "version": int(existing["version"]), "status": existing["status"], "duplicate": True}
            draft = self.connection.execute(
                "SELECT status FROM agent_config_drafts WHERE draft_id = ? AND tenant_id = ?",
                (draft_id, tenant_id),
            ).fetchone()
            if draft is None:
                raise ValueError("DRAFT_NOT_FOUND")
            if draft["status"] == "PUBLISHED":
                raise ValueError("DRAFT_ALREADY_PUBLISHED")
            review = self.connection.execute(
                "SELECT review_id, input_hash, status FROM agent_config_reviews "
                "WHERE review_id = ? AND draft_id = ? AND tenant_id = ?",
                (review_id, draft_id, tenant_id),
            ).fetchone()
            if review is None or review["input_hash"] != input_hash:
                raise ValueError("PRECHECK_VERSION_STALE")
            if review["status"] != "READY_FOR_ADMIN":
                raise ValueError("PRECHECK_NOT_READY")
            existing_actor = self.connection.execute(
                "SELECT actor_id FROM actors WHERE tenant_id = ? AND actor_type IN "
                "('PERSONAL_ASSISTANT', 'BUSINESS_AGENT', 'MANAGEMENT_AGENT')",
                (tenant_id,),
            ).fetchall()
            if any(str(row["actor_id"]).casefold() == agent_id.casefold() for row in existing_actor):
                raise ValueError("AGENT_NAME_CONFLICT")
            self.connection.execute(
                "INSERT INTO actors(actor_id, tenant_id, actor_type, capabilities_json, roles_json, external_ref_hash, active) "
                "VALUES (?, ?, ?, ?, ?, NULL, 1)",
                (agent_id, tenant_id, actor_type, self._json(sorted(capabilities)), self._json([])),
            )
            self.connection.execute(
                "INSERT INTO agent_versions(agent_id, version, capabilities_json, active, skill_version) "
                "VALUES (?, 1, ?, 1, ?)",
                (agent_id, self._json(sorted(capabilities)), skill_version),
            )
            self.connection.execute(
                "UPDATE agent_config_drafts SET status = 'PUBLISHED' WHERE draft_id = ? AND tenant_id = ?",
                (draft_id, tenant_id),
            )
            self.connection.execute(
                "INSERT INTO agent_config_approvals(approval_id, draft_id, tenant_id, approver_id, review_id, "
                "input_hash, agent_id, version, status) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'PUBLISHED')",
                (approval_id, draft_id, tenant_id, approver_id, review_id, input_hash, agent_id),
            )
            self.connection.commit()
            return {"approval_id": approval_id, "agent_id": agent_id, "version": 1,
                    "status": "PUBLISHED", "duplicate": False}
        except Exception:
            self.connection.rollback()
            raise

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
            "SELECT actor_id FROM actors WHERE tenant_id = ? AND actor_type = 'USER' "
            "AND active = 1 AND external_ref_hash IS NOT NULL", (tenant_id,),
        ).fetchall()
        return [{"actor_id": row["actor_id"]} for row in rows]

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

    def list_audits(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT data_json FROM audit_events ORDER BY rowid").fetchall()
        return [json.loads(row["data_json"]) for row in rows]
