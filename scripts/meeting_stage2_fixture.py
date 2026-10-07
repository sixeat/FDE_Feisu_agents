"""Offline stage-2 vertical-slice acceptance fixture.

It exercises document-block normalization, evidence-bound meeting records,
human draft revision, approval-bound proposals and an idempotent task gateway.
No Feishu credentials, model API, or network request is used.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fde_control_plane import (
    Actor,
    ActorType,
    AgentVersion,
    AssistantBinding,
    ControlPlane,
    FakeFeishuTaskGateway,
    MeetingOutboxWorker,
    MeetingWorkflow,
    SQLiteStore,
)


CAPS = frozenset({"task.write", "doc.read"})


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fde-stage2-") as temp:
        cp = ControlPlane(SQLiteStore(Path(temp) / "stage2.sqlite3"))
        cp.register_actor(Actor("user-1", "tenant-1", ActorType.USER, CAPS))
        cp.register_actor(Actor("assistant-1", "tenant-1", ActorType.PERSONAL_ASSISTANT, CAPS))
        cp.register_actor(Actor("meeting-agent", "tenant-1", ActorType.BUSINESS_AGENT, CAPS))
        cp.register_actor(Actor("admin-1", "tenant-1", ActorType.USER, roles=frozenset({"admin"})))
        cp.register_agent_version(AgentVersion("meeting-agent", 1, CAPS))
        cp.bind_assistant(AssistantBinding("binding-1", "tenant-1", "user-1", "assistant-1"))
        task = cp.create_task_run("user-1", "assistant-1", "meeting-agent", 1)
        workflow = MeetingWorkflow(cp)
        payload = {
            "schema_version": "meeting-agent.v1",
            "source": {"document_id_hash": "fixture-doc", "revision_id": "1", "source_block_ids": [8, 9]},
            "todos": [
                {"todo_id": "todo-1", "title": "整理部署文档", "evidence_block_ids": [8],
                 "due_date_candidate": {"normalized_date": "2026-10-02", "status": "normalized"},
                 "needs_confirmation": False},
                {"todo_id": "todo-2", "title": "跟进接口联调", "evidence_block_ids": [9],
                 "due_date_candidate": {"normalized_date": None, "status": "needs_context"},
                 "needs_confirmation": True},
            ], "relations": [],
        }
        workflow.ingest(record_id="meeting-fixture", tenant_id="tenant-1", submitted_by="user-1",
                        payload=payload, document_id_hash="fixture-doc", revision_id="1", valid_block_ids={8, 9})
        workflow.build_drafts(record_id="meeting-fixture", payload=payload, assignee_actor_by_todo={"todo-1": "user-1"})
        workflow.revise_draft("meeting-fixture", "todo-2", assignee_actor_id="user-1", due_date="2026-10-03")
        proposals = workflow.create_task_proposals("meeting-fixture", task.task_run_id)
        gateway = FakeFeishuTaskGateway()
        worker = MeetingOutboxWorker(cp, gateway)
        results = []
        for proposal, approval in proposals:
            callback = cp.handle_approval_callback(
                f"fixture-callback-{proposal.operation_id}", approval.approval_id, "admin-1", True,
                proposal.action_version, proposal.proposal_hash,
            )
            results.append(worker.execute(callback["tool_call_id"]))
        assert len(results) == 2 and all(item.status == "SUCCEEDED" for item in results)
        assert len(gateway.tasks) == 2 and len(gateway.notifications) == 2
        print("MEETING_STAGE2_FIXTURE_PASS")
        print("meeting_record=fixture-doc@1")
        print("draft_count=2 revised_count=1")
        print("approved_proposals=2")
        print("remote_tasks=2 notifications=2")
        cp.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
