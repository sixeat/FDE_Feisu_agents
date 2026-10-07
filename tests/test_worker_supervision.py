import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fde_control_plane import SQLiteStore
from scripts import meeting_task_worker as worker


def test_observer_does_not_claim_or_migrate_and_can_stop(tmp_path, capsys):
    path = tmp_path / "queue.sqlite3"
    with SQLiteStore(path) as store:
        for index, status in enumerate(("PREPARED", "UNKNOWN", "SUCCEEDED")):
            store.connection.execute("INSERT INTO tool_calls VALUES (?, ?, ?, ?)",
                                     (f"call-{index}", f"key-{index}", status, json.dumps({"status": status})))
            store.save_outbox(f"out-{index}", f"key-{index}", "READY", {"tool_call_id": f"call-{index}"})
    original = path.read_bytes()
    heartbeat = tmp_path / "heartbeat"
    class StopAfterFirstPoll(threading.Event):
        def wait(self, timeout=None):
            self.set()
    assert worker.observe(SimpleNamespace(db=str(path), heartbeat_file=heartbeat, poll_interval=30), StopAfterFirstPoll()) == 0
    assert path.read_bytes() == original
    assert 0 <= time.time() - float(heartbeat.read_text()) < 5
    output = capsys.readouterr().out
    assert '"recoverable_count": 1' in output and '"uncertain_count": 1' in output
    assert "WORKER_STOPPED remote_write=0" in output
    assert "call-" not in output


def test_failed_database_read_does_not_refresh_health(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text("10")
    with pytest.raises(Exception):
        worker.observe(SimpleNamespace(db=str(tmp_path / "missing.sqlite3"), heartbeat_file=heartbeat, poll_interval=30), threading.Event())
    assert heartbeat.read_text() == "10"
    assert not (tmp_path / "missing.sqlite3").exists()


def test_continuous_writes_are_rejected(monkeypatch):
    monkeypatch.setattr("sys.argv", ["worker", "--loop"])
    with pytest.raises(SystemExit) as exc:
        worker.parse_args()
    assert exc.value.code == 2


def test_dry_run_does_not_construct_gateway(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    with SQLiteStore(path):
        pass
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not load credentials or construct gateway")
    monkeypatch.setattr(worker, "FeishuTaskGateway", forbidden)
    assert worker.run_once(SimpleNamespace(db=str(path), limit=1, dry_run=True)) == 0
