"""Find and optionally delete the controlled phase-0 test task.

The probe lists tasks with the application identity and matches one exact test
summary. It is read-only by default; FEISHU_TASK_DELETE=1 is required before
deleting matches. It never prints full task IDs or credentials.
"""

from __future__ import annotations

import hashlib
import os
import sys

import lark_oapi as lark


TARGET_SUMMARY = "Phase0测试任务-Alpha"


def short_hash(value: object) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def main() -> int:
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print(
            "缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET；只在本机环境变量中设置，不要粘贴到聊天。",
            file=sys.stderr,
        )
        return 2

    try:
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        page_token = None
        matches = []
        while True:
            builder = lark.api.task.v2.model.ListTaskRequest.builder().page_size(50)
            if page_token:
                builder.page_token(page_token)
            response = client.task.v2.task.list(builder.build())
            print(
                f"TASK_LIST_PAGE code={getattr(response, 'code', None)} "
                f"msg={getattr(response, 'msg', None)}",
                flush=True,
            )
            if getattr(response, "code", None) != 0:
                return 1
            data = getattr(response, "data", None)
            for task in getattr(data, "items", None) or []:
                if getattr(task, "summary", None) == TARGET_SUMMARY:
                    matches.append(task)
            if not getattr(data, "has_more", False):
                break
            page_token = getattr(data, "page_token", None)
            if not page_token:
                break

        print(f"TASK_MATCH_COUNT={len(matches)}", flush=True)
        for task in matches:
            guid = getattr(task, "guid", None)
            print(
                "TASK_MATCH "
                f"guid_hash={short_hash(guid)} "
                f"status={getattr(task, 'status', None)}",
                flush=True,
            )

        if os.environ.get("FEISHU_TASK_DELETE") != "1":
            print("TASK_DELETE_MODE=DRY_RUN set FEISHU_TASK_DELETE=1 to delete exact matches")
            return 0

        for task in matches:
            guid = getattr(task, "guid", None)
            if not guid:
                continue
            request = (
                lark.api.task.v2.model.DeleteTaskRequest.builder()
                .task_guid(guid)
                .build()
            )
            response = client.task.v2.task.delete(request)
            print(
                f"TASK_DELETE_RESULT code={getattr(response, 'code', None)} "
                f"msg={getattr(response, 'msg', None)} guid_hash={short_hash(guid)}",
                flush=True,
            )
        return 0
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"TASK_CLEANUP_ERROR: {type(exc).__name__}: {str(exc)[:160]}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
