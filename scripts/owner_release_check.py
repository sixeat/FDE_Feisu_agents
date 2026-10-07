"""Read-only release check for owner-task visibility, without exposing IDs."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from collections import Counter

from fde_control_plane.follow_up import confirmed_task_bindings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError("database integrity check failed")
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("meeting_records", "meeting_todo_drafts", "approvals", "tool_calls",
                                "outbox", "follow_up_observations", "notification_deliveries")}
        tenant_rows = connection.execute("SELECT DISTINCT tenant_id FROM task_runs").fetchall()
        bindings = [binding for row in tenant_rows
                    for binding in confirmed_task_bindings(connection, row[0])]
        counts["owner_task_bindings"] = len(bindings)
        owner_rows = connection.execute(
            "SELECT json_extract(data_json, '$.decision') AS decision, COUNT(*) AS count "
            "FROM follow_up_observations WHERE json_extract(data_json, '$.decision') IS NOT NULL "
            "GROUP BY decision"
        ).fetchall()
        counts["owner_decisions"] = {row["decision"]: row["count"] for row in owner_rows}
        counts["owner_audits"] = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'OWNER_DECISION_RECORDED'"
        ).fetchone()[0]
        today = date.today().isoformat()
        counts["approved_due_in_past"] = sum(
            str(binding["proposal"]["arguments"].get("due_date") or "") < today
            for binding in bindings if binding["proposal"]["arguments"].get("due_date")
        )
        latest_remote = {}
        for row in connection.execute("SELECT data_json FROM follow_up_observations ORDER BY rowid"):
            observation = json.loads(row[0])
            if observation.get("decision") or not observation.get("remote_task_id"):
                continue
            key = (observation.get("task_run_id"), observation["remote_task_id"])
            latest_remote[key] = observation
        counts["approved_remote_due_mismatch"] = sum(
            bool(latest_remote.get((binding["task"]["task_run_id"], binding["call"]["remote_ref"]), {}).get("due_at"))
            and str(latest_remote[(binding["task"]["task_run_id"], binding["call"]["remote_ref"])]["due_at"])[:10]
            != str(binding["proposal"]["arguments"].get("due_date") or "")
            for binding in bindings
        )
        counts["due_date_pairs"] = dict(Counter(
            (str(binding["proposal"]["arguments"].get("due_date") or "") + " -> " +
             str(latest_remote.get((binding["task"]["task_run_id"],
                                    binding["call"]["remote_ref"]), {}).get("due_at") or "")[:10])
            for binding in bindings
        ))
    print(json.dumps({"integrity": check, "counts": counts}, sort_keys=True))


if __name__ == "__main__":
    main()
