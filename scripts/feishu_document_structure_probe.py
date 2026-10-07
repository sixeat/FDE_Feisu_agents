"""Read-only Feishu document structure probe for V-06.

Outputs block order, block type, text length, and text hashes only. It never
prints document title, body text, credentials, or complete identifiers.
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import Any

import lark_oapi as lark


TEXT_FIELDS = (
    "page", "text", "heading1", "heading2", "heading3", "heading4",
    "heading5", "heading6", "heading7", "heading8", "heading9",
    "bullet", "ordered", "code", "quote", "equation", "todo",
)


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
    return "".join(parts)


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
        request = (
            lark.api.docx.v1.model.GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(document_id)
            .page_size(500)
            .with_descendants(True)
            .build()
        )
        response = client.docx.v1.document_block_children.get(request)
        print(
            "DOCUMENT_STRUCTURE_META "
            f"code={getattr(response, 'code', None)} "
            f"msg={getattr(response, 'msg', None)} "
            f"document_id_hash={short_hash(document_id)}",
            flush=True,
        )
        if getattr(response, "code", None) != 0:
            return 1

        data = getattr(response, "data", None)
        blocks = getattr(data, "items", None) or []
        print(
            f"DOCUMENT_STRUCTURE_COUNT count={len(blocks)} "
            f"has_more={getattr(data, 'has_more', None)}",
            flush=True,
        )
        for index, block in enumerate(blocks, start=1):
            content = block_text(block)
            print(
                "DOCUMENT_BLOCK "
                f"index={index} type={getattr(block, 'block_type', None)} "
                f"text_length={len(content)} text_hash={short_hash(content)}",
                flush=True,
            )
        return 0
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"DOCUMENT_STRUCTURE_ERROR: {type(exc).__name__}: {str(exc)[:160]}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
