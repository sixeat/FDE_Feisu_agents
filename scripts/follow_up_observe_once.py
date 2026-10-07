"""Perform manual, read-only Feishu task observations.

The script selects successful remote task references already stored by the
control plane, calls Task v2 GET, and writes only normalized observations to
local SQLite. It never creates, edits, deletes, assigns, or notifies a Feishu
task. Use ``--latest`` for one task or ``--all`` for an explicitly bounded
batch; omitting both is an error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane import (
    SQLiteStore,
    build_remote_task_observation,
    extract_remote_task_snapshot,
    persist_remote_task_observation,
)


def short_hash(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def select_successful_tasks(path: Path, *, limit: int | None = None) -> list[tuple[str, str, str]]:
    """Return unique remote ID, TaskRun ID and tenant tuples from a read-only view."""
    if not path.is_file():
        raise ValueError("database missing")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT data_json FROM tool_calls WHERE status = 'SUCCEEDED' ORDER BY rowid DESC"
        ).fetchall()
        selected: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for (raw,) in rows:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            remote_id = data.get("remote_ref") if isinstance(data, dict) else None
            task_run_id = data.get("task_run_id") if isinstance(data, dict) else None
            if not isinstance(remote_id, str) or not remote_id or not isinstance(task_run_id, str) or not task_run_id:
                continue
            task_row = connection.execute(
                "SELECT tenant_id FROM task_runs WHERE task_run_id = ?", (task_run_id,)
            ).fetchone()
            if task_row is None or not isinstance(task_row[0], str) or not task_row[0]:
                continue
            item = (remote_id, task_run_id, task_row[0])
            if item in seen:
                continue
            seen.add(item)
            selected.append(item)
            if limit is not None and len(selected) >= limit:
                break
    if not selected:
        raise ValueError("successful remote reference missing")
    return selected


def select_latest_successful_task(path: Path) -> tuple[str, str, str]:
    """Return the newest remote ID, TaskRun ID and tenant."""
    return select_successful_tasks(path, limit=1)[0]


def read_task(remote_id: str, credentials_file: Path | None) -> tuple[int, Any]:
    config: dict[str, Any] = {}
    if credentials_file is not None:
        config = json.loads(credentials_file.read_text(encoding="utf-8-sig"))
    app_id = config.get("app_id") or os.environ.get("FEISHU_APP_ID")
    app_secret = config.get("app_secret") or os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise ValueError("missing credentials")
    import lark_oapi as lark

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).timeout(12).log_level(lark.LogLevel.ERROR).build()
    request = lark.api.task.v2.model.GetTaskRequest.builder().task_guid(remote_id).build()
    response = client.task.v2.task.get(request)
    code = getattr(response, "code", None)
    task = getattr(getattr(response, "data", None), "task", None)
    return code if isinstance(code, int) else -1, task


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3")))
    parser.add_argument("--credentials-file", type=Path)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--latest", action="store_true", help="explicitly select the newest successful task reference")
    selection.add_argument("--all", action="store_true", help="explicitly observe all successful references")
    parser.add_argument("--limit", type=int, help="required maximum references for --all")
    parser.add_argument("--now", help="optional ISO date/datetime for deterministic due-date mapping")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.latest and not args.all:
        print("FOLLOW_UP_OBSERVE_ERROR reason=EXPLICIT_SELECTION_REQUIRED REMOTE_WRITE=0 NOTIFICATION_SENT=0", file=sys.stderr)
        return 2
    if args.all and args.limit is None:
        print("FOLLOW_UP_OBSERVE_ERROR reason=BATCH_LIMIT_REQUIRED REMOTE_WRITE=0 NOTIFICATION_SENT=0", file=sys.stderr)
        return 2
    if args.limit is not None and (not args.all or args.limit <= 0):
        print("FOLLOW_UP_OBSERVE_ERROR reason=INVALID_BATCH_LIMIT REMOTE_WRITE=0 NOTIFICATION_SENT=0", file=sys.stderr)
        return 2
    try:
        selected = select_successful_tasks(args.db, limit=1 if args.latest else args.limit)
        observations: list[tuple[Any, Any]] = []
        read_failures = 0
        with SQLiteStore(args.db) as store:
            for remote_id, task_run_id, tenant_id in selected:
                code, task = read_task(remote_id, args.credentials_file)
                if code != 0:
                    read_failures += 1
                    print(f"FOLLOW_UP_OBSERVE_ITEM code={code} TASK_REMOTE_ID_HASH={short_hash(remote_id)}")
                    continue
                snapshot = extract_remote_task_snapshot(task)
                observation = build_remote_task_observation(
                    tenant_id=tenant_id,
                    task_run_id=task_run_id,
                    snapshot=snapshot,
                    observed_at=args.now,
                )
                persist_remote_task_observation(store, observation)
                observations.append((snapshot, observation))
                print(
                    f"FOLLOW_UP_OBSERVE_ITEM code={code} "
                    f"TASK_REMOTE_ID_HASH={short_hash(snapshot.remote_task_id)} "
                    f"TASK_REMOTE_STATUS={snapshot.mapping.raw_status or 'MISSING'} "
                    f"TASK_STATUS_MAPPING={snapshot.mapping.status.value} "
                    f"TASK_MAPPING_REASON={snapshot.mapping.reason or 'NONE'}"
                )
        if not observations:
            print("FOLLOW_UP_OBSERVE_ERROR reason=NO_SUCCESSFUL_READ REMOTE_WRITE=0 NOTIFICATION_SENT=0", file=sys.stderr)
            return 1
        print(
            f"FOLLOW_UP_OBSERVE_READ code=0 OBSERVATION_COUNT={len(observations)} "
            f"READ_FAILURE_COUNT={read_failures}"
        )
        print("REMOTE_WRITE=0")
        print("NOTIFICATION_SENT=0")
        return 1 if read_failures else 0
    except Exception:
        print("FOLLOW_UP_OBSERVE_ERROR reason=READ_OR_PERSIST_FAILED REMOTE_WRITE=0 NOTIFICATION_SENT=0", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
