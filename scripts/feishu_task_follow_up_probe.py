"""Read one Feishu task and map it to the local follow-up status.

This probe is deliberately read-only.  It is a narrow verification tool for
the personal-assistant follow-up contract: it fetches one existing task,
extracts only stable fields, and prints hashes/statuses instead of task text,
member IDs, credentials, or the raw remote task ID.

Offline verification can use ``--fixture`` with a sanitized JSON response,
so mapping changes can be tested without contacting the tenant.  Online mode
requires ``FEISHU_TASK_REMOTE_ID`` (or ``--remote-id``) and app credentials.
Neither mode creates, updates, deletes, assigns, or notifies a task.
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

from fde_control_plane.follow_up import RemoteTaskSnapshot, extract_remote_task_snapshot


def short_hash(value: object) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("fixture must be a JSON object")
    return data


def load_remote_id_from_db(
    path: Path, call_id: str | None = None, *, select_latest: bool = False
) -> str:
    """Read one persisted successful remote reference through a read-only DB.

    The database stores the actual reference inside ``tool_calls.data_json``;
    this helper keeps it inside the process and never prints it.  A call ID is
    optional because the server-side operator may deliberately avoid exposing
    internal IDs to a user.  Only a unique, successful reference is accepted.
    """
    if not path.is_file():
        raise ValueError("database missing")
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        if call_id:
            rows = connection.execute(
                "SELECT data_json FROM tool_calls WHERE tool_call_id = ? AND status = 'SUCCEEDED'",
                (call_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT data_json FROM tool_calls WHERE status = 'SUCCEEDED' ORDER BY rowid DESC LIMIT 2"
            ).fetchall()
    references: list[str] = []
    for (raw,) in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        remote_id = data.get("remote_ref") if isinstance(data, dict) else None
        if isinstance(remote_id, str) and remote_id:
            references.append(remote_id)
    if not references:
        raise ValueError("successful remote reference missing")
    if not call_id and select_latest:
        return references[0]
    if not call_id and len(set(references)) != 1:
        raise ValueError("latest remote references are ambiguous")
    return references[0]


def snapshot_from_fixture(data: dict[str, Any]) -> tuple[int, RemoteTaskSnapshot]:
    """Read a sanitized response fixture.

    Accepted shapes are either ``{"code": 0, "data": {"task": {...}}}``
    (the SDK response shape) or a direct task object.  A fixture may include
    ``code``/``msg`` for response simulation; ``msg`` is never printed.
    """
    code = data.get("code", 0)
    if not isinstance(code, int):
        raise ValueError("fixture code must be an integer")
    if code != 0:
        return code, RemoteTaskSnapshot(None, extract_remote_task_snapshot(None).mapping)
    candidate: Any = data
    response_data = data.get("data")
    if isinstance(response_data, dict) and "task" in response_data:
        candidate = response_data.get("task")
    elif "task" in data:
        candidate = data.get("task")
    if not isinstance(candidate, dict):
        candidate = None
    return code, extract_remote_task_snapshot(candidate)


def print_result(code: int, snapshot: RemoteTaskSnapshot, *, remote_write: int = 0) -> None:
    mapping = snapshot.mapping
    print(f"TASK_FOLLOW_UP_READ code={code}")
    print(f"TASK_REMOTE_ID_HASH={short_hash(snapshot.remote_task_id)}")
    print(f"TASK_REMOTE_STATUS={mapping.raw_status or 'MISSING'}")
    print(f"TASK_DUE_AT={mapping.due_at or 'NONE'}")
    print(f"TASK_STATUS_MAPPING={mapping.status.value}")
    print(f"TASK_MAPPING_REASON={mapping.reason or 'NONE'}")
    print(f"REMOTE_WRITE={remote_write}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, help="sanitized JSON response; skips network and credentials")
    parser.add_argument("--remote-id", default=os.environ.get("FEISHU_TASK_REMOTE_ID"))
    parser.add_argument(
        "--from-db", type=Path,
        help="read one SUCCEEDED remote reference from this control-plane SQLite database (read-only)",
    )
    parser.add_argument("--call-id", help="optional internal tool-call ID used with --from-db")
    parser.add_argument(
        "--latest", action="store_true",
        help="with --from-db, explicitly select the newest successful reference when several exist",
    )
    parser.add_argument(
        "--db", type=Path,
        default=Path(os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3")),
        help="reserved for compatibility; use --from-db to opt into DB lookup",
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=(Path(os.environ["FEISHU_TASK_CREDENTIALS_FILE"])
                  if os.environ.get("FEISHU_TASK_CREDENTIALS_FILE") else None),
        help="local JSON with app_id/app_secret; optional when environment variables are set",
    )
    return parser.parse_args(argv)


def run_online(remote_id: str, credentials_file: Path | None) -> tuple[int, RemoteTaskSnapshot]:
    config: dict[str, Any] = {}
    if credentials_file is not None:
        config = load_json(credentials_file)
    app_id = config.get("app_id") or os.environ.get("FEISHU_APP_ID")
    app_secret = config.get("app_secret") or os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise ValueError("missing credentials")

    import lark_oapi as lark

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).timeout(12).log_level(lark.LogLevel.ERROR).build()
    request = lark.api.task.v2.model.GetTaskRequest.builder().task_guid(remote_id).build()
    response = client.task.v2.task.get(request)
    code = getattr(response, "code", None)
    if not isinstance(code, int):
        raise RuntimeError("invalid response code")
    if code != 0:
        return code, extract_remote_task_snapshot(None)
    task = getattr(getattr(response, "data", None), "task", None)
    return code, extract_remote_task_snapshot(task)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.fixture:
            code, snapshot = snapshot_from_fixture(load_json(args.fixture))
        else:
            remote_id = args.remote_id
            if args.from_db:
                if remote_id:
                    raise ValueError("remote id and database lookup are mutually exclusive")
                remote_id = load_remote_id_from_db(args.from_db, args.call_id, select_latest=args.latest)
            if not remote_id:
                print("TASK_FOLLOW_UP_ERROR reason=MISSING_REMOTE_ID REMOTE_WRITE=0", file=sys.stderr)
                return 2
            code, snapshot = run_online(str(remote_id), args.credentials_file)
        print_result(code, snapshot)
        # A successful read is useful even when the mapping is UNKNOWN; the
        # caller must see that state and decide whether to review it.
        return 0 if code == 0 else 1
    except Exception:
        # Never expose SDK exception text, task IDs, or credential material.
        print("TASK_FOLLOW_UP_ERROR reason=READ_FAILED REMOTE_WRITE=0", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
