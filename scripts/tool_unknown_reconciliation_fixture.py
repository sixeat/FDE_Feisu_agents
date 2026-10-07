"""Local fixture for UNKNOWN external writes and reconciliation.

No network or Feishu API is used. The fixture verifies that a timeout does not
create a second write attempt and that reconciliation can resolve the remote
task when a later query finds it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolCall:
    write_key: str
    status: str = "PREPARED"
    remote_id: str | None = None
    attempts: int = 0

    def dispatch(self, outcome: str) -> str:
        if self.status in {"SUCCEEDED", "FAILED"}:
            return self.status
        if self.attempts > 0:
            return "RETRY_BLOCKED_UNTIL_RECONCILED"
        self.attempts += 1
        if outcome == "timeout":
            self.status = "UNKNOWN"
            return self.status
        if outcome == "success":
            self.status = "SUCCEEDED"
            self.remote_id = "remote-task-001"
            return self.status
        self.status = "FAILED"
        return self.status

    def reconcile(self, query_result: str | None) -> str:
        if self.status != "UNKNOWN":
            return self.status
        if query_result:
            self.status = "SUCCEEDED"
            self.remote_id = query_result
            return self.status
        self.status = "RECONCILING"
        return self.status


def main() -> int:
    resolved = ToolCall(write_key="run-001:step-001:target:1")
    timeout = resolved.dispatch("timeout")
    retry_before_query = resolved.dispatch("success")
    reconciled = resolved.reconcile("remote-task-001")

    unresolved = ToolCall(write_key="run-002:step-001:target:1")
    unresolved.dispatch("timeout")
    still_unknown = unresolved.reconcile(None)

    assert timeout == "UNKNOWN"
    assert retry_before_query == "RETRY_BLOCKED_UNTIL_RECONCILED"
    assert reconciled == "SUCCEEDED"
    assert resolved.remote_id == "remote-task-001"
    assert resolved.attempts == 1
    assert still_unknown == "RECONCILING"

    print("UNKNOWN_RECONCILIATION_FIXTURE_PASS")
    print(f"timeout={timeout}")
    print(f"retry_before_query={retry_before_query}")
    print(f"reconciled={reconciled} remote_id={resolved.remote_id}")
    print(f"unresolved={still_unknown}")
    print(f"write_attempts={resolved.attempts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
