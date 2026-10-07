import json
import sqlite3
from pathlib import Path

import pytest

from scripts.follow_up_observe_once import select_latest_successful_task, select_successful_tasks


def make_db(path: Path, rows):
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE tool_calls(status TEXT, data_json TEXT);
            CREATE TABLE task_runs(task_run_id TEXT, tenant_id TEXT);
        """)
        connection.executemany("INSERT INTO task_runs VALUES (?, ?)", [(run, tenant) for run, tenant in {('run-a','tenant-1'),('run-b','tenant-1')}])
        connection.executemany("INSERT INTO tool_calls VALUES (?, ?)", rows)
        connection.commit()


def test_select_latest_successful_reference_is_internal_and_deterministic(tmp_path):
    db = tmp_path / "control.sqlite3"
    make_db(db, [
        ("SUCCEEDED", json.dumps({"remote_ref": "remote-a", "task_run_id": "run-a"})),
        ("SUCCEEDED", json.dumps({"remote_ref": "remote-b", "task_run_id": "run-b"})),
    ])
    assert select_latest_successful_task(db) == ("remote-b", "run-b", "tenant-1")


def test_select_successful_tasks_deduplicates_and_applies_newest_first_limit(tmp_path):
    db = tmp_path / "control.sqlite3"
    make_db(db, [
        ("SUCCEEDED", json.dumps({"remote_ref": "remote-a", "task_run_id": "run-a"})),
        ("SUCCEEDED", json.dumps({"remote_ref": "remote-b", "task_run_id": "run-b"})),
        ("SUCCEEDED", json.dumps({"remote_ref": "remote-b", "task_run_id": "run-b"})),
    ])
    assert select_successful_tasks(db) == [
        ("remote-b", "run-b", "tenant-1"),
        ("remote-a", "run-a", "tenant-1"),
    ]
    assert select_successful_tasks(db, limit=1) == [("remote-b", "run-b", "tenant-1")]


def test_select_requires_remote_reference_and_task_run(tmp_path):
    db = tmp_path / "control.sqlite3"
    make_db(db, [("SUCCEEDED", json.dumps({"remote_ref": "remote-a"}))])
    with pytest.raises(ValueError, match="missing"):
        select_latest_successful_task(db)
