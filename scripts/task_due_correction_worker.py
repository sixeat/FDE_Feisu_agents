"""Run one approved task-deadline correction or read-only reconciliation.

Default mode only prints the selected correction state. Remote PATCH requires
both an organizer approval stored in SQLite and the explicit --execute flag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane import ControlPlane, FeishuTaskGateway, SQLiteStore  # noqa: E402
from fde_control_plane.task_due_correction import TaskDueCorrectionWorker, get_correction  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3")))
    parser.add_argument("--correction-id", required=True)
    parser.add_argument("--credentials-file", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--reconcile", action="store_true")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print("DUE_CORRECTION_ERROR reason=DATABASE_MISSING", file=sys.stderr)
        return 2
    with SQLiteStore(args.db) as store:
        correction = get_correction(store, args.correction_id)
        if correction is None:
            print("DUE_CORRECTION_ERROR reason=NOT_FOUND", file=sys.stderr)
            return 2
        reference = hashlib.sha256(correction["correction_id"].encode()).hexdigest()[:12]
        print(f"DUE_CORRECTION_PLAN ref_hash={reference} status={correction['status']} "
              f"approved_due={correction['approved_due_date']} observed_due={str(correction['observed_due_at'])[:10]} "
              f"remote_write={int(args.execute)}", flush=True)
        if not args.execute and not args.reconcile:
            return 0
        config = json.loads(args.credentials_file.read_text(encoding="utf-8-sig")) if args.credentials_file else {}
        app_id = config.get("app_id") or os.environ.get("FEISHU_APP_ID")
        app_secret = config.get("app_secret") or os.environ.get("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            print("DUE_CORRECTION_ERROR reason=CREDENTIALS_MISSING", file=sys.stderr)
            return 2
        worker = TaskDueCorrectionWorker(ControlPlane(store), FeishuTaskGateway(app_id=app_id, app_secret=app_secret))
        try:
            result = worker.execute(args.correction_id) if args.execute else worker.reconcile(args.correction_id)
        except (ValueError, RuntimeError):
            print("DUE_CORRECTION_ERROR reason=STATE_BLOCKED", file=sys.stderr)
            return 2
        print(f"DUE_CORRECTION_RESULT ref_hash={reference} status={result['status']} "
              f"remote_write={int(args.execute)}", flush=True)
        return 0 if result["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
