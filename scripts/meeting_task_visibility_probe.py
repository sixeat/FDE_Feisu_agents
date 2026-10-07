"""Read-only verification of persisted Feishu task references.

The probe never creates, updates, or deletes a task. It reads succeeded tool
calls from the control-plane database, fetches each persisted remote task, and
checks that the configured responsible member is present. Only hashes and
counts are printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def short_hash(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3"))
    parser.add_argument(
        "--assignee-open-id-file",
        default=os.environ.get(
            "FDE_TASK_ASSIGNEE_OPEN_ID_FILE",
            "/var/lib/fde-agent/approval_recipient_open_id",
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("VISIBILITY_ERROR type=MissingFeishuCredentials", file=sys.stderr)
        return 2
    db_path = Path(args.db)
    if not db_path.is_file():
        print("VISIBILITY_ERROR type=DatabaseMissing", file=sys.stderr)
        return 2
    try:
        expected_open_id = Path(args.assignee_open_id_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"VISIBILITY_ERROR type={type(exc).__name__} field=assignee_open_id_file", file=sys.stderr)
        return 2
    if not expected_open_id:
        print("VISIBILITY_ERROR type=EmptyAssigneeOpenId", file=sys.stderr)
        return 2

    import lark_oapi as lark

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT data_json FROM tool_calls WHERE status = 'SUCCEEDED' ORDER BY rowid"
        ).fetchall()
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    failures = 0
    print(f"VISIBILITY_PLAN succeeded_count={len(rows)}", flush=True)
    for row in rows:
        data = json.loads(row[0])
        remote_id = data.get("remote_ref")
        if not remote_id:
            failures += 1
            print("VISIBILITY_RESULT code=LOCAL_MISSING_REMOTE_ID visible=False", flush=True)
            continue
        response = client.task.v2.task.get(
            lark.api.task.v2.model.GetTaskRequest.builder().task_guid(remote_id).build()
        )
        task = getattr(getattr(response, "data", None), "task", None)
        members = getattr(task, "members", None) or []
        member_ids = {str(getattr(member, "id", "")) for member in members}
        visible = expected_open_id in member_ids
        if getattr(response, "code", None) != 0 or not visible:
            failures += 1
        print(
            f"VISIBILITY_RESULT code={getattr(response, 'code', None)} "
            f"remote_id_hash={short_hash(remote_id)} member_count={len(members)} "
            f"responsible_member_visible={visible}",
            flush=True,
        )
    print(f"VISIBILITY_CONCLUSION failures={failures}", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
