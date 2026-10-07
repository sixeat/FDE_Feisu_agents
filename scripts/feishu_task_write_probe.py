"""Controlled Feishu task write probe for phase 0.

The probe is dry-run by default. Setting FEISHU_TASK_WRITE=1 explicitly enables
one test task creation with a stable client token, followed by a task query.
Setting FEISHU_TASK_REPLAY=1 sends the same create request a second time so the
remote idempotency behavior can be observed. It never prints the full remote
task ID or credentials.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid

import lark_oapi as lark


TEST_CLIENT_TOKEN = str(uuid.uuid5(uuid.NAMESPACE_URL, "fde-phase0:test-task:replay-beta-20260926"))


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

    print(
        "TASK_WRITE_PLAN "
        "summary=Phase0幂等测试任务-Beta "
        f"client_token_hash={short_hash(TEST_CLIENT_TOKEN)}",
        flush=True,
    )
    if os.environ.get("FEISHU_TASK_WRITE") != "1":
        print("TASK_WRITE_MODE=DRY_RUN set FEISHU_TASK_WRITE=1 to enable one test write")
        return 0

    try:
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        task = (
            lark.api.task.v2.model.InputTask.builder()
            .summary("Phase0幂等测试任务-Beta")
            .description("阶段0远端 client_token 幂等验证任务；验证后可在飞书任务中删除或归档。")
            .client_token(TEST_CLIENT_TOKEN)
            .build()
        )
        request = (
            lark.api.task.v2.model.CreateTaskRequest.builder()
            .request_body(task)
            .build()
        )
        response = client.task.v2.task.create(request)
        print(
            f"TASK_CREATE_RESULT code={getattr(response, 'code', None)} "
            f"msg={getattr(response, 'msg', None)}",
            flush=True,
        )
        if getattr(response, "code", None) != 0:
            return 1

        remote_task = getattr(getattr(response, "data", None), "task", None)
        task_guid = getattr(remote_task, "guid", None)
        print(f"TASK_REMOTE_ID_HASH={short_hash(task_guid)}", flush=True)
        if not task_guid:
            print("TASK_RECONCILIATION_REQUIRED missing_remote_guid", file=sys.stderr)
            return 1

        if os.environ.get("FEISHU_TASK_REPLAY") == "1":
            replay_response = client.task.v2.task.create(request)
            replay_task = getattr(getattr(replay_response, "data", None), "task", None)
            replay_guid = getattr(replay_task, "guid", None)
            print(
                f"TASK_REPLAY_RESULT code={getattr(replay_response, 'code', None)} "
                f"msg={getattr(replay_response, 'msg', None)} "
                f"remote_id_hash={short_hash(replay_guid)}",
                flush=True,
            )
            if getattr(replay_response, "code", None) != 0:
                print("TASK_REPLAY_CONCLUSION=REMOTE_REJECTED_OR_NON_IDEMPOTENT", flush=True)
            elif replay_guid == task_guid:
                print("TASK_REPLAY_CONCLUSION=SAME_REMOTE_TASK", flush=True)
            else:
                print("TASK_REPLAY_CONCLUSION=DUPLICATE_REMOTE_TASK", flush=True)

        query = (
            lark.api.task.v2.model.GetTaskRequest.builder()
            .task_guid(task_guid)
            .build()
        )
        query_response = client.task.v2.task.get(query)
        print(
            f"TASK_QUERY_RESULT code={getattr(query_response, 'code', None)} "
            f"msg={getattr(query_response, 'msg', None)}",
            flush=True,
        )
        return 0 if getattr(query_response, "code", None) == 0 else 1
    except Exception as exc:  # pragma: no cover - real tenant path
        print(
            f"TASK_WRITE_UNKNOWN: {type(exc).__name__}: {str(exc)[:160]}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
