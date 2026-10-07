"""Run one no-tool Hermes meeting-agent probe and validate its contract."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane import (  # noqa: E402
    HermesRuntimeAdapter,
    RunRequest,
    validate_meeting_contract,
)


def short_hash(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def main() -> int:
    blocks = [
        {"index": 8, "block_type": 13, "text": "张三负责整理部署文档，截止日期为下周三。"},
        {"index": 9, "block_type": 2, "text": "李四跟进接口联调，截止时间待确认。"},
        {"index": 10, "block_type": 2, "text": "王五负责后续事项。"},
        {"index": 12, "block_type": 2, "text": "补充：接口联调由李四负责，截止日期为下周五。"},
    ]
    request = RunRequest(
        task_run_id="hermes-probe-task",
        trace_id="hermes-probe-trace",
        actor_ref="probe-member",
        agent_version="meeting-agent@1",
        input_ref={
            "document_id_hash": short_hash("hermes-probe-document"),
            "revision_id": "probe-1",
            "blocks": blocks,
        },
        tool_policy_snapshot={"allowed_tools": [], "risk": "HIGH"},
        deadline=datetime.now(timezone.utc).isoformat(),
    )
    print("HERMES_MEETING_PROBE_PLAN tools=0 side_effects=0")
    result = HermesRuntimeAdapter().run_once(request)
    print(
        f"HERMES_RUNTIME_RESULT status={result.status} "
        f"error_code={result.error_code or 'NONE'} "
        f"event_count={len(result.events)}"
    )
    if result.status != "SUCCEEDED" or result.payload is None:
        return 1
    validation = validate_meeting_contract(
        result.payload,
        document_id_hash=request.input_ref["document_id_hash"],
        revision_id=request.input_ref["revision_id"],
        valid_block_ids={block["index"] for block in blocks},
    )
    print(
        f"HERMES_CONTRACT_RESULT status={validation.status} "
        f"todo_count={validation.todo_count} reasons={','.join(validation.reasons) or 'NONE'}"
    )
    print(f"HERMES_RELATION_COUNT count={len(result.payload.get('relations') or [])}")
    if not result.payload.get("todos"):
        print("HERMES_SEMANTIC_WARNING code=NO_TODO_CANDIDATES expected_at_least=1")
        return 1
    for todo in result.payload.get("todos") or []:
        print(
            f"HERMES_TODO todo_id_hash={short_hash(todo.get('todo_id'))} "
            f"evidence={','.join(str(item) for item in todo.get('evidence_block_ids') or [])} "
            f"needs_confirmation={bool(todo.get('needs_confirmation'))}"
        )
    return 0 if validation.status in {"READY_FOR_REVIEW", "NEEDS_HUMAN_REVIEW"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
