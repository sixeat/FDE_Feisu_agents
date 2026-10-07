import json
import sqlite3

from scripts.meeting_task_reconcile import main


def test_inspect_is_read_only_and_prints_only_hashes(tmp_path, capsys):
    path = tmp_path / "inspect.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tool_calls(status TEXT, data_json TEXT)")
        conn.execute("INSERT INTO tool_calls VALUES (?, ?)", ("UNKNOWN", json.dumps({
            "tool_call_id": "private-call", "status": "UNKNOWN", "remote_ref": "private-remote",
        })))
    before = path.read_bytes()
    assert main(["inspect", "--db", str(path), "--credentials-file", "must-not-read.json"]) == 0
    output = capsys.readouterr().out
    assert "private-" not in output
    assert json.loads(output)["calls"][0]["status"] == "UNKNOWN"
    assert path.read_bytes() == before


def test_missing_database_not_created_and_errors_redacted(tmp_path, capsys):
    path = tmp_path / "private-path.sqlite3"
    assert main(["inspect", "--db", str(path)]) == 2
    assert not path.exists()
    assert str(path) not in capsys.readouterr().out


def test_prepared_call_cannot_use_reconciliation_as_write_bypass(tmp_path, capsys):
    path = tmp_path / "prepared.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tool_calls(status TEXT, data_json TEXT)")
        conn.execute("INSERT INTO tool_calls VALUES (?, ?)", ("PREPARED", json.dumps({"tool_call_id": "call-1"})))
    assert main(["reconcile", "--db", str(path), "--call-id", "call-1", "--credentials-file", "must-not-read.json"]) == 2
    assert "NOT_ELIGIBLE" in capsys.readouterr().out
