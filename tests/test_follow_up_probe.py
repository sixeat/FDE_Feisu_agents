import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.feishu_task_follow_up_probe import load_remote_id_from_db, snapshot_from_fixture
from fde_control_plane import FollowUpStatus


ROOT = Path(__file__).resolve().parents[1]


def test_fixture_uses_sdk_shape_and_maps_status_without_raw_id(capsys):
    code, snapshot = snapshot_from_fixture({
        "code": 0,
        "data": {"task": {"guid": "remote-secret", "status": "DONE"}},
    })
    assert code == 0
    assert snapshot.remote_task_id == "remote-secret"
    assert snapshot.mapping.status == FollowUpStatus.COMPLETED


def test_probe_fixture_prints_only_deidentified_fields(tmp_path):
    fixture = tmp_path / "response.json"
    fixture.write_text(json.dumps({
        "code": 0,
        "data": {"task": {
            "guid": "remote-secret",
            "status": "DONE",  # Terminal state keeps this redaction test independent of today's date.
            "summary": "不要输出的任务正文",
            "members": [{"id": "ou_secret"}],
            "due": {"timestamp": 1791417600000},
        }},
    }), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "feishu_task_follow_up_probe.py"), "--fixture", str(fixture)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "TASK_FOLLOW_UP_READ code=0" in result.stdout
    assert "TASK_STATUS_MAPPING=COMPLETED" in result.stdout
    assert "remote-secret" not in result.stdout
    assert "不要输出的任务正文" not in result.stdout
    assert "ou_secret" not in result.stdout
    assert "REMOTE_WRITE=0" in result.stdout


def test_probe_requires_remote_id_without_fixture():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "feishu_task_follow_up_probe.py")],
        check=False, capture_output=True, text=True,
        env={key: value for key, value in __import__("os").environ.items() if key != "FEISHU_TASK_REMOTE_ID"},
    )
    assert result.returncode == 2
    assert "MISSING_REMOTE_ID" in result.stderr


def test_db_lookup_reads_only_a_unique_successful_remote_reference(tmp_path):
    db = tmp_path / "control.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("create table tool_calls(tool_call_id text primary key, status text, data_json text)")
        connection.execute(
            "insert into tool_calls values (?, ?, ?)",
            ("call-1", "SUCCEEDED", json.dumps({"remote_ref": "remote-secret"})),
        )
        connection.commit()
    assert load_remote_id_from_db(db) == "remote-secret"


def test_db_lookup_rejects_ambiguous_latest_successes(tmp_path):
    db = tmp_path / "control.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("create table tool_calls(tool_call_id text primary key, status text, data_json text)")
        for index, remote in enumerate(("remote-a", "remote-b")):
            connection.execute(
                "insert into tool_calls values (?, ?, ?)",
                (f"call-{index}", "SUCCEEDED", json.dumps({"remote_ref": remote})),
            )
        connection.commit()
    with pytest.raises(ValueError, match="ambiguous"):
        load_remote_id_from_db(db)
    assert load_remote_id_from_db(db, select_latest=True) == "remote-b"
