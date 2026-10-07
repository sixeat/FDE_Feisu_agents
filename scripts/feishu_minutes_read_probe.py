"""Read a single Feishu Minutes record without printing its contents or IDs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import lark_oapi as lark


class ProbeInputError(ValueError):
    """A safe, user-facing input or source-document error."""


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def required_scopes(response: object) -> list[str]:
    message = str(getattr(response, "msg", "") or "")
    return sorted(set(re.findall(r"\b[a-z][a-z0-9_.]*:[a-z0-9_.:]+\b", message)))


def error_hint(response: object) -> str:
    message = str(getattr(response, "msg", "") or "").lower()
    if any(word in message for word in ("access", "permission", "forbidden", "not allowed", "无权限", "没有权限", "无权", "未授权", "访问受限")):
        return "ACCESS_DENIED"
    if any(word in message for word in ("not found", "not exist", "does not exist", "不存在", "未找到")):
        return "NOT_FOUND"
    if any(word in message for word in ("invalid", "illegal", "无效", "非法")):
        return "INVALID_INPUT"
    return "UNCLASSIFIED"


def minute_token_from_input(value: str) -> str:
    if not value.startswith(("http://", "https://")):
        return value
    parsed = urlsplit(value)
    if not parsed.hostname or not any(
        parsed.hostname == domain or parsed.hostname.endswith("." + domain)
        for domain in ("feishu.cn", "larksuite.com")
    ):
        raise ProbeInputError("unsupported Minutes link host")
    query = parse_qs(parsed.query)
    for key in ("minute_token", "token"):
        if query.get(key):
            return query[key][0]
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[-2] in ("minutes", "minute"):
        return parts[-1]
    raise ProbeInputError("could not find minute token in link")


def minute_token_from_document(client: lark.Client, value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.hostname or not any(
        parsed.hostname == domain or parsed.hostname.endswith("." + domain)
        for domain in ("feishu.cn", "larksuite.com")
    ):
        raise ProbeInputError("unsupported document link host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "docx":
        raise ProbeInputError("expected a Feishu docx link")
    document_id = parts[1]
    tokens: set[str] = set()
    page_token = None
    for _ in range(20):
        builder = (
            lark.api.docx.v1.model.GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(document_id)
            .page_size(500)
            .with_descendants(True)
        )
        if page_token:
            builder = builder.page_token(page_token)
        response = client.docx.v1.document_block_children.get(builder.build())
        print(f"MINUTES_SOURCE_DOCUMENT code={response.code} document_hash={short_hash(document_id)}", flush=True)
        if response.code != 0:
            print(f"MINUTES_SOURCE_REQUIRED_SCOPES {required_scopes(response)}", flush=True)
            raise ProbeInputError("source document could not be read")
        data = response.data
        for block in getattr(data, "items", None) or []:
            for field_name in (
                "page", "text", "heading1", "heading2", "heading3", "heading4",
                "heading5", "heading6", "heading7", "heading8", "heading9",
                "bullet", "ordered", "todo", "quote",
            ):
                field = getattr(block, field_name, None)
                for element in getattr(field, "elements", None) or []:
                    run = getattr(element, "text_run", None)
                    style = getattr(run, "text_element_style", None)
                    link = getattr(style, "link", None)
                    url = getattr(link, "url", None)
                    if url and "/minutes/" in urlsplit(url).path:
                        tokens.add(minute_token_from_input(url))
        if not getattr(data, "has_more", False):
            break
        next_page = getattr(data, "page_token", None)
        if not next_page or next_page == page_token:
            raise ProbeInputError("source document pagination could not advance")
        page_token = next_page
    else:
        raise ProbeInputError("source document exceeded pagination limit")
    print(f"MINUTES_SOURCE_LINKS unique_count={len(tokens)}", flush=True)
    if len(tokens) != 1:
        raise ProbeInputError("source document must contain exactly one distinct Minutes link")
    return next(iter(tokens))


def main() -> int:
    config_path = Path(
        os.environ.get("FEISHU_MINUTES_CONFIG", "D:/Temp/feishu-minutes.local.json")
    )
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            print("MINUTES_CONFIG_ERROR: invalid local JSON file", file=sys.stderr)
            return 2
        if not isinstance(config, dict):
            print("MINUTES_CONFIG_ERROR: expected a JSON object", file=sys.stderr)
            return 2

    app_id = config.get("app_id") or os.environ.get("FEISHU_APP_ID")
    app_secret = config.get("app_secret") or os.environ.get("FEISHU_APP_SECRET")
    minute_input = config.get("minute_token") or os.environ.get("FEISHU_MINUTE_TOKEN")
    if not all((app_id, app_secret)):
        print(
            "Fill app_id and app_secret in the local config file, "
            "or set matching FEISHU_* environment variables.",
            file=sys.stderr,
        )
        return 2

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    try:
        if not minute_input:
            search_request = (
                lark.api.minutes.v1.model.SearchMinuteRequest.builder()
                .page_size(10)
                .request_body(lark.api.minutes.v1.model.SearchMinuteRequestBody.builder().build())
                .build()
            )
            search = client.minutes.v1.minute.search(search_request)
            print(f"MINUTES_SEARCH code={search.code}", flush=True)
            if search.code != 0:
                print(f"MINUTES_REQUIRED_SCOPES {required_scopes(search)}", flush=True)
                return 1
            data = search.data
            items = getattr(data, "items", None) or []
            print(
                "MINUTES_SEARCH_SUMMARY "
                f"total={getattr(data, 'total', None)} "
                f"returned={len(items)} has_more={getattr(data, 'has_more', None)}",
                flush=True,
            )
            return 0

        minute_token = (
            minute_token_from_document(client, minute_input)
            if minute_input.startswith(("http://", "https://"))
            and urlsplit(minute_input).path.startswith("/docx/")
            else minute_token_from_input(minute_input)
        )
        token_hash = short_hash(minute_token)
        detail_request = (
            lark.api.minutes.v1.model.GetMinuteRequest.builder()
            .minute_token(minute_token)
            .build()
        )
        detail = client.minutes.v1.minute.get(detail_request)
        print(f"MINUTES_DETAIL code={detail.code} token_hash={token_hash}", flush=True)
        if detail.code != 0:
            print(
                f"MINUTES_DETAIL_ERROR hint={error_hint(detail)} "
                f"required_scopes={required_scopes(detail)} msg_hash={short_hash(str(detail.msg or ''))}",
                flush=True,
            )

        transcript_request = (
            lark.api.minutes.v1.model.GetMinuteTranscriptRequest.builder()
            .minute_token(minute_token)
            .need_speaker(True)
            .need_timestamp(True)
            .build()
        )
        transcript = client.minutes.v1.minute_transcript.get(transcript_request)
        print(f"MINUTES_TRANSCRIPT code={transcript.code}", flush=True)
        if transcript.code != 0:
            print(
                f"MINUTES_TRANSCRIPT_ERROR hint={error_hint(transcript)} "
                f"required_scopes={required_scopes(transcript)} msg_hash={short_hash(str(transcript.msg or ''))}",
                flush=True,
            )
            return 1

        payload = transcript.file.getvalue() if transcript.file else b""
        print(
            "MINUTES_TRANSCRIPT_SUMMARY "
            f"byte_count={len(payload)} content_hash={hashlib.sha256(payload).hexdigest()[:12]} "
            f"file_name_hash={short_hash(transcript.file_name) if transcript.file_name else None}",
            flush=True,
        )
        return 0 if detail.code == 0 and payload else 1
    except ProbeInputError as exc:
        print(f"MINUTES_PROBE_INPUT_ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - requires a live tenant
        print(f"MINUTES_PROBE_ERROR type={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
