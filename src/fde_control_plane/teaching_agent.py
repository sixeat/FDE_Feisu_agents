"""Read-only teaching extraction contract; semantic claims remain human candidates."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError


class TeachingContractError(ValueError):
    """A source snapshot or model candidate failed the teaching contract."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _SourceRef(_StrictModel):
    source_id: StrictStr
    revision_id: StrictStr
    content_hash: StrictStr
    kind: Literal["SYLLABUS", "WEEKLY_PLAN", "LESSON_PLAN"]


class _Item(_StrictModel):
    item_id: StrictStr
    kind: Literal["OBJECTIVE", "WEEK_PLAN", "LESSON_FRAGMENT"]
    week_number: StrictInt | None
    block_index: StrictInt
    start_offset: StrictInt
    end_offset: StrictInt
    quote: StrictStr


class _Output(_StrictModel):
    schema_version: Literal["teaching-source-agent.v1"]
    source: _SourceRef
    items: list[_Item]


def _source_ref(source: dict[str, Any]) -> _SourceRef:
    if not isinstance(source, dict):
        raise TeachingContractError("source identity is invalid")
    try:
        ref = _SourceRef.model_validate({key: source[key] for key in _SourceRef.model_fields})
    except (KeyError, ValidationError) as exc:
        raise TeachingContractError("source identity is invalid") from exc
    if not all((ref.source_id, ref.revision_id)) or not re.fullmatch(r"[0-9a-f]{64}", ref.content_hash):
        raise TeachingContractError("source identity is invalid")
    blocks = source.get("blocks")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 1000:
        raise TeachingContractError("source blocks are invalid")
    for index, block in enumerate(blocks, 1):
        if (not isinstance(block, dict) or set(block) != {"index", "block_type", "text"}
                or type(block["index"]) is not int or block["index"] != index
                or type(block["block_type"]) is not int or not isinstance(block["text"], str)):
            raise TeachingContractError("source blocks are invalid")
    encoded = json.dumps(blocks, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 1_100_000 or hashlib.sha256(encoded).hexdigest() != ref.content_hash:
        raise TeachingContractError("source content hash does not match blocks")
    return ref


def validate_teaching_source_input(source: dict[str, Any]) -> None:
    """Require a pinned, self-consistent source before sending text to a model."""
    _source_ref(source)


def validate_teaching_source_output(source: dict[str, Any], payload: Any) -> dict[str, Any]:
    """Check schema, pinned source, exact citations and bounded output.

    Passing this check proves neither semantic completeness nor correctness.
    """
    ref = _source_ref(source)
    try:
        output = _Output.model_validate(payload)
    except ValidationError as exc:
        raise TeachingContractError("teaching output schema is invalid") from exc
    if output.source != ref or len(output.items) > 100:
        raise TeachingContractError("output source or item count is invalid")
    expected_kind = {"SYLLABUS": "OBJECTIVE", "WEEKLY_PLAN": "WEEK_PLAN",
                     "LESSON_PLAN": "LESSON_FRAGMENT"}[ref.kind]
    seen: set[str] = set()
    for item in output.items:
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", item.item_id)
                or item.item_id in seen or item.kind != expected_kind):
            raise TeachingContractError("item identity or kind is invalid")
        seen.add(item.item_id)
        if (item.week_number is not None and not 1 <= item.week_number <= 60
                or (item.kind == "WEEK_PLAN" and item.week_number is None)
                or (item.kind == "OBJECTIVE" and item.week_number is not None)):
            raise TeachingContractError("item week is invalid")
        if (not 1 <= item.block_index <= len(source["blocks"])
                or not item.quote or not 0 <= item.start_offset < item.end_offset):
            raise TeachingContractError("item citation is invalid")
        block_text = source["blocks"][item.block_index - 1]["text"]
        if block_text[item.start_offset:item.end_offset] != item.quote:
            raise TeachingContractError("item citation does not match source text")
    return output.model_dump()


__all__ = ["TeachingContractError", "validate_teaching_source_input",
           "validate_teaching_source_output"]
