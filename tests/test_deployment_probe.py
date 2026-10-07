import sqlite3

import pytest

from scripts.control_plane_deployment_probe import inspect_database


def database(tmp_path):
    path = tmp_path / "control.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE web_sessions(session_hash TEXT)")
        connection.execute("INSERT INTO web_sessions VALUES ('private-session-hash')")
        connection.execute("CREATE TABLE task_runs(data_json TEXT)")
        connection.execute('INSERT INTO task_runs VALUES (?)', ('{"status":"WAITING_REVIEW"}',))
    return path


def test_probe_makes_recoverable_backup_and_reports_only_aggregates(tmp_path):
    path = database(tmp_path)
    backup = tmp_path / "checkpoint.sqlite3"
    result = inspect_database(path, backup)
    assert result["integrity"] == "ok"
    assert result["backup_created"] is True
    assert result["counts"]["web_sessions"] == 1
    assert result["counts"]["approvals"] is None
    assert result["task_status_counts"] == {"WAITING_REVIEW": 1}
    assert "private-session-hash" not in str(result)
    assert inspect_database(backup)["counts"] == result["counts"]


def test_existing_checkpoint_and_source_cannot_be_overwritten(tmp_path):
    path = database(tmp_path)
    backup = tmp_path / "checkpoint.sqlite3"
    inspect_database(path, backup)
    before = backup.read_bytes()
    with pytest.raises(FileExistsError):
        inspect_database(path, backup)
    assert backup.read_bytes() == before
    with pytest.raises(ValueError, match="separate"):
        inspect_database(path, path)
    assert inspect_database(path)["integrity"] == "ok"


def test_missing_database_is_not_created_by_inspection(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError, match="does not exist"):
        inspect_database(missing)
    assert not missing.exists()
