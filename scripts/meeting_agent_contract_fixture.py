"""Local fixture for the meeting Agent output contract.

No model, network, database, or Feishu credentials are used. The fixture
simulates an Agent result and verifies that the control-plane boundary keeps
incomplete, stale, and conflicting proposals out of the write queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_SCHEMA = "meeting-agent.v1"
DOCUMENT_ID_HASH = "31eafe749d09"
REVISION_ID = "6"
VALID_BLOCKS = {1, 2, 4, 8, 9, 10, 12}


@dataclass
class ValidationResult:
    status: str
    reasons: list[str]


def validate_result(payload: dict[str, Any]) -> ValidationResult:
    reasons: list[str] = []
    if payload.get("schema_version") != SUPPORTED_SCHEMA:
        reasons.append("UNSUPPORTED_SCHEMA")

    source = payload.get("source") or {}
    if source.get("document_id_hash") != DOCUMENT_ID_HASH:
        reasons.append("SOURCE_DOCUMENT_MISMATCH")
    if str(source.get("revision_id")) != REVISION_ID:
        reasons.append("STALE_REVISION")
    source_blocks = set(source.get("source_block_ids") or [])
    if not source_blocks or not source_blocks.issubset(VALID_BLOCKS):
        reasons.append("UNKNOWN_SOURCE_BLOCK")

    todos = payload.get("todos") or []
    todo_ids = {todo.get("todo_id") for todo in todos}
    for todo in todos:
        if not todo.get("todo_id") or not todo.get("title"):
            reasons.append("TODO_REQUIRED_FIELD_MISSING")
        evidence = set(todo.get("evidence_block_ids") or [])
        if not evidence or not evidence.issubset(VALID_BLOCKS):
            reasons.append("TODO_EVIDENCE_INVALID")
        if todo.get("needs_confirmation"):
            reasons.append(f"TODO_NEEDS_CONFIRMATION:{todo.get('todo_id')}")

    for relation in payload.get("relations") or []:
        relation_ids = set(relation.get("todo_ids") or [])
        if len(relation_ids) < 2 or not relation_ids.issubset(todo_ids):
            reasons.append("RELATION_TODO_REFERENCE_INVALID")
        if not relation.get("evidence_block_ids"):
            reasons.append("RELATION_EVIDENCE_MISSING")
        if relation.get("needs_human_confirmation"):
            reasons.append("RELATION_NEEDS_CONFIRMATION")

    if reasons:
        return ValidationResult("NEEDS_HUMAN_REVIEW", sorted(set(reasons)))
    return ValidationResult("READY_FOR_REVIEW", [])


def sample_payload() -> dict[str, Any]:
    return {
        "schema_version": SUPPORTED_SCHEMA,
        "source": {
            "document_id_hash": DOCUMENT_ID_HASH,
            "revision_id": REVISION_ID,
            "source_block_ids": [8, 9, 10, 12],
        },
        "todos": [
            {
                "todo_id": "todo-1",
                "title": "整理部署文档",
                "assignee_candidate": {"display_name": "张三"},
                "due_date_candidate": {
                    "raw_text": "下周三",
                    "normalized_date": None,
                    "status": "needs_context",
                },
                "evidence_block_ids": [8],
                "needs_confirmation": True,
            },
            {
                "todo_id": "todo-2",
                "title": "跟进接口联调",
                "assignee_candidate": {"display_name": None},
                "due_date_candidate": {"raw_text": "待确认"},
                "evidence_block_ids": [9],
                "needs_confirmation": True,
            },
            {
                "todo_id": "todo-3",
                "title": "负责后续事项",
                "assignee_candidate": {"display_name": "王五"},
                "due_date_candidate": {"raw_text": None},
                "evidence_block_ids": [10],
                "needs_confirmation": True,
            },
            {
                "todo_id": "todo-4",
                "title": "接口联调",
                "assignee_candidate": {"display_name": "李四"},
                "due_date_candidate": {"raw_text": "下周五"},
                "evidence_block_ids": [12],
                "needs_confirmation": True,
            },
        ],
        "relations": [
            {
                "todo_ids": ["todo-2", "todo-4"],
                "relation": "possible_duplicate_or_conflict",
                "reason": "两条内容都涉及接口联调，但截止日期信息不同",
                "evidence_block_ids": [9, 12],
                "field_conflicts": ["due_date"],
                "needs_human_confirmation": True,
            }
        ],
    }


def main() -> int:
    payload = sample_payload()
    valid = validate_result(payload)
    assert valid.status == "NEEDS_HUMAN_REVIEW"
    assert "TODO_NEEDS_CONFIRMATION:todo-2" in valid.reasons
    assert "RELATION_NEEDS_CONFIRMATION" in valid.reasons

    stale = sample_payload()
    stale["source"]["revision_id"] = "4"
    stale_result = validate_result(stale)
    assert stale_result.status == "NEEDS_HUMAN_REVIEW"
    assert "STALE_REVISION" in stale_result.reasons

    invalid_source = sample_payload()
    invalid_source["todos"][0]["evidence_block_ids"] = [999]
    invalid_result = validate_result(invalid_source)
    assert "TODO_EVIDENCE_INVALID" in invalid_result.reasons

    print("MEETING_AGENT_CONTRACT_FIXTURE_PASS")
    print(f"sample_status={valid.status}")
    print(f"sample_reason_count={len(valid.reasons)}")
    print(f"stale_revision={stale_result.status}")
    print(f"invalid_evidence={invalid_result.status}")
    print("write_eligible_count=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
