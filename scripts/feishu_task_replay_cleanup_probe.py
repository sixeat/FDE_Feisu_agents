"""Recover and optionally delete the Beta idempotency test task.

The task was created without an assignee, so it may not appear in a member's
task list. Replaying its stable client token lets the application recover the
remote GUID without printing it. Deletion is disabled unless the explicit
FEISHU_TASK_DELETE=1 switch is set.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid

import lark_oapi as lark


SUMMARY = "Phase0幂等测试任务-Beta"
DESCRIPTION = "阶段0远端 client_token 幂等验证任务；验证后可在飞书任务中删除或归档。"
CLIENT_TOKEN = str(uuid.uuid5(uuid.NAMESPACE_URL, "fde-phase0:test-task:replay-beta-20260926"))


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
        task = (
            lark.api.task.v2.model.InputTask.builder()
            .summary(SUMMARY)
            .description(DESCRIPTION)
            .client_token(CLIENT_TOKEN)
            .build()
        )
        request = (
            lark.api.task.v2.model.CreateTaskRequest.builder()
            .request_body(task)
            .build()
        )
        response = client.task.v2.task.create(request)
        remote_task = getattr(getattr(response, "data", None), "task", None)
        task_guid = getattr(remote_task, "guid", None)
        print(
            f"TASK_RECOVER_RESULT code={getattr(response, 'code', None)} "
            f"msg={getattr(response, 'msg', None)} "
            f"remote_id_hash={short_hash(task_guid)}",
            flush=True,
        )
        if getattr(response, "code", None) != 0 or not task_guid:
            return 1

        if os.environ.get("FEISHU_TASK_DELETE") != "1":
            print(
                "TASK_DELETE_MODE=DRY_RUN set FEISHU_TASK_DELETE=1 to delete this exact recovered task",
                flush=True,
            )
            return 0

        delete_request = (
            lark.api.task.v2.model.DeleteTaskRequest.builder()
            .task_guid(task_guid)
            .build()
        )
        delete_response = client.task.v2.task.delete(delete_request)
        print(
            f"TASK_DELETE_RESULT code={getattr(delete_response, 'code', None)} "
            f"msg={getattr(delete_response, 'msg', None)} "
            f"remote_id_hash={short_hash(task_guid)}",
            flush=True,
        )
        return 0 if getattr(delete_response, "code", None) == 0 else 1
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"TASK_CLEANUP_UNKNOWN: {type(exc).__name__}: {str(exc)[:160]}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
