"""SQLite-only deployment verification and online backup; no SDK imports."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path


def inspect_database(db_path: Path, backup_path: Path | None = None) -> dict:
    if not db_path.is_file():
        raise ValueError("database does not exist")
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as source:
        source.execute("PRAGMA busy_timeout = 5000")
        if backup_path is not None:
            if db_path.resolve() == backup_path.resolve():
                raise ValueError("backup must be a separate file")
            # Never overwrite an earlier recovery checkpoint.
            fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with sqlite3.connect(backup_path) as destination:
                source.backup(destination, pages=128, sleep=0.05)
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for table in ("actors", "web_sessions", "meeting_records", "meeting_sources", "meeting_todo_drafts",
                      "approvals", "approval_deliveries", "approval_delivery_attempts", "tool_calls", "outbox"):
            counts[table] = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in tables else None
        statuses = {}
        if "task_runs" in tables:
            statuses = dict(source.execute(
                "SELECT json_extract(data_json, '$.status'), COUNT(*) FROM task_runs GROUP BY 1"
            ))
        return {"integrity": source.execute("PRAGMA quick_check").fetchone()[0],
                "backup_created": backup_path is not None, "counts": counts, "task_status_counts": statuses}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_database(args.db, args.backup), sort_keys=True))


if __name__ == "__main__":
    main()
