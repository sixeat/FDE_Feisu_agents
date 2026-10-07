"""Inspect uncertain calls or reconcile one call. Never write to Feishu.

Default inspection uses read-only SQLite and needs no SDK or credentials.
Reconcile updates local state only. probe-list checks one visible API page;
it never treats an empty page as proof that an earlier create failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def inspect_calls(path: Path) -> list[dict]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT data_json FROM tool_calls WHERE status IN ('DISPATCHED', 'UNKNOWN', 'RECONCILING') ORDER BY rowid"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=["inspect", "reconcile", "probe-list"], default="inspect")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("FDE_DB_PATH", "/var/lib/fde-agent/control_plane.sqlite3")))
    parser.add_argument("--call-id", help="one internal tool-call ID; never a credential")
    parser.add_argument("--credentials-file", type=Path, help="local JSON with app_id/app_secret; optional if environment is set")
    args = parser.parse_args(argv)
    if args.mode == "reconcile" and not args.call_id:
        parser.error("reconcile requires --call-id")
    try:
        if args.mode != "probe-list":
            calls = inspect_calls(args.db)
            if args.mode == "inspect":
                print(json.dumps({"mode": "inspect", "remote_write": 0, "calls": [
                    {"call_hash": short_hash(c["tool_call_id"]), "status": c["status"],
                     "has_remote_ref": bool(c.get("remote_ref"))} for c in calls
                ]}))
                return 0
            if args.call_id not in {c["tool_call_id"] for c in calls}:
                print("RECONCILE_RESULT status=NOT_ELIGIBLE remote_write=0")
                return 2
        config = json.loads(args.credentials_file.read_text(encoding="utf-8-sig")) if args.credentials_file else {}
        app_id = config.get("app_id") or os.environ.get("FEISHU_APP_ID")
        app_secret = config.get("app_secret") or os.environ.get("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            print("RECONCILE_ERROR reason=MISSING_CREDENTIALS remote_write=0")
            return 2
        import lark_oapi as lark
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).timeout(12).log_level(lark.LogLevel.ERROR).build()
        if args.mode == "probe-list":
            response = client.task.v2.task.list(lark.api.task.v2.model.ListTaskRequest.builder().page_size(50).build())
            code = response.code if isinstance(response.code, int) else None
            data = getattr(response, "data", None)
            items = getattr(data, "items", None)
            print(json.dumps({"mode": "probe-list", "remote_write": 0, "code": code,
                              "items_is_list": isinstance(items, list), "item_count": len(items) if isinstance(items, list) else None,
                              "has_more": getattr(data, "has_more", None), "pages_read": 1}))
            return 0 if code == 0 else 1
        from fde_control_plane import ControlPlane, FeishuTaskGateway, MeetingOutboxWorker, SQLiteStore
        with SQLiteStore(args.db) as store:
            result = MeetingOutboxWorker(ControlPlane(store), FeishuTaskGateway(client=client)).reconcile(args.call_id)
            print(json.dumps({"mode": "reconcile", "remote_write": 0, "call_hash": short_hash(result.tool_call_id),
                              "status": result.status, "warning": result.warning,
                              "remote_hash": short_hash(result.remote_task_id) if result.remote_task_id else None}))
            return 0 if result.status == "SUCCEEDED" else 1
    except Exception:
        # No SDK exception text, payload, credentials, or raw IDs in logs.
        print("RECONCILE_ERROR reason=CHECK_FAILED remote_write=0")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
