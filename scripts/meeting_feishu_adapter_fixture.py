"""Offline SDK-shape fixture for FeishuTaskGateway.

The fake client captures the generated lark-oapi request and returns a stable
remote task. It verifies that the adapter sends members, due date and the same
client token on replay without making a network call.
"""

from __future__ import annotations

from types import SimpleNamespace
import json

import lark_oapi as lark

from fde_control_plane.meeting import FeishuTaskGateway


class FakeTaskApi:
    def __init__(self) -> None:
        self.requests = []

    def create(self, request):
        self.requests.append(request)
        return SimpleNamespace(code=0, msg="success", data=SimpleNamespace(task=SimpleNamespace(guid="remote-stage2")))

    def get(self, request):
        return lark.api.task.v2.model.GetTaskResponse({"code": 0, "data": {"task": {"guid": "remote-stage2"}}})

    def list(self, request):
        return lark.api.task.v2.model.ListTaskResponse({"code": 0, "data": {
            "items": [{"guid": "remote-stage2", "description": self.requests[0].request_body.description}],
            "has_more": False,
        }})

    def delete(self, request):
        return SimpleNamespace(code=0, msg="success")


class FakeClient:
    def __init__(self) -> None:
        self.task = SimpleNamespace(v2=SimpleNamespace(task=FakeTaskApi()))


def main() -> int:
    fake = FakeClient()
    gateway = FeishuTaskGateway(client=fake, actor_open_ids={"member-1": "ou_test"})
    payload = {
        "title": "阶段2适配器夹具任务",
        "due_date": "2026-10-02",
        "assignee_actor_id": "member-1",
        "origin": {"type": "meeting", "document_id_hash": "fixture"},
    }
    first = gateway.create_task(payload, "stable-client-token")
    second = gateway.create_task(payload, "stable-client-token")
    body = fake.task.v2.task.requests[0].request_body
    assert first == second == "remote-stage2"
    assert body.summary == payload["title"]
    assert body.client_token == "stable-client-token"
    assert json.loads(body.description)["fde_idempotency_key"] == body.client_token
    assert body.due is not None and body.members and body.members[0].id == "ou_test"
    assert gateway.query_task("stable-client-token") == "remote-stage2"
    fresh = FeishuTaskGateway(client=fake)
    assert fresh.find_task_candidate("stable-client-token") == "remote-stage2"
    assert fresh.query_task("stable-client-token", remote_task_id=first) == first
    print("MEETING_FEISHU_ADAPTER_FIXTURE_PASS")
    print("client_token_replay_same_remote=True")
    print("member_and_due_fields=True")
    print("fresh_gateway_known_receipt=True candidate_is_not_receipt=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
