"""Validation of the structured meeting-Agent boundary.

This is intentionally a schema/evidence gate. It does not attempt to decide
whether two pieces of meeting text are semantically duplicates; that remains
the Agent's job and unresolved candidates stay human-reviewable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_SCHEMA = "meeting-agent.v1"


@dataclass(frozen=True)
class ContractValidation:
    status: str
    reasons: tuple[str, ...]
    todo_count: int = 0
    write_eligible_count: int = 0


def validate_meeting_contract(
    payload: dict[str, Any],
    *,
    document_id_hash: str,
    revision_id: str | int,
    valid_block_ids: set[int] | frozenset[int],
) -> ContractValidation:
    reasons: list[str] = []
    if payload.get("schema_version") != SUPPORTED_SCHEMA:
        reasons.append("UNSUPPORTED_SCHEMA")
    source = payload.get("source") or {}
    if source.get("document_id_hash") != document_id_hash:
        reasons.append("SOURCE_DOCUMENT_MISMATCH")
    if str(source.get("revision_id")) != str(revision_id):
        reasons.append("STALE_REVISION")
    valid_blocks = set(valid_block_ids)
    source_blocks = set(source.get("source_block_ids") or [])
    if not source_blocks or not source_blocks.issubset(valid_blocks):
        reasons.append("UNKNOWN_SOURCE_BLOCK")

    todos = payload.get("todos") or []
    todo_ids: set[str] = set()
    eligible = 0
    for todo in todos:
        todo_id = todo.get("todo_id")
        if not todo_id or not todo.get("title"):
            reasons.append("TODO_REQUIRED_FIELD_MISSING")
        if todo_id:
            if todo_id in todo_ids:
                reasons.append(f"TODO_ID_DUPLICATE:{todo_id}")
            todo_ids.add(todo_id)
        evidence = set(todo.get("evidence_block_ids") or [])
        if not evidence or not evidence.issubset(valid_blocks):
            reasons.append(f"TODO_EVIDENCE_INVALID:{todo_id or 'unknown'}")
        if todo.get("needs_confirmation"):
            reasons.append(f"TODO_NEEDS_CONFIRMATION:{todo_id or 'unknown'}")
        else:
            eligible += 1

    for relation in payload.get("relations") or []:
        relation_ids = set(relation.get("todo_ids") or [])
        if len(relation_ids) < 2 or not relation_ids.issubset(todo_ids):
            reasons.append("RELATION_TODO_REFERENCE_INVALID")
        evidence = set(relation.get("evidence_block_ids") or [])
        if not evidence or not evidence.issubset(valid_blocks):
            reasons.append("RELATION_EVIDENCE_INVALID")
        if relation.get("needs_human_confirmation"):
            reasons.append("RELATION_NEEDS_CONFIRMATION")

    # Structural failures cannot be repaired by confirming an otherwise valid draft.
    review_only = {"RELATION_NEEDS_CONFIRMATION"}
    invalid = any(
        reason not in review_only and not reason.startswith("TODO_NEEDS_CONFIRMATION:")
        for reason in reasons
    )
    status = "INVALID" if invalid else "NEEDS_HUMAN_REVIEW" if reasons else "READY_FOR_REVIEW"
    return ContractValidation(status, tuple(sorted(set(reasons))), len(todos), 0)
