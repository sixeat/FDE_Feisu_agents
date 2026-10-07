"""SQLite durability fixture for approval and outbox idempotency.

This is a phase-0 verification helper. It uses a temporary SQLite file and
does not call Feishu or any external service.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


SCHEMA = """
CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY,
  task_run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  action_version INTEGER NOT NULL,
  proposal_hash TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE event_inbox (
  source TEXT NOT NULL,
  event_id TEXT NOT NULL,
  approval_id TEXT NOT NULL,
  result TEXT NOT NULL,
  PRIMARY KEY (source, event_id)
);
CREATE TABLE outbox (
  write_key TEXT PRIMARY KEY,
  approval_id TEXT NOT NULL,
  status TEXT NOT NULL
);
"""


def handle(conn: sqlite3.Connection, event_id: str, approval_id: str, version: int, proposal_hash: str) -> str:
    conn.execute("BEGIN IMMEDIATE")
    try:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO event_inbox(source, event_id, approval_id, result) VALUES (?, ?, ?, ?)",
            ("feishu", event_id, approval_id, "RECEIVED"),
        ).rowcount
        if inserted == 0:
            conn.rollback()
            return "DUPLICATE_EVENT"

        row = conn.execute(
            "SELECT task_run_id, step_id, action_version, proposal_hash, status FROM approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "UPDATE event_inbox SET result = ? WHERE source = ? AND event_id = ?",
                ("UNKNOWN_APPROVAL", "feishu", event_id),
            )
            conn.commit()
            return "UNKNOWN_APPROVAL"

        task_run_id, step_id, expected_version, expected_hash, status = row
        if status != "PENDING":
            result = "ALREADY_DECIDED"
        elif version != expected_version or proposal_hash != expected_hash:
            conn.execute(
                "UPDATE approvals SET status = 'INVALIDATED' WHERE approval_id = ?",
                (approval_id,),
            )
            result = "STALE_APPROVAL"
        else:
            conn.execute(
                "UPDATE approvals SET status = 'APPROVED' WHERE approval_id = ?",
                (approval_id,),
            )
            write_key = f"{task_run_id}:{step_id}:target:{expected_version}"
            conn.execute(
                "INSERT OR IGNORE INTO outbox(write_key, approval_id, status) VALUES (?, ?, ?)",
                (write_key, approval_id, "PREPARED"),
            )
            result = "APPROVED"

        conn.execute(
            "UPDATE event_inbox SET result = ? WHERE source = ? AND event_id = ?",
            (result, "feishu", event_id),
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fde-approval-") as directory:
        db_path = Path(directory) / "approval.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?)",
            ("approval-001", "run-001", "step-001", 1, "hash-v1", "PENDING"),
        )
        conn.commit()

        first = handle(conn, "event-001", "approval-001", 1, "hash-v1")
        conn.close()

        reopened = sqlite3.connect(db_path)
        after_restart = handle(reopened, "event-002", "approval-001", 1, "hash-v1")
        same_event = handle(reopened, "event-001", "approval-001", 1, "hash-v1")
        outbox_count = reopened.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        status = reopened.execute(
            "SELECT status FROM approvals WHERE approval_id = ?", ("approval-001",)
        ).fetchone()[0]
        reopened.close()

    assert first == "APPROVED"
    assert after_restart == "ALREADY_DECIDED"
    assert same_event == "DUPLICATE_EVENT"
    assert outbox_count == 1
    assert status == "APPROVED"

    print("APPROVAL_SQLITE_FIXTURE_PASS")
    print(f"first={first}")
    print(f"after_restart={after_restart}")
    print(f"same_event={same_event}")
    print(f"approval_status={status}")
    print(f"outbox_count={outbox_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
