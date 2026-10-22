"""Feishu-only course and fresh-source slice; all documents are synthetic."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fde_control_plane import (
    Actor, ActorType, AgentVersion, AssistantBinding, ControlPlane, IdentityContext,
    SQLiteOAuthSessionIssuer, SQLiteStore, TaskRunStatus,
)
from fde_control_plane.teaching import TeachingService
from fde_control_plane.teaching_execution import TeachingSourceExecutionService
from fde_control_plane.teaching_preparation import TeachingPreparationService
from fde_control_plane.runtime import RunEvent, RuntimeResult
from fde_control_plane.teaching_web import build_teaching_router
from fde_control_plane.user_document_access import DOC_READ_SCOPE


DOC_ID = "teaching-demo-doc"
DOC_URL = f"https://demo.feishu.cn/docx/{DOC_ID}"


class FakeUserReader:
    def __init__(self) -> None:
        self.allowed = {"admin", "lead", "teacher", "teacher2"}
        self.revision = "1"
        self.text = "第 2 周：以虚构示例讲授递归。"
        self.title = "虚构课程周计划"
        self.blocks = None
        self.calls: list[tuple[str, str]] = []
        self.change_during_read = False
        self.documents: dict[str, dict] = {}

    def read_document(self, identity, document_id):
        self.calls.append((identity.actor_id, document_id))
        if identity.actor_id not in self.allowed:
            raise PermissionError("member cannot read the Feishu document")
        if document_id in self.documents:
            return dict(self.documents[document_id])
        snapshot = {"document_id": document_id, "revision_id": self.revision, "title": self.title,
                    "blocks": self.blocks or [{"index": 1, "block_type": 2, "text": self.text}]}
        if self.change_during_read:
            self.revision = str(int(self.revision) + 1)
        return snapshot

    def get_revision(self, identity, document_id):
        self.calls.append((identity.actor_id, document_id))
        if identity.actor_id not in self.allowed:
            raise PermissionError("member cannot read the Feishu document")
        if document_id in self.documents:
            return self.documents[document_id]["revision_id"]
        return self.revision


def setup(tmp_path):
    store = SQLiteStore(tmp_path / "course.sqlite3")
    cp = ControlPlane(store)
    contexts = {}
    for name, roles, tenant in [
        ("admin", {"admin"}, "tenant"), ("lead", set(), "tenant"),
        ("teacher", set(), "tenant"), ("teacher2", set(), "tenant"),
        ("outsider", set(), "tenant"),
        ("foreign", {"admin"}, "other"),
    ]:
        subject = hashlib.sha256(f"oauth-{name}".encode()).hexdigest()
        cp.register_actor(Actor(name, tenant, ActorType.USER, roles=frozenset(roles), external_ref_hash=subject))
        contexts[name] = IdentityContext(tenant, name, "user_oauth", subject, frozenset({DOC_READ_SCOPE}))
    sessions = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant")
    reader = FakeUserReader()
    app = FastAPI()
    app.include_router(build_teaching_router(TeachingService(cp, reader), sessions,
                                              public_origin="https://example.test"))
    client = TestClient(app)

    def login(name):
        client.cookies.set("fde_auth_session", sessions(contexts[name]))
        token = client.get("/api/teaching/courses").json()["csrf_token"]
        return {"X-CSRF-Token": token, "Origin": "https://example.test"}

    return cp, reader, client, contexts, login


def teaching_handoff_fixture(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    caps = frozenset({"doc.read"})
    cp.register_actor(Actor("assistant", "tenant", ActorType.PERSONAL_ASSISTANT, caps))
    cp.register_actor(Actor("source-agent", "tenant", ActorType.BUSINESS_AGENT, caps))
    cp.register_agent_version(AgentVersion("source-agent", 1, caps))
    cp.bind_assistant(AssistantBinding("lead-binding", "tenant", "lead", "assistant"))
    cp.register_actor(Actor("lead", "tenant", ActorType.USER, caps,
                            external_ref_hash=contexts["lead"].subject_ref_hash))
    create_course(client, login("admin"))
    login("lead")
    source_id = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "SYLLABUS", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=login("lead")).json()["source_id"]
    outline_id = client.post(f"/api/teaching/courses/demo/sources/{source_id}/outlines", json={
        "items": [{"item_id": "objective-1", "kind": "OBJECTIVE", "block_index": 1,
                   "quote": "递归", "week_number": None}],
    }, headers=login("lead")).json()["outline_id"]
    return cp, reader, client, contexts, source_id, outline_id


def teaching_alignment_fixture(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    create_course(client, login("admin"))
    documents = {
        "course-syllabus": ("SYLLABUS", ["理解递归", "分析算法复杂度"],
                            [{"item_id": "objective-1", "kind": "OBJECTIVE", "block_index": 1,
                              "quote": "理解递归"},
                             {"item_id": "objective-2", "kind": "OBJECTIVE", "block_index": 2,
                              "quote": "分析算法复杂度"}], []),
        "course-weekly": ("WEEKLY_PLAN", ["第 1 周：递归定义", "第 2 周：递归练习"],
                          [{"item_id": "week-1", "kind": "WEEK_PLAN", "block_index": 1,
                            "quote": "递归定义", "week_number": 1},
                           {"item_id": "week-2", "kind": "WEEK_PLAN", "block_index": 2,
                            "quote": "递归练习", "week_number": 2}], [1, 2, 3]),
        "course-lesson": ("LESSON_PLAN", ["第 1 周：递归示例", "第 4 周：树结构示例"],
                          [{"item_id": "lesson-1", "kind": "LESSON_FRAGMENT", "block_index": 1,
                            "quote": "递归示例", "week_number": 1},
                           {"item_id": "lesson-4", "kind": "LESSON_FRAGMENT", "block_index": 2,
                            "quote": "树结构示例", "week_number": 4}], []),
    }
    references = {}
    for document_id, (kind, texts, items, expected) in documents.items():
        reader.documents[document_id] = {
            "document_id": document_id, "revision_id": "1", "title": "虚构教研资料",
            "blocks": [{"index": index, "block_type": 2, "text": text}
                       for index, text in enumerate(texts, 1)],
        }
        source = client.post("/api/teaching/courses/demo/sources", json={
            "kind": kind, "document_url": f"https://demo.feishu.cn/docx/{document_id}",
            "attested_no_student_data": True,
        }, headers=login("lead"))
        assert source.status_code == 200
        source_id = source.json()["source_id"]
        outline = client.post(f"/api/teaching/courses/demo/sources/{source_id}/outlines", json={
            "items": items, "expected_weeks": expected,
        }, headers=login("lead"))
        assert outline.status_code == 200
        role = {"SYLLABUS": "syllabus", "WEEKLY_PLAN": "weekly_plan",
                "LESSON_PLAN": "lesson_plan"}[kind]
        references[role] = {"source_id": source_id, "outline_id": outline.json()["outline_id"]}
    body = {**references, "links": [{"objective_item_id": "objective-1", "week_item_id": "week-1"}]}
    return cp, reader, client, contexts, login, body


def test_alignment_candidates_are_cited_immutable_and_lead_reviewed(tmp_path):
    cp, reader, client, contexts, login, body = teaching_alignment_fixture(tmp_path)
    path = "/api/teaching/courses/demo/alignments"
    response = client.post(path, json=body, headers=login("lead"))
    assert response.status_code == 200
    created = response.json()
    assert created["status"] == "PENDING_REVIEW"
    assert {issue["code"] for issue in created["issues"]} == {
        "OBJECTIVE_UNLINKED", "WEEK_WITHOUT_OBJECTIVE_LINK",
        "WEEK_WITHOUT_LESSON_ANNOTATION", "LESSON_WITHOUT_WEEK_PLAN_ANNOTATION",
        "EXPECTED_WEEK_UNANNOTATED",
    }
    assert created["issues"][0]["evidence"][0]["citation"]["source_id"] == body["syllabus"]["source_id"]
    alignment_id = created["alignment_id"]
    assert client.get(path).json()["alignments"][0]["issue_count"] == 5
    saved = dict(cp.store.get_teaching_alignment("tenant", "demo", alignment_id))
    assert "理解递归" not in saved["data_json"]
    assert "树结构示例" not in saved["data_json"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable teaching alignment"):
        cp.store.connection.execute(
            "UPDATE teaching_alignments SET input_hash = 'x' WHERE alignment_id = ?", (alignment_id,)
        )
    review_path = f"{path}/{alignment_id}/review"
    accepted = client.post(review_path, json={"outcome": "ACCEPTED",
                                              "reason_code": "EVIDENCE_CONFIRMED"},
                           headers=login("lead"))
    assert accepted.status_code == 200
    with pytest.raises(sqlite3.IntegrityError, match="immutable teaching alignment review"):
        cp.store.connection.execute(
            "UPDATE teaching_alignment_reviews SET outcome = 'RETURNED' WHERE alignment_id = ?",
            (alignment_id,),
        )
    assert client.get(f"{path}/{alignment_id}").json()["status"] == "ACCEPTED"
    assert client.post(review_path, json={"outcome": "RETURNED",
                                               "reason_code": "SOURCE_INCOMPLETE"},
                       headers=login("lead")).status_code == 409
    second_id = client.post(path, json={**body, "links": []},
                            headers=login("lead")).json()["alignment_id"]
    assert client.post(f"{path}/{second_id}/review", json={
        "outcome": "ACCEPTED", "reason_code": "MAPPING_INCORRECT",
    }, headers=login("lead")).status_code == 422
    returned = client.post(f"{path}/{second_id}/review", json={
        "outcome": "RETURNED", "reason_code": "MAPPING_INCORRECT",
    }, headers=login("lead"))
    assert returned.status_code == 200
    assert client.get(f"{path}/{second_id}").json()["status"] == "RETURNED"
    reopened = SQLiteStore(tmp_path / "course.sqlite3")
    assert reopened.get_teaching_alignment_review(alignment_id)["outcome"] == "ACCEPTED"
    reopened.close()
    assert cp.store.connection.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 0
    audits = cp.store.list_tenant_audits("tenant")
    assert {event["event_type"] for event in audits} >= {
        "TEACHING_ALIGNMENT_CREATED", "TEACHING_ALIGNMENT_REVIEWED",
    }
    assert all("理解递归" not in str(event) for event in audits)


def test_alignment_requires_all_source_access_and_current_revisions(tmp_path):
    cp, reader, client, contexts, login, body = teaching_alignment_fixture(tmp_path)
    path = "/api/teaching/courses/demo/alignments"
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json={**body, "links": [{"objective_item_id": "unknown",
                                                     "week_item_id": "week-1"}]},
                       headers=login("lead")).status_code == 422
    assert client.post(path, json={**body, "links": body["links"] * 2},
                       headers=login("lead")).status_code == 422
    assert client.post(path, json={**body, "syllabus": body["weekly_plan"],
                                   "weekly_plan": body["syllabus"]},
                       headers=login("lead")).status_code == 422
    assert create_course(client, login("admin"), "other").status_code == 200
    assert client.post("/api/teaching/courses/other/alignments", json=body,
                       headers=login("lead")).status_code == 404
    assert client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "负责人授权教师参加教研",
    }, headers=login("lead")).status_code == 200
    for ref in body.values():
        if not isinstance(ref, dict):
            continue
        assert client.post(f"/api/teaching/courses/demo/sources/{ref['source_id']}/readers", json={
            "actor_id": "teacher", "reason": "负责人共享教研资料",
        }, headers=login("lead")).status_code == 200
    created = client.post(path, json=body, headers=login("teacher"))
    assert created.status_code == 200
    alignment_id = created.json()["alignment_id"]
    assert [row["alignment_id"] for row in client.get(path).json()["alignments"]] == [alignment_id]
    assert client.post(f"{path}/{alignment_id}/review", json={
        "outcome": "ACCEPTED", "reason_code": "EVIDENCE_CONFIRMED",
    }, headers=login("teacher")).status_code == 403
    ref = body["weekly_plan"]
    assert client.delete(f"/api/teaching/courses/demo/sources/{ref['source_id']}/readers/teacher",
                         params={"reason": "负责人撤销该来源共享"}, headers=login("lead")).status_code == 200
    login("teacher")
    assert client.get(path).json()["alignments"] == []
    before = len(reader.calls)
    assert client.get(f"{path}/{alignment_id}").status_code == 403
    assert len(reader.calls) > before  # Earlier authorized sources may be rechecked first.
    reader.documents["course-weekly"]["revision_id"] = "2"
    login("lead")
    assert client.get(f"{path}/{alignment_id}").status_code == 409
    assert client.post(f"{path}/{alignment_id}/review", json={
        "outcome": "RETURNED", "reason_code": "SOURCE_INCOMPLETE",
    }, headers=login("lead")).status_code == 409
    assert cp.store.get_teaching_alignment_review(alignment_id) is None


def test_alignment_rechecks_earlier_source_after_last_document_read(tmp_path):
    cp, reader, client, contexts, login, body = teaching_alignment_fixture(tmp_path)
    path = "/api/teaching/courses/demo/alignments"
    assert client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "负责人授权教师参加教研",
    }, headers=login("lead")).status_code == 200
    for role in ("syllabus", "weekly_plan", "lesson_plan"):
        ref = body[role]
        assert client.post(f"/api/teaching/courses/demo/sources/{ref['source_id']}/readers", json={
            "actor_id": "teacher", "reason": "负责人逐项共享教研来源",
        }, headers=login("lead")).status_code == 200
    alignment_id = client.post(path, json=body, headers=login("teacher")).json()["alignment_id"]
    original_read = reader.read_document

    def revoke_during_last_read(identity, document_id):
        document = original_read(identity, document_id)
        if document_id == "course-lesson" and identity.actor_id == "teacher":
            cp.store.connection.execute(
                "UPDATE teaching_source_readers SET active = 0 WHERE source_id = ? AND actor_id = 'teacher'",
                (body["syllabus"]["source_id"],),
            )
            cp.store.connection.commit()
        return document

    reader.read_document = revoke_during_last_read
    assert client.get(f"{path}/{alignment_id}").status_code == 403
    assert client.get(path).json()["alignments"] == []


def test_readonly_teaching_handoff_is_durable_idempotent_and_has_no_tools(tmp_path):
    cp, reader, _, contexts, source_id, outline_id = teaching_handoff_fixture(tmp_path)
    service = TeachingService(cp, reader)
    args = (contexts["lead"], "demo", source_id, outline_id, "source-agent", 1)
    first = service.handoff_outline(*args)
    second = service.handoff_outline(*args)
    assert second == first
    assert cp._task(first["task_run_id"]).status == TaskRunStatus.SUCCEEDED
    assert cp.store.list_task_steps(first["task_run_id"])[0]["status"] == "SUCCEEDED"
    delegation = cp.store.list_delegations(first["task_run_id"])
    assert len(delegation) == 1 and delegation[0]["scope"] == ["doc.read"]
    assert cp.store.list_tool_calls(first["task_run_id"]) == []
    assert cp.store.list_approvals(first["task_run_id"]) == []
    assert cp.store.list_runtime_events(first["task_run_id"]) == []
    reopened = SQLiteStore(tmp_path / "course.sqlite3")
    saved = dict(reopened.get_teaching_handoff(first["task_run_id"]))
    assert saved["outline_hash"] == first["outline_hash"]
    assert reader.text not in str(saved)
    reopened.close()
    assert sum(e["event_type"] == "TEACHING_READONLY_HANDOFF"
               for e in cp.store.list_tenant_audits("tenant")) == 1


def test_teaching_source_runtime_persists_review_candidates_without_body(tmp_path):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)
    calls = []

    class FakeRuntime:
        def run_once(self, request):
            calls.append(request)
            source = request.input_ref
            quote = "递归"
            offset = source["blocks"][0]["text"].index(quote)
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "teaching-source-agent.v1",
                "source": {key: source[key] for key in
                           ("source_id", "revision_id", "content_hash", "kind")},
                "items": [{"item_id": "objective-1", "kind": "OBJECTIVE", "week_number": None,
                           "block_index": 1, "start_offset": offset,
                           "end_offset": offset + len(quote), "quote": quote}],
            }, None, events=(RunEvent(request.task_run_id, request.trace_id, 1,
                                      "completed", "2026-10-22T00:00:00Z", "candidate ready"),))

    service = TeachingSourceExecutionService(TeachingService(cp, reader), FakeRuntime())
    args = (contexts["lead"], "demo", source_id, "source-agent", 1)
    first = service.execute(*args)
    assert first["status"] == "WAITING_REVIEW" and first["item_count"] == 1
    assert service.execute(*args) == first | {"deduplicated": True}
    assert len(calls) == 1 and calls[0].tool_policy_snapshot == {"allowed_tools": []}
    assert cp.store.list_delegations(first["task_run_id"])[0]["scope"] == ["doc.read"]
    assert len(cp.store.list_runtime_events(first["task_run_id"])) == 1
    assert cp.store.list_tool_calls(first["task_run_id"]) == []
    assert cp.store.list_approvals(first["task_run_id"]) == []
    row = cp.store.connection.execute("SELECT data_json FROM teaching_extraction_results").fetchone()
    assert "递归" not in row["data_json"] and "quote_hash" in row["data_json"]
    assert service.read_candidates(contexts["lead"], "demo", first["task_run_id"])["items"][0]["quote"] == "递归"
    reopened = SQLiteStore(tmp_path / "course.sqlite3")
    assert reopened.connection.execute("SELECT count(*) FROM teaching_extraction_results").fetchone()[0] == 1
    reopened.close()


def test_teaching_source_runtime_rejects_bad_output_and_does_not_retry(tmp_path):
    cp, _, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)
    calls = []

    class BadRuntime:
        def run_once(self, request):
            calls.append(request)
            return RuntimeResult("SUCCEEDED", {"schema_version": "teaching-source-agent.v1",
                                               "source": dict(request.input_ref, blocks=[]),
                                               "items": []}, None)

    service = TeachingSourceExecutionService(TeachingService(cp, FakeUserReader()), BadRuntime())
    args = (contexts["lead"], "demo", source_id, "source-agent", 1)
    first = service.execute(*args)
    assert first["status"] == "FAILED"
    assert service.execute(*args)["deduplicated"] is True
    assert len(calls) == 1
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_extraction_results").fetchone()[0] == 0


def test_teaching_source_runtime_rechecks_revision_after_model(tmp_path):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)

    class ChangingRuntime:
        def run_once(self, request):
            reader.revision = "2"
            source = request.input_ref
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "teaching-source-agent.v1",
                "source": {key: source[key] for key in
                           ("source_id", "revision_id", "content_hash", "kind")},
                "items": [],
            }, None)

    service = TeachingSourceExecutionService(TeachingService(cp, reader), ChangingRuntime())
    result = service.execute(contexts["lead"], "demo", source_id, "source-agent", 1)
    assert result["status"] == "FAILED"
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_extraction_results").fetchone()[0] == 0


def test_teaching_source_runtime_rechecks_member_and_unknown_is_not_retried(tmp_path):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)
    calls = []

    class UnknownRuntime:
        def run_once(self, request):
            calls.append(request)
            return RuntimeResult("UNKNOWN", None, None, error_code="RUNTIME_TIMEOUT")

    service = TeachingSourceExecutionService(TeachingService(cp, reader), UnknownRuntime())
    args = (contexts["lead"], "demo", source_id, "source-agent", 1)
    result = service.execute(*args)
    assert result["status"] == "RECONCILING"
    assert service.execute(*args)["deduplicated"] is True
    assert len(calls) == 1
    cp.store.connection.execute(
        "UPDATE teaching_course_members SET active = 0 WHERE course_id = 'demo' AND actor_id = 'lead'")
    cp.store.connection.commit()
    with pytest.raises(PermissionError, match="course membership"):
        service.execute(*args)


def test_teaching_source_runtime_rechecks_authorization_in_result_transaction(tmp_path):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)

    class RevokingRuntime:
        def run_once(self, request):
            source = request.input_ref
            payload = {"schema_version": "teaching-source-agent.v1",
                       "source": {key: source[key] for key in
                                  ("source_id", "revision_id", "content_hash", "kind")},
                       "items": []}
            cp.store.connection.execute(
                "UPDATE teaching_course_members SET active = 0 "
                "WHERE course_id = 'demo' AND actor_id = 'lead'")
            cp.store.connection.commit()
            return RuntimeResult("SUCCEEDED", payload, None)

    service = TeachingSourceExecutionService(TeachingService(cp, reader), RevokingRuntime())
    result = service.execute(contexts["lead"], "demo", source_id, "source-agent", 1)
    assert result["status"] == "FAILED"
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_extraction_results").fetchone()[0] == 0


def test_teaching_source_concurrent_replay_calls_runtime_once(tmp_path):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class BlockingRuntime:
        def run_once(self, request):
            calls.append(request.task_run_id)
            entered.set()
            assert release.wait(5)
            source = request.input_ref
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "teaching-source-agent.v1",
                "source": {key: source[key] for key in
                           ("source_id", "revision_id", "content_hash", "kind")},
                "items": [],
            }, None)

    first = TeachingSourceExecutionService(TeachingService(cp, reader), BlockingRuntime())
    other_cp = ControlPlane(SQLiteStore(tmp_path / "course.sqlite3"))
    second = TeachingSourceExecutionService(TeachingService(other_cp, reader), BlockingRuntime())
    args = (contexts["lead"], "demo", source_id, "source-agent", 1)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first.execute, *args)
            assert entered.wait(5)
            replay = second.execute(*args)
            assert replay["deduplicated"] is True
            assert replay["status"] == "RUNNING"
            assert len(calls) == 1
            release.set()
            assert future.result(timeout=5)["status"] == "WAITING_REVIEW"
    finally:
        release.set()
        other_cp.close()


def test_teaching_source_interrupted_run_requires_reconciliation(tmp_path):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)
    calls = []

    class InterruptedRuntime:
        def run_once(self, request):
            calls.append(request.task_run_id)
            raise KeyboardInterrupt("simulated process interruption")

    service = TeachingSourceExecutionService(TeachingService(cp, reader), InterruptedRuntime())
    args = (contexts["lead"], "demo", source_id, "source-agent", 1)
    with pytest.raises(KeyboardInterrupt):
        service.execute(*args)
    replay = service.execute(*args)
    assert replay["status"] == "RUNNING" and replay["deduplicated"] is True
    assert len(calls) == 1
    reconciled = service.reconcile_stale(contexts["lead"], "demo", calls[0], grace_seconds=0)
    assert reconciled["status"] == "RECONCILING"
    assert service.execute(*args)["status"] == "RECONCILING"
    assert len(calls) == 1


def test_teaching_source_reconcile_persisted_result_after_interruption(tmp_path, monkeypatch):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)

    class EmptyRuntime:
        def run_once(self, request):
            source = request.input_ref
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "teaching-source-agent.v1",
                "source": {key: source[key] for key in
                           ("source_id", "revision_id", "content_hash", "kind")},
                "items": [],
            }, None)

    service = TeachingSourceExecutionService(TeachingService(cp, reader), EmptyRuntime())
    transition = cp.transition_task

    def interrupt_on_review(task_run_id, status, reason=None):
        if status == TaskRunStatus.WAITING_REVIEW:
            raise KeyboardInterrupt("simulated interruption after result commit")
        return transition(task_run_id, status, reason)

    monkeypatch.setattr(cp, "transition_task", interrupt_on_review)
    with pytest.raises(KeyboardInterrupt):
        service.execute(contexts["lead"], "demo", source_id, "source-agent", 1)
    monkeypatch.setattr(cp, "transition_task", transition)
    task_id = cp.store.connection.execute("SELECT task_run_id FROM teaching_extraction_results").fetchone()[0]
    assert cp._task(task_id).status == TaskRunStatus.RUNNING
    result = service.reconcile_stale(contexts["lead"], "demo", task_id, grace_seconds=0)
    assert result["status"] == "WAITING_REVIEW" and result["item_count"] == 0


def test_teaching_extraction_api_is_opt_in_and_rechecks_source_access(tmp_path):
    cp, reader, closed_client, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)
    path = f"/api/teaching/courses/demo/sources/{source_id}/extractions"
    assert closed_client.post(path, json={"agent_id": "source-agent", "agent_version": 1}).status_code == 404
    calls = []

    class FakeRuntime:
        def run_once(self, request):
            calls.append(request)
            source = request.input_ref
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "teaching-source-agent.v1",
                "source": {key: source[key] for key in
                           ("source_id", "revision_id", "content_hash", "kind")},
                "items": [],
            }, None)

    teaching = TeachingService(cp, reader)
    sessions = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant")
    app = FastAPI()
    app.include_router(build_teaching_router(
        teaching, sessions, public_origin="https://example.test",
        extraction=TeachingSourceExecutionService(
            teaching, FakeRuntime(), allowed_agent_id="source-agent"),
    ))
    client = TestClient(app)
    token = sessions(contexts["lead"])
    client.cookies.set("fde_auth_session", token)
    body = {"agent_id": "source-agent", "agent_version": 1}
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers={"Origin": "https://other.test",
                                                "X-CSRF-Token": hashlib.sha256(
                                                    ("meeting-review-csrf:" + token).encode()).hexdigest()}).status_code == 403
    assert calls == []
    headers = {"Origin": "https://example.test",
               "X-CSRF-Token": hashlib.sha256(("meeting-review-csrf:" + token).encode()).hexdigest()}
    assert client.post(path, json={"agent_id": "other-agent", "agent_version": 1},
                       headers=headers).status_code == 403
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 200 and first.json()["status"] == "WAITING_REVIEW"
    again = client.post(path, json=body, headers=headers)
    assert again.status_code == 200 and again.json()["deduplicated"] is True
    assert len(calls) == 1
    result_path = f"/api/teaching/courses/demo/extractions/{first.json()['task_run_id']}"
    assert client.get(result_path).json()["items"] == []
    review_path = result_path + "/review"
    review_body = {"outcome": "ACCEPTED", "reason_code": "EVIDENCE_CONFIRMED",
                   "coverage_confirmed": True, "decisions": []}
    assert client.post(review_path, json=review_body).status_code == 403
    assert client.post(review_path, json=review_body, headers=headers).status_code == 200
    assert client.get(result_path).json()["review"]["outcome"] == "ACCEPTED"
    assert client.post(review_path, json=review_body, headers=headers).status_code == 409
    cp.store.connection.execute(
        "UPDATE teaching_course_members SET active = 0 WHERE course_id = 'demo' AND actor_id = 'lead'")
    cp.store.connection.commit()
    assert client.get(result_path).status_code == 403
    assert client.post(path, json=body, headers=headers).status_code == 403
    assert len(calls) == 1


def test_teaching_extraction_detects_persisted_result_corruption(tmp_path):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)

    class EmptyRuntime:
        def run_once(self, request):
            source = request.input_ref
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "teaching-source-agent.v1",
                "source": {key: source[key] for key in
                           ("source_id", "revision_id", "content_hash", "kind")},
                "items": [],
            }, None)

    service = TeachingSourceExecutionService(TeachingService(cp, reader), EmptyRuntime())
    task = service.execute(contexts["lead"], "demo", source_id, "source-agent", 1)
    row = cp.store.connection.execute(
        "SELECT output_hash, data_json FROM teaching_extraction_results WHERE task_run_id = ?",
        (task["task_run_id"],),
    ).fetchone()
    assert row["output_hash"] == hashlib.sha256(row["data_json"].encode()).hexdigest()
    cp.store.connection.execute("DROP TRIGGER teaching_extraction_results_no_update")
    cp.store.connection.execute(
        "UPDATE teaching_extraction_results SET data_json = ? WHERE task_run_id = ?",
        ('{"items":[],"source":{}}', task["task_run_id"]),
    )
    cp.store.connection.commit()
    with pytest.raises(Exception, match="integrity check failed"):
        service.read_candidates(contexts["lead"], "demo", task["task_run_id"])


def test_teaching_extraction_review_requires_every_item_and_is_immutable(tmp_path):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)

    class OneItemRuntime:
        def run_once(self, request):
            source = request.input_ref
            quote = "递归"
            start = source["blocks"][0]["text"].index(quote)
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "teaching-source-agent.v1",
                "source": {key: source[key] for key in
                           ("source_id", "revision_id", "content_hash", "kind")},
                "items": [{"item_id": "objective-1", "kind": "OBJECTIVE", "week_number": None,
                           "block_index": 1, "start_offset": start,
                           "end_offset": start + len(quote), "quote": quote}],
            }, None)

    service = TeachingSourceExecutionService(TeachingService(cp, reader), OneItemRuntime())
    task_id = service.execute(contexts["lead"], "demo", source_id, "source-agent", 1)["task_run_id"]
    args = (contexts["lead"], "demo", task_id)
    with pytest.raises(ValueError, match="every teaching candidate"):
        service.review_candidates(*args, "ACCEPTED", "EVIDENCE_CONFIRMED", True, [])
    with pytest.raises(ValueError, match="outcome and reason"):
        service.review_candidates(*args, "ACCEPTED", "EVIDENCE_CONFIRMED", False,
                                  [{"item_id": "objective-1", "verdict": "CONFIRMED"}])
    with pytest.raises(ValueError, match="every teaching candidate"):
        service.review_candidates(*args, "ACCEPTED", "EVIDENCE_CONFIRMED", True,
                                  [{"item_id": "objective-1", "verdict": "REJECTED"}])
    with pytest.raises(ValueError, match="every teaching candidate"):
        service.review_candidates(*args, "RETURNED", "WRONG_WEEK", False,
                                  [{"item_id": "objective-1", "verdict": "CONFIRMED"}])
    service.teaching.set_teacher(contexts["lead"], "demo", "teacher",
                                 active=True, reason="课程负责人邀请普通教师")
    with pytest.raises(PermissionError, match="course lead authorization"):
        service.review_candidates(contexts["teacher"], "demo", task_id, "ACCEPTED",
                                  "EVIDENCE_CONFIRMED", True,
                                  [{"item_id": "objective-1", "verdict": "CONFIRMED"}])
    decision = [{"item_id": "objective-1", "verdict": "CONFIRMED"}]
    reviewed = service.review_candidates(*args, "ACCEPTED", "EVIDENCE_CONFIRMED", True, decision)
    assert reviewed["outcome"] == "ACCEPTED"
    assert service.read_candidates(*args)["review"]["decisions"] == decision
    with pytest.raises(ValueError, match="already reviewed"):
        service.review_candidates(*args, "ACCEPTED", "EVIDENCE_CONFIRMED", True, decision)
    row = cp.store.connection.execute("SELECT * FROM teaching_extraction_reviews").fetchone()
    assert row["output_hash"] and "递归" not in row["decisions_json"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable teaching extraction review"):
        cp.store.connection.execute(
            "UPDATE teaching_extraction_reviews SET outcome = 'RETURNED' WHERE task_run_id = ?",
            (task_id,),
        )
    reopened = SQLiteStore(tmp_path / "course.sqlite3")
    assert reopened.connection.execute(
        "SELECT outcome FROM teaching_extraction_reviews WHERE task_run_id = ?", (task_id,),
    ).fetchone()["outcome"] == "ACCEPTED"
    reopened.close()
    assert sum(event["event_type"] == "TEACHING_EXTRACTION_REVIEWED"
               for event in cp.store.list_tenant_audits("tenant")) == 1


def test_reviewed_three_sources_create_recheckable_preparation_draft(tmp_path):
    cp, reader, client, contexts, login, body = teaching_alignment_fixture(tmp_path)
    caps = frozenset({"doc.read"})
    cp.register_actor(Actor("assistant", "tenant", ActorType.PERSONAL_ASSISTANT, caps))
    cp.register_actor(Actor("source-agent", "tenant", ActorType.BUSINESS_AGENT, caps))
    cp.register_agent_version(AgentVersion("source-agent", 1, caps))
    cp.bind_assistant(AssistantBinding("lead-binding", "tenant", "lead", "assistant"))
    cp.register_actor(Actor("lead", "tenant", ActorType.USER, caps,
                            external_ref_hash=contexts["lead"].subject_ref_hash))

    class ThreeSourceRuntime:
        def run_once(self, request):
            source = request.input_ref
            kind = {"SYLLABUS": "OBJECTIVE", "WEEKLY_PLAN": "WEEK_PLAN",
                    "LESSON_PLAN": "LESSON_FRAGMENT"}[source["kind"]]
            items = []
            for index, block in enumerate(source["blocks"], 1):
                week = (index if source["kind"] == "WEEKLY_PLAN" else
                        (1 if index == 1 else 4) if source["kind"] == "LESSON_PLAN" else None)
                items.append({"item_id": f"item-{index}", "kind": kind,
                              "week_number": week, "block_index": index,
                              "start_offset": 0, "end_offset": len(block["text"]),
                              "quote": block["text"]})
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "teaching-source-agent.v1",
                "source": {key: source[key] for key in
                           ("source_id", "revision_id", "content_hash", "kind")},
                "items": items,
            }, None)

    extraction = TeachingSourceExecutionService(TeachingService(cp, reader), ThreeSourceRuntime())
    preparation = TeachingPreparationService(extraction)
    task_ids = {}
    for role, source_ref in body.items():
        if role == "links":
            continue
        task_id = extraction.execute(contexts["lead"], "demo", source_ref["source_id"],
                                     "source-agent", 1)["task_run_id"]
        task_ids[role] = task_id
    with pytest.raises(ValueError, match="accepted lead reviews"):
        preparation.create(contexts["lead"], "demo", task_ids)
    for task_id in task_ids.values():
        items = extraction.read_candidates(contexts["lead"], "demo", task_id)["items"]
        extraction.review_candidates(contexts["lead"], "demo", task_id, "ACCEPTED",
                                     "EVIDENCE_CONFIRMED", True,
                                     [{"item_id": item["item_id"], "verdict": "CONFIRMED"}
                                      for item in items])

    draft = preparation.create(contexts["lead"], "demo", task_ids)
    assert draft["status"] == "NEEDS_TEACHER_REVIEW"
    assert [item["code"] for item in draft["suggestions"]] == [
        "OBJECTIVE_MAPPING_TO_CONFIRM", "OBJECTIVE_MAPPING_TO_CONFIRM",
        "WEEK_PREPARATION", "LESSON_NOT_ANNOTATED", "PLAN_NOT_ANNOTATED",
    ]
    assert draft["suggestions"][2]["evidence"][0]["item"]["quote"] == "第 1 周：递归定义"
    assert preparation.create(contexts["lead"], "demo", task_ids) == draft
    assert preparation.read(contexts["lead"], "demo", draft["draft_id"]) == draft
    route = f"/api/teaching/courses/demo/preparations/{draft['draft_id']}"
    assert client.get(route).status_code == 404
    sessions = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant")
    app = FastAPI()
    app.include_router(build_teaching_router(
        TeachingService(cp, reader), sessions, public_origin="https://example.test",
        extraction=extraction,
    ))
    api = TestClient(app)
    token = sessions(contexts["lead"])
    api.cookies.set("fde_auth_session", token)
    headers = {"Origin": "https://example.test",
               "X-CSRF-Token": hashlib.sha256(("meeting-review-csrf:" + token).encode()).hexdigest()}
    assert api.get(route).json() == draft
    assert api.post("/api/teaching/courses/demo/preparations", json=task_ids).status_code == 403
    assert api.post("/api/teaching/courses/demo/preparations", json=task_ids,
                    headers=headers).json() == draft

    extraction.teaching.set_teacher(contexts["lead"], "demo", "teacher",
                                    active=True, reason="负责人邀请教师核对备课清单")
    with pytest.raises(PermissionError, match="source access"):
        preparation.read(contexts["teacher"], "demo", draft["draft_id"])
    row = cp.store.connection.execute("SELECT data_json FROM teaching_preparation_drafts").fetchone()
    assert "递归定义" not in row["data_json"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable teaching preparation draft"):
        cp.store.connection.execute(
            "UPDATE teaching_preparation_drafts SET input_hash = 'changed' WHERE draft_id = ?",
            (draft["draft_id"],),
        )
    assert sum(event["event_type"] == "TEACHING_PREPARATION_CREATED"
               for event in cp.store.list_tenant_audits("tenant")) == 1
    assert cp.store.list_tool_calls(task_ids["syllabus"]) == []

    reader.documents["course-weekly"]["revision_id"] = "2"
    with pytest.raises(Exception, match="source revision or content changed"):
        preparation.read(contexts["lead"], "demo", draft["draft_id"])


def test_teaching_extraction_review_rechecks_lead_during_commit(tmp_path):
    cp, reader, _, contexts, source_id, _ = teaching_handoff_fixture(tmp_path)

    class EmptyRuntime:
        def run_once(self, request):
            source = request.input_ref
            return RuntimeResult("SUCCEEDED", {
                "schema_version": "teaching-source-agent.v1",
                "source": {key: source[key] for key in
                           ("source_id", "revision_id", "content_hash", "kind")},
                "items": [],
            }, None)

    service = TeachingSourceExecutionService(TeachingService(cp, reader), EmptyRuntime())
    task_id = service.execute(contexts["lead"], "demo", source_id, "source-agent", 1)["task_run_id"]
    original_read = service.read_candidates

    def revoke_after_read(*args):
        result = original_read(*args)
        cp.store.connection.execute(
            "UPDATE teaching_course_members SET active = 0 WHERE course_id = 'demo' AND actor_id = 'lead'")
        cp.store.connection.commit()
        return result

    service.read_candidates = revoke_after_read
    with pytest.raises(PermissionError, match="revoked during review"):
        service.review_candidates(contexts["lead"], "demo", task_id,
                                  "RETURNED", "MISSED_CONTENT", False, [])
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_extraction_reviews").fetchone()[0] == 0
    cp.store.connection.execute(
        "UPDATE teaching_course_members SET active = 1 WHERE course_id = 'demo' AND actor_id = 'lead'")
    cp.store.connection.commit()
    service.read_candidates = original_read
    reader.revision = "2"
    with pytest.raises(Exception, match="source revision or content changed"):
        service.review_candidates(contexts["lead"], "demo", task_id,
                                  "RETURNED", "MISSED_CONTENT", False, [])
    reader.revision = "1"
    returned = service.review_candidates(contexts["lead"], "demo", task_id,
                                         "RETURNED", "MISSED_CONTENT", False, [])
    assert returned["outcome"] == "RETURNED"
    assert service.read_candidates(contexts["lead"], "demo", task_id)["review"]["outcome"] == "RETURNED"
    assert cp.store.list_tool_calls(task_id) == []
    assert cp.store.list_approvals(task_id) == []


def test_readonly_handoff_rechecks_course_source_version_and_agent_scope(tmp_path):
    cp, reader, _, contexts, source_id, outline_id = teaching_handoff_fixture(tmp_path)
    service = TeachingService(cp, reader)
    args = (contexts["lead"], "demo", source_id, outline_id, "source-agent", 1)
    with pytest.raises(PermissionError, match="course membership"):
        service.handoff_outline(contexts["lead"], "other", source_id, outline_id, "source-agent", 1)
    reader.revision = "2"
    with pytest.raises(Exception, match="source revision or content changed"):
        service.handoff_outline(*args)
    reader.revision = "1"
    cp.register_agent_version(AgentVersion("source-agent", 2, frozenset()))
    with pytest.raises(PermissionError, match="read-only teaching delegation"):
        service.handoff_outline(*args[:-1], 2)
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_handoffs").fetchone()[0] == 0
    assert cp.store.connection.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0


def test_readonly_handoff_denies_revoked_member_on_replay_and_during_handoff(tmp_path):
    cp, reader, _, contexts, source_id, outline_id = teaching_handoff_fixture(tmp_path)
    service = TeachingService(cp, reader)
    args = (contexts["lead"], "demo", source_id, outline_id, "source-agent", 1)
    first = service.handoff_outline(*args)
    cp.store.connection.execute(
        "UPDATE teaching_course_members SET active = 0 WHERE course_id = 'demo' AND actor_id = 'lead'"
    )
    cp.store.connection.commit()
    with pytest.raises(PermissionError, match="course membership"):
        service.handoff_outline(*args)
    assert len(cp.store.list_delegations(first["task_run_id"])) == 1

    cp.store.connection.execute(
        "UPDATE teaching_course_members SET active = 1 WHERE course_id = 'demo' AND actor_id = 'lead'"
    )
    cp.store.connection.commit()
    original_read = service.read_outline

    def revoke_after_read(*params):
        result = original_read(*params)
        cp.store.connection.execute(
            "UPDATE teaching_course_members SET active = 0 WHERE course_id = 'demo' AND actor_id = 'lead'"
        )
        cp.store.connection.commit()
        return result

    service.read_outline = revoke_after_read
    cp.register_agent_version(AgentVersion("source-agent", 2, frozenset({"doc.read"})))
    with pytest.raises(PermissionError, match="revoked during handoff"):
        service.handoff_outline(*args[:-1], 2)
    failed = cp.store.connection.execute(
        "SELECT data_json FROM task_runs WHERE task_run_id != ?", (first["task_run_id"],)
    ).fetchone()
    assert failed is not None and json.loads(failed["data_json"])["status"] == "FAILED"
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_handoffs").fetchone()[0] == 1


def test_readonly_handoff_requires_binding_and_current_source_share(tmp_path):
    cp, reader, _, contexts, source_id, outline_id = teaching_handoff_fixture(tmp_path)
    service = TeachingService(cp, reader)
    cp.store.connection.execute("DELETE FROM assistant_bindings WHERE member_actor_id = 'lead'")
    cp.store.connection.commit()
    with pytest.raises(PermissionError, match="personal assistant binding"):
        service.handoff_outline(contexts["lead"], "demo", source_id, outline_id, "source-agent", 1)
    cp.bind_assistant(AssistantBinding("lead-binding-2", "tenant", "lead", "assistant"))
    cp.register_actor(Actor("teacher", "tenant", ActorType.USER, frozenset({"doc.read"}),
                            external_ref_hash=contexts["teacher"].subject_ref_hash))
    cp.bind_assistant(AssistantBinding("teacher-binding", "tenant", "teacher", "assistant"))
    service.set_teacher(contexts["lead"], "demo", "teacher", active=True, reason="课程负责人授予资料访问")
    service.set_source_reader(contexts["lead"], "demo", source_id, "teacher",
                              active=True, reason="课程负责人逐项共享大纲")
    args = (contexts["teacher"], "demo", source_id, outline_id, "source-agent", 1)
    first = service.handoff_outline(*args)
    service.set_source_reader(contexts["lead"], "demo", source_id, "teacher",
                              active=False, reason="课程负责人撤销大纲共享")
    with pytest.raises(PermissionError, match="source access"):
        service.handoff_outline(*args)
    assert len(cp.store.list_delegations(first["task_run_id"])) == 1


def create_course(client, headers, course_id="demo"):
    return client.post("/api/teaching/courses", json={
        "course_id": course_id, "title": "虚构算法课程", "term": "2026 秋", "lead_actor_id": "lead",
    }, headers=headers)


def test_course_roles_revocation_and_tenant_isolation(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    assert client.get("/api/teaching/courses").status_code == 401
    outsider_headers = login("outsider")
    assert create_course(client, outsider_headers).status_code == 403
    admin_headers = login("admin")
    assert create_course(client, admin_headers).status_code == 200
    assert create_course(client, admin_headers).status_code == 409
    assert client.get("/api/teaching/courses/demo").status_code == 403  # admin is not a course member
    assert client.get("/api/teaching/courses").json()["courses"] == []
    assert create_course(client, admin_headers, "another").status_code == 200
    lead_headers = login("lead")
    assert len(client.get("/api/teaching/courses").json()["courses"]) == 2
    assert client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "课程组负责人确认教师分工",
    }, headers=lead_headers).status_code == 200
    teacher_headers = login("teacher")
    assert client.get("/api/teaching/courses/demo").status_code == 200
    assert client.get("/api/teaching/courses/another").status_code == 403
    assert client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "outsider", "reason": "教师请求自行授予权限",
    }, headers=teacher_headers).status_code == 403
    lead_headers = login("lead")
    assert client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "foreign", "reason": "尝试跨租户授权教师",
    }, headers=lead_headers).status_code == 403
    assert client.delete("/api/teaching/courses/demo/teachers/teacher",
                         params={"reason": "课程组负责人撤销教师访问"}, headers=lead_headers).status_code == 200
    login("teacher")
    assert client.get("/api/teaching/courses/demo").status_code == 403
    assert client.get("/api/teaching/courses").json()["courses"] == []
    audits = cp.store.list_tenant_audits("tenant")
    assert {e["event_type"] for e in audits} >= {
        "TEACHING_COURSE_CREATED", "TEACHING_TEACHER_GRANTED",
        "TEACHING_TEACHER_REVOKED", "TEACHING_ACCESS_DENIED",
    }
    assert all("虚构算法课程" not in str(e) for e in audits)
    assert reader.calls == []


def test_feishu_source_is_fresh_member_oauth_only_and_never_persisted_as_text(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    admin_headers = login("admin")
    assert create_course(client, admin_headers).status_code == 200
    lead_headers = login("lead")
    source_request = {"kind": "WEEKLY_PLAN", "document_url": DOC_URL,
                      "attested_no_student_data": True}
    assert client.post("/api/teaching/courses/demo/sources", json=source_request).status_code == 403
    assert client.post("/api/teaching/courses/demo/sources", json=source_request,
                       headers={**lead_headers, "Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/teaching/courses/demo/sources", json={**source_request, "extra": "x"},
                       headers=lead_headers).status_code == 422
    created = client.post("/api/teaching/courses/demo/sources", json=source_request, headers=lead_headers)
    assert created.status_code == 200
    source_id = created.json()["source_id"]
    assert reader.calls == [("lead", DOC_ID), ("lead", DOC_ID)]
    raw = cp.store.connection.execute("SELECT * FROM teaching_sources WHERE source_id = ?", (source_id,)).fetchone()
    assert reader.text not in repr(dict(raw))
    assert "document_id" not in created.json()
    assert "text" not in str(client.get("/api/teaching/courses/demo/sources").json())
    fresh = client.get(f"/api/teaching/courses/demo/sources/{source_id}")
    assert fresh.status_code == 200
    assert fresh.json()["blocks"][0]["text"] == reader.text
    assert fresh.json()["revision_id"] == "1"
    assert any(event["event_type"] == "TEACHING_SOURCE_READ" for event in cp.store.list_tenant_audits("tenant"))
    assert reader.calls[-2:] == [("lead", DOC_ID), ("lead", DOC_ID)]
    assert create_course(client, login("admin"), "another").status_code == 200
    lead_headers = login("lead")
    before = len(reader.calls)
    assert client.get(f"/api/teaching/courses/another/sources/{source_id}").status_code == 404
    assert len(reader.calls) == before

    granted = client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "课程组负责人批准资料阅读",
    }, headers=lead_headers)
    assert granted.status_code == 200
    assert client.post(f"/api/teaching/courses/demo/sources/{source_id}/readers", json={
        "actor_id": "teacher", "reason": "课程负责人共享该来源",
    }, headers=lead_headers).status_code == 200
    login("teacher")
    assert client.get(f"/api/teaching/courses/demo/sources/{source_id}").status_code == 200
    assert reader.calls[-2:] == [("teacher", DOC_ID), ("teacher", DOC_ID)]
    reader.allowed.remove("teacher")
    assert client.get(f"/api/teaching/courses/demo/sources/{source_id}").status_code == 403
    reader.allowed.add("teacher")
    reader.revision = "2"
    assert client.get(f"/api/teaching/courses/demo/sources/{source_id}").status_code == 409
    reader.revision = "1"
    reader.text = "第 2 周：调整为迭代。"
    assert client.get(f"/api/teaching/courses/demo/sources/{source_id}").status_code == 409
    reader.change_during_read = True
    assert client.get(f"/api/teaching/courses/demo/sources/{source_id}").status_code == 409
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_sources").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable teaching source"):
        cp.store.connection.execute("UPDATE teaching_sources SET revision_id = '9' WHERE source_id = ?", (source_id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable teaching source"):
        cp.store.connection.execute("DELETE FROM teaching_sources WHERE source_id = ?", (source_id,))


def test_source_requires_course_access_and_document_scope(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    create_course(client, login("admin"))
    lead_headers = login("lead")
    source = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "SYLLABUS", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=lead_headers).json()
    login("outsider")
    before = len(reader.calls)
    assert client.get(f"/api/teaching/courses/demo/sources/{source['source_id']}").status_code == 403
    assert len(reader.calls) == before
    granted = client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "批准成员查看课程资料",
    }, headers=login("lead"))
    assert granted.status_code == 200
    assert client.post(f"/api/teaching/courses/demo/sources/{source['source_id']}/readers", json={
        "actor_id": "teacher", "reason": "负责人授权阅读该资料",
    }, headers=login("lead")).status_code == 200
    login("teacher")
    assert client.get(f"/api/teaching/courses/demo/sources/{source['source_id']}").status_code == 200
    lead_headers = login("lead")
    assert client.delete("/api/teaching/courses/demo/teachers/teacher",
                         params={"reason": "结束本学期课程成员授权"}, headers=lead_headers).status_code == 200
    login("teacher")
    before = len(reader.calls)
    assert client.get(f"/api/teaching/courses/demo/sources/{source['source_id']}").status_code == 403
    assert len(reader.calls) == before
    no_scope = IdentityContext("tenant", "lead", "user_oauth", contexts["lead"].subject_ref_hash)
    client.cookies.set("fde_auth_session", SQLiteOAuthSessionIssuer(cp, tenant_id="tenant")(no_scope))
    assert client.get(f"/api/teaching/courses/demo/sources/{source['source_id']}").status_code == 403
    assert len(reader.calls) == before


def test_source_diagnostics_separate_removed_blocks_edits_and_revision_only(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    create_course(client, login("admin"))
    original = [
        {"index": 1, "block_type": 2, "text": "课程目标：理解递归。"},
        {"index": 2, "block_type": 2, "text": "第 1 周：递归定义。"},
        {"index": 3, "block_type": 2, "text": "第 2 周：递归练习。"},
    ]
    reader.blocks = original
    source = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "WEEKLY_PLAN", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=login("lead")).json()
    source_id = source["source_id"]
    path = f"/api/teaching/courses/demo/sources/{source_id}/diagnostics"
    assert client.get(path).json()["status"] == "CURRENT"
    assert len(cp.store.list_teaching_source_blocks(source_id)) == 3
    assert all("递归" not in str(dict(row)) for row in cp.store.list_teaching_source_blocks(source_id))
    with pytest.raises(sqlite3.IntegrityError, match="immutable teaching source block"):
        cp.store.connection.execute(
            "UPDATE teaching_source_blocks SET block_hash = 'x' WHERE source_id = ?", (source_id,)
        )
    reader.revision = "2"
    reader.blocks = [original[0], {**original[2], "index": 2}]
    result = client.get(path).json()
    assert result["status"] == "CONTENT_CHANGED"
    assert result["missing_block_candidates"] == [2]
    assert result["new_block_candidates"] == []
    assert result["baseline_block_count"] == 3 and result["current_block_count"] == 2
    assert "递归" not in str(result)
    assert client.get(f"/api/teaching/courses/demo/sources/{source_id}").status_code == 409
    reader.blocks = [original[0], {**original[1], "text": "第 1 周：递归案例。"}, original[2]]
    edited = client.get(path).json()
    assert edited["missing_block_candidates"] == [2]
    assert edited["new_block_candidates"] == [2]
    reader.blocks = original
    assert client.get(path).json()["status"] == "VERSION_CHANGED"
    assert all("递归" not in str(event) for event in cp.store.list_tenant_audits("tenant"))


def test_source_diagnostics_fail_closed_on_revocation_and_old_baseline(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    create_course(client, login("admin"))
    source_id = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "SYLLABUS", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=login("lead")).json()["source_id"]
    path = f"/api/teaching/courses/demo/sources/{source_id}/diagnostics"
    login("outsider")
    before = len(reader.calls)
    assert client.get(path).status_code == 403
    assert len(reader.calls) == before
    login("lead")
    reader.allowed.remove("lead")
    assert client.get(path).status_code == 403
    reader.allowed.add("lead")
    original_read = reader.read_document

    def revoke_during_read(identity, document_id):
        result = original_read(identity, document_id)
        cp.store.connection.execute(
            "UPDATE teaching_course_members SET active = 0 WHERE course_id = 'demo' AND actor_id = 'lead'"
        )
        cp.store.connection.commit()
        return result

    reader.read_document = revoke_during_read
    assert client.get(path).status_code == 403
    assert not any(event["event_type"] == "TEACHING_SOURCE_DIAGNOSED"
                   for event in cp.store.list_tenant_audits("tenant"))
    reader.read_document = original_read
    cp.store.connection.execute(
        "UPDATE teaching_course_members SET active = 1 WHERE course_id = 'demo' AND actor_id = 'lead'"
    )
    cp.store.connection.commit()
    revision, digest, _ = TeachingService._snapshot(original_read(contexts["lead"], DOC_ID), DOC_ID)
    cp.store.connection.execute(
        "INSERT INTO teaching_sources(source_id, tenant_id, course_id, kind, document_id, revision_id, "
        "content_hash, block_count, data_reviewed, submitted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        ("old-source", "tenant", "demo", "SYLLABUS", DOC_ID, revision, digest, 1, "lead"),
    )
    cp.store.connection.commit()
    reader.text = "虚构课程目标改为迭代。"
    legacy = client.get("/api/teaching/courses/demo/sources/old-source/diagnostics").json()
    assert legacy["status"] == "BASELINE_UNAVAILABLE"
    assert legacy["baseline_available"] is False
    assert legacy["missing_block_candidates"] == []


def test_reopening_database_preserves_course_and_source_contract(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    create_course(client, login("admin"))
    lead_headers = login("lead")
    source = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "LESSON_PLAN", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=lead_headers).json()
    assert client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "课程负责人批准任课教师",
    }, headers=lead_headers).status_code == 200
    assert client.post(f"/api/teaching/courses/demo/sources/{source['source_id']}/readers", json={
        "actor_id": "teacher", "reason": "负责人共享教案来源",
    }, headers=lead_headers).status_code == 200
    reopened = SQLiteStore(tmp_path / "course.sqlite3")
    assert reopened.get_teaching_course("tenant", "demo")["term"] == "2026 秋"
    assert reopened.get_teaching_source("tenant", "demo", source["source_id"])["revision_id"] == "1"
    assert reopened.get_teaching_source_reader("tenant", "demo", source["source_id"], "teacher")["active"] == 1
    reopened.close()


def test_missing_member_oauth_reader_fails_closed(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    create_course(client, login("admin"))
    sessions = SQLiteOAuthSessionIssuer(cp, tenant_id="tenant")
    app = FastAPI()
    app.include_router(build_teaching_router(TeachingService(cp, None), sessions,
                                              public_origin="https://example.test"))
    closed_client = TestClient(app)
    token = sessions(contexts["lead"])
    closed_client.cookies.set("fde_auth_session", token)
    assert closed_client.post("/api/teaching/courses/demo/sources", json={
        "kind": "MATERIAL", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers={"X-CSRF-Token": hashlib.sha256(("meeting-review-csrf:" + token).encode()).hexdigest(),
                "Origin": "https://example.test"}).status_code == 503
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_sources").fetchone()[0] == 0


def test_source_attestation_and_student_data_screening_fail_closed(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    assert create_course(client, login("admin")).status_code == 200
    headers = login("lead")
    path = "/api/teaching/courses/demo/sources"
    request = {"kind": "SYLLABUS", "document_url": DOC_URL}
    before = len(reader.calls)
    assert client.post(path, json=request, headers=headers).status_code == 422
    assert client.post(path, json={**request, "attested_no_student_data": False},
                       headers=headers).status_code == 422
    assert client.post(path, json={**request, "attested_no_student_data": "true"},
                       headers=headers).status_code == 422
    assert len(reader.calls) == before

    reviewed_request = {**request, "attested_no_student_data": True}
    for sensitive in ("学生姓名：张三", "学号：2026123456", "个人成绩：95",
                      "联系电话 13800138000", "11010519491231002X"):
        reader.text = sensitive
        response = client.post(path, json=reviewed_request, headers=headers)
        assert response.status_code == 422
        assert sensitive not in response.text
    reader.text = "教学目标：学生能够分析算法复杂度。"
    reader.title = "学生名单"
    assert client.post(path, json=reviewed_request, headers=headers).status_code == 422
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_sources").fetchone()[0] == 0

    reader.title = "虚构课程大纲"
    created = client.post(path, json=reviewed_request, headers=headers)
    assert created.status_code == 200
    source_id = created.json()["source_id"]
    assert created.json()["data_reviewed"] is True
    assert cp.store.get_teaching_source("tenant", "demo", source_id)["data_reviewed"] == 1
    assert client.get(f"{path}/{source_id}").status_code == 200
    reader.text = "学生姓名：张三"
    response = client.get(f"{path}/{source_id}")
    assert response.status_code == 422
    assert "张三" not in response.text
    assert all("张三" not in str(event) for event in cp.store.list_tenant_audits("tenant"))


def test_legacy_source_migration_requires_new_review_before_read(tmp_path):
    path = tmp_path / "course.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.execute(
            "CREATE TABLE teaching_sources ("
            "source_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, course_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL, "
            "content_hash TEXT NOT NULL, block_count INTEGER NOT NULL, submitted_by TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        legacy.execute(
            "INSERT INTO teaching_sources(source_id, tenant_id, course_id, kind, document_id, "
            "revision_id, content_hash, block_count, submitted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-source", "tenant", "demo", "SYLLABUS", DOC_ID, "1", "old-hash", 1, "lead"),
        )
    cp, reader, client, contexts, login = setup(tmp_path)
    assert create_course(client, login("admin")).status_code == 200
    login("lead")
    assert cp.store.get_teaching_source("tenant", "demo", "legacy-source")["data_reviewed"] == 0
    before = len(reader.calls)
    legacy_path = "/api/teaching/courses/demo/sources/legacy-source"
    assert client.get(legacy_path).status_code == 409
    assert len(reader.calls) == before
    new_source = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "SYLLABUS", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=login("lead"))
    assert new_source.status_code == 200
    assert client.get(f"/api/teaching/courses/demo/sources/{new_source.json()['source_id']}").status_code == 200


def test_weekly_outline_has_pinned_citations_and_human_coverage(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    create_course(client, login("admin"))
    reader.blocks = [
        {"index": 1, "block_type": 3, "text": "虚构周计划"},
        {"index": 2, "block_type": 2, "text": "第 1 周：递归基础与练习。"},
        {"index": 3, "block_type": 2, "text": "第 2 周：树的遍历与讨论。"},
    ]
    lead_headers = login("lead")
    source = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "WEEKLY_PLAN", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=lead_headers).json()
    source_id = source["source_id"]
    outline_request = {"expected_weeks": [1, 2, 3], "items": [
        {"item_id": "week-1", "kind": "WEEK_PLAN", "block_index": 2,
         "quote": "递归基础与练习", "week_number": 1},
        {"item_id": "week-2", "kind": "WEEK_PLAN", "block_index": 3,
         "quote": "树的遍历与讨论", "week_number": 2},
    ]}
    path = f"/api/teaching/courses/demo/sources/{source_id}/outlines"
    assert client.post(path, json=outline_request).status_code == 403
    assert client.post(path, json={**outline_request, "unexpected": True}, headers=lead_headers).status_code == 422
    created = client.post(path, json=outline_request, headers=lead_headers)
    assert created.status_code == 200
    outline_id = created.json()["outline_id"]
    assert created.json()["coverage"]["unannotated_expected_weeks"] == [3]
    assert created.json()["coverage"]["unexpected_weeks"] == []
    stored = dict(cp.store.get_teaching_outline("tenant", "demo", source_id, outline_id))
    assert "递归基础与练习" not in str(stored)
    assert "树的遍历与讨论" not in str(stored)
    assert all("quote" not in item for item in json.loads(stored["data_json"])["items"])
    with pytest.raises(sqlite3.IntegrityError, match="immutable teaching outline"):
        cp.store.connection.execute("UPDATE teaching_outlines SET source_revision = '9' WHERE outline_id = ?",
                                    (outline_id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable teaching outline"):
        cp.store.connection.execute("DELETE FROM teaching_outlines WHERE outline_id = ?", (outline_id,))

    granted = client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "课程负责人允许查看周计划",
    }, headers=lead_headers)
    assert granted.status_code == 200
    assert client.post(f"/api/teaching/courses/demo/sources/{source_id}/readers", json={
        "actor_id": "teacher", "reason": "课程负责人共享周计划",
    }, headers=lead_headers).status_code == 200
    login("teacher")
    assert client.get(path).json()["outlines"][0]["outline_id"] == outline_id
    detail = client.get(f"{path}/{outline_id}")
    assert detail.status_code == 200
    assert [item["quote"] for item in detail.json()["items"]] == ["递归基础与练习", "树的遍历与讨论"]
    assert detail.json()["items"][0]["citation"]["block_index"] == 2
    assert detail.json()["items"][0]["citation"]["revision_id"] == "1"
    assert detail.json()["coverage"]["unannotated_expected_weeks"] == [3]
    reader.allowed.remove("teacher")
    assert client.get(f"{path}/{outline_id}").status_code == 403
    reader.allowed.add("teacher")
    reader.revision = "2"
    assert client.get(f"{path}/{outline_id}").status_code == 409
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_outlines").fetchone()[0] == 1
    reader.revision = "1"
    assert client.delete("/api/teaching/courses/demo/teachers/teacher",
                         params={"reason": "结束教师的课程资料访问"}, headers=login("lead")).status_code == 200
    login("teacher")
    before = len(reader.calls)
    assert client.get(path).status_code == 403
    assert client.get(f"{path}/{outline_id}").status_code == 403
    assert len(reader.calls) == before
    audits = cp.store.list_tenant_audits("tenant")
    assert {event["event_type"] for event in audits} >= {
        "TEACHING_OUTLINE_CREATED", "TEACHING_OUTLINE_READ",
    }
    assert all("递归基础与练习" not in str(event) for event in audits)


@pytest.mark.parametrize("source_kind,item_kind,quote,week", [
    ("SYLLABUS", "OBJECTIVE", "理解递归的基本思想", None),
    ("LESSON_PLAN", "LESSON_FRAGMENT", "递归课堂练习", 2),
])
def test_syllabus_and_lesson_fragments_require_exact_source_quote(
    tmp_path, source_kind, item_kind, quote, week,
):
    cp, reader, client, contexts, login = setup(tmp_path)
    create_course(client, login("admin"))
    reader.text = f"虚构资料：{quote}。"
    headers = login("lead")
    source_id = client.post("/api/teaching/courses/demo/sources", json={
        "kind": source_kind, "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=headers).json()["source_id"]
    path = f"/api/teaching/courses/demo/sources/{source_id}/outlines"
    item = {"item_id": "item-1", "kind": item_kind, "block_index": 1,
            "quote": quote, "week_number": week}
    assert client.post(path, json={"items": [{**item, "quote": "不存在的内容"}]},
                       headers=headers).status_code == 422
    assert client.post(path, json={"items": [{**item, "kind": "WEEK_PLAN"}],
                                   "expected_weeks": [1]}, headers=headers).status_code == 422
    created = client.post(path, json={"items": [item]}, headers=headers)
    assert created.status_code == 200
    fetched = client.get(f"{path}/{created.json()['outline_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["items"][0]["quote"] == quote
    assert fetched.json()["items"][0]["citation"]["start_offset"] == len("虚构资料：")
    if source_kind == "LESSON_PLAN":
        assert fetched.json()["coverage"] == {
            "expected_weeks": [], "annotated_weeks": [week],
            "unannotated_expected_weeks": [], "unexpected_weeks": [],
        }


def test_outline_rejects_ambiguous_quotes_and_cross_course_ids(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    assert create_course(client, login("admin")).status_code == 200
    assert create_course(client, login("admin"), "another").status_code == 200
    reader.text = "第 1 周练习，继续第 1 周练习。"
    headers = login("lead")
    source_id = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "WEEKLY_PLAN", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=headers).json()["source_id"]
    path = f"/api/teaching/courses/demo/sources/{source_id}/outlines"
    item = {"item_id": "w1", "kind": "WEEK_PLAN", "block_index": 1,
            "quote": "第 1 周练习", "week_number": 1}
    assert client.post(path, json={"items": [item], "expected_weeks": [1]}, headers=headers).status_code == 422
    assert client.post(path, json={"items": [item, item], "expected_weeks": [1]}, headers=headers).status_code == 422
    assert client.post(path, json={"items": [{**item, "start_offset": 3}],
                                   "expected_weeks": [1]}, headers=headers).status_code == 422
    created = client.post(path, json={"items": [{**item, "start_offset": 0}],
                                        "expected_weeks": [1, 2]}, headers=headers)
    assert created.status_code == 200
    assert created.json()["coverage"]["unannotated_expected_weeks"] == [2]
    before = len(reader.calls)
    wrong_path = f"/api/teaching/courses/another/sources/{source_id}/outlines/{created.json()['outline_id']}"
    assert client.get(wrong_path).status_code == 404
    assert len(reader.calls) == before


def test_two_teachers_sources_and_outlines_are_private_until_lead_shares(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    assert create_course(client, login("admin")).status_code == 200
    lead_headers = login("lead")
    for actor_id in ("teacher", "teacher2"):
        assert client.post("/api/teaching/courses/demo/teachers", json={
            "actor_id": actor_id, "reason": "课程负责人确认任课教师",
        }, headers=lead_headers).status_code == 200

    first_headers = login("teacher")
    first = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "SYLLABUS", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=first_headers).json()["source_id"]
    first_path = f"/api/teaching/courses/demo/sources/{first}"
    assert client.post(f"{first_path}/outlines", json={"items": [{
        "item_id": "objective-1", "kind": "OBJECTIVE", "block_index": 1,
        "quote": "以虚构示例讲授递归",
    }]}, headers=first_headers).status_code == 200

    second_headers = login("teacher2")
    second = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "LESSON_PLAN", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=second_headers).json()["source_id"]
    second_path = f"/api/teaching/courses/demo/sources/{second}"
    second_outline = client.post(f"{second_path}/outlines", json={"items": [{
        "item_id": "lesson-1", "kind": "LESSON_FRAGMENT", "block_index": 1,
        "quote": "以虚构示例讲授递归", "week_number": 2,
    }]}, headers=second_headers).json()["outline_id"]
    assert [row["source_id"] for row in client.get("/api/teaching/courses/demo/sources").json()["sources"]] == [second]
    before = len(reader.calls)
    assert client.get(first_path).status_code == 403
    assert client.get(f"{first_path}/outlines").status_code == 403
    assert len(reader.calls) == before

    login("teacher")
    assert [row["source_id"] for row in client.get("/api/teaching/courses/demo/sources").json()["sources"]] == [first]
    before = len(reader.calls)
    assert client.get(second_path).status_code == 403
    assert client.get(f"{second_path}/outlines/{second_outline}").status_code == 403
    assert len(reader.calls) == before
    assert client.post(f"{second_path}/readers", json={
        "actor_id": "teacher", "reason": "自行申请访问其他教师来源",
    }, headers=first_headers).status_code == 403

    lead_headers = login("lead")
    assert {row["source_id"] for row in client.get("/api/teaching/courses/demo/sources").json()["sources"]} == {
        first, second,
    }
    reader.allowed.remove("lead")
    assert client.get(second_path).status_code == 403
    reader.allowed.add("lead")
    share_path = f"{second_path}/readers"
    share = {"actor_id": "teacher", "reason": "负责人同意共同备课"}
    assert client.post(share_path, json=share, headers=lead_headers).status_code == 200
    assert client.post(share_path, json=share, headers=lead_headers).status_code == 409
    assert client.post(share_path, json={"actor_id": "outsider", "reason": "尝试共享给非成员"},
                       headers=lead_headers).status_code == 403
    assert client.get(share_path).json()["readers"][0]["actor_id"] == "teacher"
    login("teacher")
    assert {row["source_id"] for row in client.get("/api/teaching/courses/demo/sources").json()["sources"]} == {
        first, second,
    }
    assert client.get(second_path).status_code == 200
    assert client.get(f"{second_path}/outlines/{second_outline}").status_code == 200
    assert client.get(share_path).status_code == 403

    lead_headers = login("lead")
    assert client.delete(f"{share_path}/teacher", params={"reason": "负责人结束共同备课访问"},
                         headers=lead_headers).status_code == 200
    login("teacher")
    before = len(reader.calls)
    assert client.get(f"{second_path}/outlines/{second_outline}").status_code == 403
    assert len(reader.calls) == before
    lead_headers = login("lead")
    assert client.post(share_path, json=share, headers=lead_headers).status_code == 200
    assert client.delete("/api/teaching/courses/demo/teachers/teacher",
                         params={"reason": "课程负责人撤销课程成员"}, headers=lead_headers).status_code == 200
    assert client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "课程负责人重新加入成员",
    }, headers=lead_headers).status_code == 200
    login("teacher")
    assert [row["source_id"] for row in client.get("/api/teaching/courses/demo/sources").json()["sources"]] == [first]
    assert client.get(second_path).status_code == 403
    assert {event["event_type"] for event in cp.store.list_tenant_audits("tenant")} >= {
        "TEACHING_SOURCE_READER_GRANTED", "TEACHING_SOURCE_READER_REVOKED", "TEACHING_ACCESS_DENIED",
    }


def test_source_share_revoked_during_remote_read_does_not_return_content(tmp_path):
    cp, reader, client, contexts, login = setup(tmp_path)
    assert create_course(client, login("admin")).status_code == 200
    lead_headers = login("lead")
    assert client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "课程负责人确认任课教师",
    }, headers=lead_headers).status_code == 200
    source_id = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "SYLLABUS", "document_url": DOC_URL, "attested_no_student_data": True,
    }, headers=lead_headers).json()["source_id"]
    path = f"/api/teaching/courses/demo/sources/{source_id}"
    assert client.post(f"{path}/readers", json={
        "actor_id": "teacher", "reason": "负责人共享课程资料",
    }, headers=lead_headers).status_code == 200
    original_read = reader.read_document

    def revoke_during_read(identity, document_id):
        document = original_read(identity, document_id)
        cp.store.connection.execute(
            "UPDATE teaching_source_readers SET active = 0 WHERE source_id = ? AND actor_id = ?",
            (source_id, "teacher"),
        )
        cp.store.connection.commit()
        return document

    reader.read_document = revoke_during_read
    login("teacher")
    assert client.get(path).status_code == 403
    assert client.post(f"{path}/outlines", json={"items": [{
        "item_id": "objective-1", "kind": "OBJECTIVE", "block_index": 1,
        "quote": "以虚构示例讲授递归",
    }]}, headers=login("teacher")).status_code == 403
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_outlines").fetchone()[0] == 0
