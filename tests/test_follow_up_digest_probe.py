import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_digest_probe_fixture_is_deidentified_and_unsent(tmp_path):
    fixture = tmp_path / "observations.json"
    fixture.write_text(json.dumps([
        {"tenant_id": "tenant-1", "task_run_id": "run-secret", "status": "OVERDUE", "observed_at": "2026-10-05T12:00:00Z"},
        {"tenant_id": "tenant-1", "task_run_id": "run-done", "status": "COMPLETED", "observed_at": "2026-10-05T12:00:00Z"},
    ]), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "follow_up_digest_probe.py"),
         "--fixture", str(fixture), "--tenant-id", "tenant-1", "--recipient-actor-id", "member-secret",
         "--business-date", "2026-10-05"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "FOLLOW_UP_COUNT status=OVERDUE count=1" in result.stdout
    assert "FOLLOW_UP_ATTENTION_COUNT=1" in result.stdout
    assert "run-secret" not in result.stdout
    assert "member-secret" not in result.stdout
    assert "REMOTE_WRITE=0" in result.stdout
    assert "NOTIFICATION_SENT=0" in result.stdout
