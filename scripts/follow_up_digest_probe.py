"""Preview a daily follow-up digest without sending anything.

The input can be a sanitized JSON list of observations or a control-plane
SQLite database.  This is a read-only preview: it prints counts and hashes of
task-run references, never task text, member IDs, remote IDs, credentials, or
messages, and always reports ``REMOTE_WRITE=0`` and ``NOTIFICATION_SENT=0``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane.follow_up import build_daily_follow_up_digest


def short_hash(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def load_observations_from_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("observations")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("observations fixture must be a JSON list")
    return data


def load_observations_from_db(path: Path, tenant_id: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError("database missing")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT data_json FROM follow_up_observations WHERE tenant_id = ? ORDER BY rowid",
            (tenant_id,),
        ).fetchall()
    observations: list[dict[str, Any]] = []
    for (raw,) in rows:
        data = json.loads(raw)
        if isinstance(data, dict):
            observations.append(data)
    return observations


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, help="sanitized observation JSON list")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3")))
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--recipient-actor-id", required=True)
    parser.add_argument("--business-date", default=date.today().isoformat())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        observations = load_observations_from_json(args.fixture) if args.fixture else load_observations_from_db(args.db, args.tenant_id)
        digest = build_daily_follow_up_digest(
            tenant_id=args.tenant_id,
            business_date=args.business_date,
            observations=observations,
            recipient_actor_id=args.recipient_actor_id,
        )
        print(f"FOLLOW_UP_DIGEST business_date={digest.business_date}")
        print(f"FOLLOW_UP_OBSERVATION_COUNT={len(observations)}")
        for status, count in digest.counts.items():
            if count:
                print(f"FOLLOW_UP_COUNT status={status} count={count}")
        print(f"FOLLOW_UP_ATTENTION_COUNT={len(digest.attention_task_run_ids)}")
        print("FOLLOW_UP_ATTENTION_TASK_HASHES=" + ",".join(short_hash(value) for value in digest.attention_task_run_ids))
        print(f"FOLLOW_UP_NOTIFICATION_KEY_HASH={short_hash(digest.notification_key)}")
        print("REMOTE_WRITE=0")
        print("NOTIFICATION_SENT=0")
        return 0
    except Exception:
        print("FOLLOW_UP_DIGEST_ERROR reason=PREVIEW_FAILED REMOTE_WRITE=0 NOTIFICATION_SENT=0", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
