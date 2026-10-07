"""Controlled meeting-artifact parsing probe for phase 0 V-06.

This is a deterministic parser for the fictional test document. It validates
the boundary between Feishu block reading and structured meeting output before
an LLM is introduced. By default it prints hashes for extracted fields. Set
FEISHU_MEETING_PARSE_SHOW_TEXT=1 only for a local fictional test document to
display parsed values; never use that switch for real meeting data.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark


TEXT_FIELDS = (
    "page", "text", "heading1", "heading2", "heading3", "heading4",
    "heading5", "heading6", "heading7", "heading8", "heading9",
    "bullet", "ordered", "code", "quote", "equation", "todo",
)
NAME = r"[\u4e00-\u9fff]{2,4}"


@dataclass
class SourceBlock:
    index: int
    block_type: int | None
    text: str


def short_hash(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def block_text(block: Any) -> str:
    parts: list[str] = []
    for field_name in TEXT_FIELDS:
        field = getattr(block, field_name, None)
        for element in getattr(field, "elements", None) or []:
            run = getattr(element, "text_run", None)
            content = getattr(run, "content", None)
            if content:
                parts.append(content)
    return "".join(parts).strip()


def extract_assignee(text: str) -> str | None:
    # Prefer the explicit "由某人负责" form. The boundary on the fallback
    # prevents a greedy match such as "调由李四" in "接口联调由李四负责".
    match = re.search(rf"由\s*({NAME})\s*负责", text)
    if match:
        return match.group(1)
    match = re.search(rf"(?<![\u4e00-\u9fff])({NAME})\s*负责", text)
    return match.group(1) if match else None


def extract_due_date(text: str) -> str | None:
    match = re.search(r"截止(?:日期|时间)?为?\s*([^，。；;]+)", text)
    return match.group(1).strip() if match else None


def is_todo_candidate(block: SourceBlock) -> bool:
    return block.block_type == 13 or "负责" in block.text or "截止" in block.text


def main() -> int:
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    document_id = os.environ.get("FEISHU_DOCUMENT_ID")
    if not app_id or not app_secret or not document_id:
        print(
            "缺少 FEISHU_APP_ID、FEISHU_APP_SECRET 或 FEISHU_DOCUMENT_ID；"
            "只在本机环境变量中设置，不要粘贴到聊天。",
            file=sys.stderr,
        )
        return 2

    try:
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        doc_request = (
            lark.api.docx.v1.model.GetDocumentRequest.builder()
            .document_id(document_id)
            .build()
        )
        doc_response = client.docx.v1.document.get(doc_request)
        if getattr(doc_response, "code", None) != 0:
            print(
                f"MEETING_PARSE_META code={getattr(doc_response, 'code', None)} "
                f"msg={getattr(doc_response, 'msg', None)}",
                flush=True,
            )
            return 1
        document = getattr(getattr(doc_response, "data", None), "document", None)
        revision_id = getattr(document, "revision_id", None)

        block_request = (
            lark.api.docx.v1.model.GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(document_id)
            .page_size(500)
            .with_descendants(True)
            .build()
        )
        block_response = client.docx.v1.document_block_children.get(block_request)
        if getattr(block_response, "code", None) != 0:
            print(
                f"MEETING_PARSE_BLOCKS code={getattr(block_response, 'code', None)} "
                f"msg={getattr(block_response, 'msg', None)}",
                flush=True,
            )
            return 1

        items = getattr(getattr(block_response, "data", None), "items", None) or []
        blocks = [
            SourceBlock(i, getattr(block, "block_type", None), block_text(block))
            for i, block in enumerate(items, start=1)
        ]
        candidates = [block for block in blocks if block.text and is_todo_candidate(block)]
        todos = []
        for block in candidates:
            assignee = extract_assignee(block.text)
            due_date = extract_due_date(block.text)
            todos.append((block, assignee, due_date))

        print(
            "MEETING_PARSE_SUMMARY "
            f"code=0 revision_id={revision_id} block_count={len(blocks)} "
            f"candidate_count={len(todos)} document_id_hash={short_hash(document_id)}",
            flush=True,
        )
        show_text = os.environ.get("FEISHU_MEETING_PARSE_SHOW_TEXT") == "1"
        for number, (block, assignee, due_date) in enumerate(todos, start=1):
            if show_text:
                print(
                    "MEETING_TODO "
                    f"index={number} source_block={block.index} "
                    f"text={block.text} "
                    f"assignee={assignee or '待确认'} "
                    f"due_date={due_date or '待确认'}",
                    flush=True,
                )
            else:
                print(
                    "MEETING_TODO "
                    f"index={number} source_block={block.index} "
                    f"text_hash={short_hash(block.text)} "
                    f"assignee_hash={short_hash(assignee)} "
                    f"due_date_hash={short_hash(due_date)} "
                    f"needs_confirmation={not assignee or not due_date}",
                    flush=True,
                )

        return 0
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"MEETING_PARSE_ERROR: {type(exc).__name__}: {str(exc)[:160]}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
