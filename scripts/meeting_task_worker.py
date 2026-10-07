"""Run a bounded real Feishu task worker pass.

The worker is deliberately single-pass: process supervision and scheduling are
deployment concerns, while the control plane owns durable claims and fences.
The default limit is one so an operator must explicitly choose a larger batch.
No credential or raw Feishu open_id is printed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import json
import signal
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane import ControlPlane, FeishuTaskGateway, MeetingOutboxWorker, SQLiteStore  # noqa: E402


def short_hash(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3"))
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument(
        "--assignee-actor-id",
        default=os.environ.get("FDE_TASK_ASSIGNEE_ACTOR_ID", "member-primary"),
    )
    parser.add_argument(
        "--assignee-open-id-file",
        default=os.environ.get(
            "FDE_TASK_ASSIGNEE_OPEN_ID_FILE",
            "/var/lib/fde-agent/approval_recipient_open_id",
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="report recoverable rows without calling Feishu")
    parser.add_argument("--loop", action="store_true", help="supervised read-only polling; requires --dry-run")
    parser.add_argument("--poll-interval", type=float, default=30)
    parser.add_argument("--heartbeat-file", type=Path, default=Path("/tmp/fde-worker-heartbeat"))
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.limit > 1 and os.environ.get("FDE_WORKER_ALLOW_BATCH") != "1":
        parser.error("batch execution requires FDE_WORKER_ALLOW_BATCH=1")
    if args.loop and not args.dry_run:
        parser.error("continuous writes are not enabled; --loop requires --dry-run")
    if not 5 <= args.poll_interval <= 3600:
        parser.error("--poll-interval must be between 5 and 3600 seconds")
    return args


def inspect_queue(db_path: Path) -> dict:
    """No migrations or claims during observation, even after a restart."""
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        statuses = dict(conn.execute("SELECT status, COUNT(*) FROM tool_calls GROUP BY status"))
        recoverable = conn.execute(
            "SELECT COUNT(*) FROM outbox o JOIN tool_calls t ON o.write_idempotency_key = t.write_idempotency_key "
            "WHERE t.status = 'PREPARED' AND (o.status = 'READY' OR "
            "(o.status = 'CLAIMED' AND (o.lease_until IS NULL OR o.lease_until <= ?)))", (time.time(),),
        ).fetchone()[0]
    return {"recoverable_count": recoverable, "uncertain_count": sum(statuses.get(s, 0) for s in ("UNKNOWN", "RECONCILING", "DISPATCHED"))}


def observe(args, stop: threading.Event) -> int:
    previous = None
    while not stop.is_set():
        state = inspect_queue(Path(args.db))
        if state != previous:
            print("WORKER_OBSERVE " + json.dumps({**state, "remote_write": 0}), flush=True)
            previous = state
        # Heartbeat means a successful DB inspection, not a Feishu connection.
        temporary = args.heartbeat_file.with_suffix(".tmp")
        temporary.write_text(str(time.time()), encoding="ascii")
        temporary.replace(args.heartbeat_file)
        stop.wait(args.poll_interval)
    print("WORKER_STOPPED remote_write=0", flush=True)
    return 0


def run_once(args) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        print("WORKER_ERROR type=DatabaseMissing", file=sys.stderr)
        return 2

    if args.dry_run:
        print("WORKER_PLAN " + json.dumps({**inspect_queue(db_path), "limit": args.limit, "dry_run": 1}), flush=True)
        return 0

    with SQLiteStore(db_path) as store:
        control_plane = ControlPlane(store)
        rows = control_plane.store.recoverable_outbox()[: args.limit]
        print(f"WORKER_PLAN recoverable_count={len(rows)} limit={args.limit} dry_run={int(args.dry_run)}", flush=True)
        if args.dry_run:
            for row in rows:
                print(f"WORKER_ITEM outbox_hash={short_hash(row['outbox_id'])} status={row['status']}", flush=True)
            return 0
        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            print("WORKER_ERROR type=MissingFeishuCredentials", file=sys.stderr)
            return 2
        try:
            open_id = Path(args.assignee_open_id_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"WORKER_ERROR type={type(exc).__name__} field=assignee_open_id_file", file=sys.stderr)
            return 2
        if not open_id:
            print("WORKER_ERROR type=EmptyAssigneeOpenId", file=sys.stderr)
            return 2
        gateway = FeishuTaskGateway(
            app_id=app_id,
            app_secret=app_secret,
            actor_open_ids={args.assignee_actor_id: open_id},
        )
        results = MeetingOutboxWorker(control_plane, gateway).run_once(limit=args.limit)
        for result in results:
            print(
                "WORKER_RESULT "
                f"tool_call_hash={short_hash(result.tool_call_id)} "
                f"status={result.status} "
                f"remote_id_hash={short_hash(result.remote_task_id) if result.remote_task_id else 'none'} "
                f"notification={result.notification_status} "
                f"warning={result.warning or 'none'}",
                flush=True,
            )
        return 0 if all(result.status in {"SUCCEEDED", "FAILED", "UNKNOWN", "RECONCILING"} for result in results) else 1


def main() -> int:
    args = parse_args()
    try:
        if not args.loop:
            return run_once(args)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        print("WORKER_STARTED mode=observe remote_write=0", flush=True)
        return observe(args, stop)
    except Exception as exc:
        print(f"WORKER_ERROR type={type(exc).__name__}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
