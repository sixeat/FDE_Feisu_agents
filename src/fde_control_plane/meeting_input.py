"""Normalize Feishu document blocks into a model-ready meeting contract.

This deterministic fallback is useful for local fixtures and degraded mode. A
semantic meeting Agent can replace it by returning the same ``meeting-agent.v1``
payload; source block and revision fields remain mandatory in either case.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


_NAME = r"[\u4e00-\u9fff]{2,4}"


def _assignee(text: str) -> str | None:
    match = re.search(rf"由\s*({_NAME})\s*负责", text)
    if match:
        return match.group(1)
    match = re.search(rf"(?<![\u4e00-\u9fff])({_NAME})\s*负责", text)
    return match.group(1) if match else None


def _due_date(text: str) -> str | None:
    match = re.search(r"截止(?:日期|时间)?为?\s*([^，。；;]+)", text)
    return match.group(1).strip() if match else None


def normalize_document_blocks(
    *,
    document_id_hash: str,
    revision_id: str | int,
    blocks: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build an auditable candidate payload from block text.

    ``blocks`` contains only local normalized fields (index, type, text). The
    function never invents a normalized date or a member identity.
    """
    block_list = list(blocks)
    source_ids = [int(block["index"]) for block in block_list if str(block.get("text") or "").strip()]
    todos: list[dict[str, Any]] = []
    for block in block_list:
        text = str(block.get("text") or "").strip()
        if not text or not (int(block.get("block_type", 0) or 0) == 13 or "负责" in text or "截止" in text):
            continue
        assignee = _assignee(text)
        due = _due_date(text)
        todo_id = f"todo-block-{int(block['index'])}"
        reasons: list[str] = []
        if not assignee:
            reasons.append("assignee_missing")
        if not due:
            reasons.append("due_date_missing")
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            reasons.append("relative_date_not_normalized")
        todos.append({
            "todo_id": todo_id,
            "title": text,
            "assignee_candidate": {"display_name": assignee, "feishu_open_id": None, "status": "candidate"},
            "due_date_candidate": {
                "raw_text": due,
                "normalized_date": due if due and not reasons else None,
                "status": "normalized" if due and not reasons else "needs_context",
            },
            "evidence_block_ids": [int(block["index"])],
            "needs_confirmation": bool(reasons),
            "confirmation_reasons": reasons,
        })
    return {
        "schema_version": "meeting-agent.v1",
        "source": {"document_id_hash": document_id_hash, "revision_id": str(revision_id), "source_block_ids": source_ids},
        "summary": {"text": None, "evidence_block_ids": []},
        "decisions": [],
        "todos": todos,
        "relations": [],
        "agent_notes": "deterministic degraded-mode candidate extraction; semantic confirmation required",
    }

