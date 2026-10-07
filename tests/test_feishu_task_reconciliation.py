"""Use real SDK request/response shapes with a network-free task service."""
import json
from types import SimpleNamespace

import lark_oapi as lark
import pytest

from fde_control_plane.meeting import FeishuTaskGateway, TaskLookupUncertain

model = lark.api.task.v2.model
KEY = "fixture-write-key"


def task(guid="remote-1", description=None):
    return {"guid": guid, "description": description if description is not None else json.dumps({"fde_idempotency_key": KEY})}


def page(items=None, has_more=False, page_token=None):
    return model.ListTaskResponse({"code": 0, "data": {
        "items": items or [], "has_more": has_more, "page_token": page_token,
    }})


class TaskApi:
    def __init__(self, pages, get_result=None):
        self.pages = iter(pages)
        self.requests = []
        self.get_result = get_result or model.GetTaskResponse({"code": 0, "data": {"task": task()}})

    def list(self, request):
        assert isinstance(request, model.ListTaskRequest)
        self.requests.append(request)
        return next(self.pages)

    def get(self, request):
        assert isinstance(request, model.GetTaskRequest)
        self.requests.append(request)
        return self.get_result

    def create(self, request):
        pytest.fail("reconciliation must never create a task")


def gateway(pages, get_result=None, **options):
    api = TaskApi(pages, get_result)
    return FeishuTaskGateway(client=SimpleNamespace(task=SimpleNamespace(v2=SimpleNamespace(task=api))), **options), api


def test_fresh_gateway_reads_persisted_remote_receipt_without_list():
    gw, api = gateway([])
    assert gw.query_task(KEY, remote_task_id="remote-1") == "remote-1"
    assert len(api.requests) == 1 and api.requests[0].task_guid == "remote-1"


def test_candidate_requires_complete_scan_and_is_not_a_trusted_receipt():
    gw, api = gateway([page([task()], True, "next"), page([])])
    with pytest.raises(TaskLookupUncertain, match="REMOTE_CANDIDATE_REQUIRES_REVIEW"):
        gw.query_task(KEY)
    assert len(api.requests) == 2 and api.requests[1].page_token == "next"
    assert gw._remote_by_key == {}


def test_marker_is_emitted_by_real_sdk_builder():
    gw, _ = gateway([])
    body = gw._build_task_builder(lark, {"title": "测试", "due_date": "2026-10-12"}, KEY).build()
    assert json.loads(body.description)["fde_idempotency_key"] == body.client_token == KEY


@pytest.mark.parametrize("description", ['[]', 'null', '7', '"text"', 'invalid json', '{}', '{"fde_idempotency_key":"other"}'])
def test_unrelated_or_non_object_descriptions_are_not_matches(description):
    gw, _ = gateway([page([task(description=description)])])
    assert gw.query_task(KEY) is None


@pytest.mark.parametrize("pages, options, reason", [
    ([page([task()], True, "next"), page([task("remote-2")])], {}, "REMOTE_MULTIPLE_MATCHES"),
    ([page([task()], True, "next")], {"max_lookup_pages": 1}, "REMOTE_SCAN_LIMIT"),
    ([page([], True, "a"), page([], True, "b"), page([], True, "a")], {}, "REMOTE_PAGINATION_INVALID"),
    ([page([], True, None)], {}, "REMOTE_PAGINATION_INVALID"),
    ([model.ListTaskResponse({"code": 0})], {}, "REMOTE_LIST_INVALID"),
    ([model.ListTaskResponse({"code": 99991672, "msg": "sensitive URL"})], {}, "REMOTE_LIST_FAILED"),
    ([page([task(guid="")])], {}, "REMOTE_LIST_INVALID"),
])
def test_uncertain_list_results_never_return_a_remote_receipt(pages, options, reason):
    gw, _ = gateway(pages, **options)
    with pytest.raises(TaskLookupUncertain, match=reason):
        gw.query_task(KEY)
    assert gw._remote_by_key == {}


@pytest.mark.parametrize("response, reason", [
    (model.GetTaskResponse({"code": 403}), "REMOTE_GET_FAILED"),
    (model.GetTaskResponse({"code": 0}), "REMOTE_GET_INVALID"),
    (model.GetTaskResponse({"code": 0, "data": {"task": task("different-id")}}), "REMOTE_GET_INVALID"),
])
def test_get_requires_success_and_matching_remote_id(response, reason):
    gw, _ = gateway([], response)
    with pytest.raises(TaskLookupUncertain, match=reason):
        gw.query_task(KEY, remote_task_id="remote-1")
