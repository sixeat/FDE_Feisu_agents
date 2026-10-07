"""Read-only Feishu document probe for phase 0.

This is a disposable verification helper, not the product runtime. It reads
one document and its root children, then prints only status and aggregate
metadata. It never prints document title, body text, credentials, or full IDs.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections import Counter
from typing import Any

import lark_oapi as lark


def short_hash(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def text_length(block: Any) -> int:
    total = 0
    for field_name in (
        "page",
        "text",
        "heading1",
        "heading2",
        "heading3",
        "heading4",
        "heading5",
        "heading6",
        "heading7",
        "heading8",
        "heading9",
        "bullet",
        "ordered",
        "code",
        "quote",
        "equation",
        "todo",
    ):
        field = getattr(block, field_name, None)
        for element in getattr(field, "elements", None) or []:
            run = getattr(element, "text_run", None)
            total += len(getattr(run, "content", None) or "")
    return total


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
        document_request = (
            lark.api.docx.v1.model.GetDocumentRequest.builder()
            .document_id(document_id)
            .build()
        )
        document_response = client.docx.v1.document.get(document_request)
        print(
            "DOCUMENT_META "
            f"code={getattr(document_response, 'code', None)} "
            f"msg={getattr(document_response, 'msg', None)} "
            f"document_id_hash={short_hash(document_id)}",
            flush=True,
        )
        if getattr(document_response, "code", None) != 0:
            return 1

        document = getattr(getattr(document_response, "data", None), "document", None)
        revision_id = getattr(document, "revision_id", None)
        title = getattr(document, "title", None) or ""
        print(
            "DOCUMENT_SUMMARY "
            f"revision_id={revision_id} title_length={len(title)}",
            flush=True,
        )

        children_request = (
            lark.api.docx.v1.model.GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(document_id)
            .page_size(500)
            .with_descendants(True)
            .build()
        )
        children_response = client.docx.v1.document_block_children.get(children_request)
        print(
            "DOCUMENT_BLOCKS "
            f"code={getattr(children_response, 'code', None)} "
            f"msg={getattr(children_response, 'msg', None)}",
            flush=True,
        )
        if getattr(children_response, "code", None) != 0:
            return 1

        body = getattr(children_response, "data", None)
        blocks = getattr(body, "items", None) or []
        block_types = Counter(
            str(getattr(block, "block_type", None)) for block in blocks
        )
        aggregate_length = sum(text_length(block) for block in blocks)
        print(
            "DOCUMENT_CONTENT_SUMMARY "
            f"block_count={len(blocks)} text_length={aggregate_length} "
            f"block_types={dict(sorted(block_types.items()))} "
            f"has_more={getattr(body, 'has_more', None)}",
            flush=True,
        )
        return 0
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"DOCUMENT_PROBE_ERROR: {type(exc).__name__}: {str(exc)[:160]}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
