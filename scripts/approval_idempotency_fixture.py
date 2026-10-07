"""Deterministic local fixture for approval and idempotency rules.

This is a phase-0 design verification helper. It has no database, network,
Feishu credentials, or external side effects. The output is intentionally
small and records only state transitions and counts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


def canonical_hash(value: dict[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Approval:
    approval_id: str
    task_run_id: str
    step_id: str
    action_version: int
    proposal_hash: str
    status: str = "PENDING"
    inbox_event_ids: set[str] = field(default_factory=set)
    approval_action_keys: set[str] = field(default_factory=set)
    outbox_write_keys: set[str] = field(default_factory=set)

    def receive_decision(
        self,
        *,
        event_id: str,
        approver_id: str,
        action_version: int,
        proposal_hash: str,
        decision: str = "APPROVE",
    ) -> str:
        if event_id in self.inbox_event_ids:
            return "DUPLICATE_EVENT"
        self.inbox_event_ids.add(event_id)

        if self.status != "PENDING":
            return "ALREADY_DECIDED"
        if action_version != self.action_version or proposal_hash != self.proposal_hash:
            self.status = "INVALIDATED"
            return "STALE_APPROVAL"
        if not approver_id:
            return "APPROVER_REJECTED"

        action_key = f"{self.approval_id}:{self.action_version}:{decision}"
        if action_key in self.approval_action_keys:
            return "DUPLICATE_APPROVAL_ACTION"
        self.approval_action_keys.add(action_key)

        if decision != "APPROVE":
            self.status = "REJECTED"
            return "REJECTED"

        self.status = "APPROVED"
        write_key = (
            f"{self.task_run_id}:{self.step_id}:test-target:{self.action_version}"
        )
        if write_key not in self.outbox_write_keys:
            self.outbox_write_keys.add(write_key)
            return "APPROVED_OUTBOX_CREATED"
        return "APPROVED_OUTBOX_ALREADY_EXISTS"


def main() -> int:
    proposal = {
        "operation_type": "task.create",
        "title": "Alpha 项目接口开发",
        "assignee": "test-user-a",
        "due_date": "2099-12-31",
    }
    proposal_hash = canonical_hash(proposal)
    approval = Approval(
        approval_id="approval-001",
        task_run_id="run-001",
        step_id="step-001",
        action_version=1,
        proposal_hash=proposal_hash,
    )

    first = approval.receive_decision(
        event_id="event-001",
        approver_id="admin-a",
        action_version=1,
        proposal_hash=proposal_hash,
    )
    assert first == "APPROVED_OUTBOX_CREATED"
    assert approval.status == "APPROVED"
    assert len(approval.outbox_write_keys) == 1

    same_event = approval.receive_decision(
        event_id="event-001",
        approver_id="admin-a",
        action_version=1,
        proposal_hash=proposal_hash,
    )
    assert same_event == "DUPLICATE_EVENT"

    second_click = approval.receive_decision(
        event_id="event-002",
        approver_id="admin-a",
        action_version=1,
        proposal_hash=proposal_hash,
    )
    assert second_click == "ALREADY_DECIDED"
    assert len(approval.outbox_write_keys) == 1

    stale = Approval(
        approval_id="approval-002",
        task_run_id="run-002",
        step_id="step-001",
        action_version=2,
        proposal_hash=proposal_hash,
    )
    stale_result = stale.receive_decision(
        event_id="event-003",
        approver_id="admin-a",
        action_version=1,
        proposal_hash=proposal_hash,
    )
    assert stale_result == "STALE_APPROVAL"
    assert stale.status == "INVALIDATED"
    assert not stale.outbox_write_keys

    print("APPROVAL_FIXTURE_PASS")
    print(f"first={first}")
    print(f"same_event={same_event}")
    print(f"second_click={second_click}")
    print(f"stale={stale_result}")
    print(f"outbox_count={len(approval.outbox_write_keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
