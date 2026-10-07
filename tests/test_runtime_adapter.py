import subprocess
from pathlib import Path

from fde_control_plane import (
    Actor,
    ActorType,
    AgentVersion,
    AssistantBinding,
    ControlPlane,
    HermesRuntimeAdapter,
    MeetingWorkflow,
    RunRequest,
    SQLiteStore,
)


def request() -> RunRequest:
    return RunRequest(
        task_run_id="task-1",
        trace_id="trace-1",
        actor_ref="member-1",
        agent_version="meeting-agent@1",
        input_ref={
            "document_id_hash": "doc-hash",
            "revision_id": "7",
            "blocks": [
                {"index": 8, "block_type": 2, "text": "张三负责整理部署文档。"},
            ],
        },
        tool_policy_snapshot={"allowed_tools": [], "risk": "HIGH"},
        deadline="2026-09-27T12:00:00Z",
    )


def test_hermes_adapter_returns_contract_and_disables_tools():
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["input"] = kwargs["input"]
        seen["encoding"] = kwargs["encoding"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='```json\n{"schema_version":"meeting-agent.v1","source":{"document_id_hash":"doc-hash","revision_id":"7","source_block_ids":[8]},"todos":[],"relations":[]}\n```',
            stderr="",
        )

    result = HermesRuntimeAdapter(executable="hermes-test", runner=runner).run_once(request())

    assert result.status == "SUCCEEDED"
    assert result.payload["schema_version"] == "meeting-agent.v1"
    assert [event.event_type for event in result.events] == ["accepted", "completed"]
    assert "--safe-mode" in seen["command"]
    assert "--ignore-user-config" in seen["command"]
    assert "--ignore-rules" in seen["command"]
    assert "--toolsets" not in seen["command"]
    assert "doc-hash" in seen["input"]
    assert "Do not silently omit an action item" in seen["input"]
    assert "access_token" not in seen["input"]
    assert seen["encoding"] == "utf-8"


def test_hermes_adapter_fails_closed_on_invalid_json():
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="not json", stderr="")

    result = HermesRuntimeAdapter(runner=runner).run_once(request())

    assert result.status == "FAILED"
    assert result.error_code == "INVALID_JSON"
    assert result.payload is None
    assert result.events[-1].event_type == "failed"


def test_hermes_adapter_can_use_python_entrypoint():
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    HermesRuntimeAdapter(
        executable="blocked-hermes.exe", python_executable="python-test", runner=runner
    ).run_once(request())

    assert seen["command"][:3] == [
        "python-test", "-c", "from hermes_cli.main import main; main()"
    ]
    assert seen["command"][3] == "chat"
    assert "--safe-mode" in seen["command"]


def test_hermes_adapter_marks_timeout_unknown():
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    result = HermesRuntimeAdapter(runner=runner).run_once(request())

    assert result.status == "UNKNOWN"
    assert result.error_code == "RUNTIME_TIMEOUT"
    assert result.events[-1].event_type == "unknown"


def test_successful_runtime_result_enters_meeting_contract_gate(tmp_path: Path):
    payload = {
        "schema_version": "meeting-agent.v1",
        "source": {"document_id_hash": "doc-hash", "revision_id": "7", "source_block_ids": [8]},
        "summary": {"text": "会议摘要", "evidence_block_ids": [8]},
        "todos": [{
            "todo_id": "todo-8",
            "title": "整理部署文档",
            "evidence_block_ids": [8],
            "assignee_candidate": {"display_name": "张三"},
            "due_date_candidate": {"raw_text": "下周三", "normalized_date": None, "status": "needs_context"},
            "needs_confirmation": True,
            "confirmation_reasons": ["relative_date_not_normalized"],
        }],
        "relations": [],
    }
    cp = ControlPlane(SQLiteStore(tmp_path / "runtime.sqlite3"))
    cp.register_actor(Actor("member-1", "tenant-1", ActorType.USER))
    workflow = MeetingWorkflow(cp)
    result = workflow.ingest_runtime_result(
        runtime_result=__import__("fde_control_plane").RuntimeResult(
            status="SUCCEEDED", payload=payload, external_run_ref=None,
        ),
        record_id="meeting-runtime-1", tenant_id="tenant-1", submitted_by="member-1",
        document_id_hash="doc-hash", revision_id="7", valid_block_ids={8},
    )
    assert result.validation.status == "NEEDS_HUMAN_REVIEW"
    assert result.record.record_id == "meeting-runtime-1"
    cp.close()
