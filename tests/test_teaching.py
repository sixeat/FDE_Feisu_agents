"""Feishu-only course and fresh-source slice; all documents are synthetic."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fde_control_plane import Actor, ActorType, ControlPlane, IdentityContext, SQLiteOAuthSessionIssuer, SQLiteStore
from fde_control_plane.teaching import TeachingService
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

    def read_document(self, identity, document_id):
        self.calls.append((identity.actor_id, document_id))
        if identity.actor_id not in self.allowed:
            raise PermissionError("member cannot read the Feishu document")
        snapshot = {"document_id": document_id, "revision_id": self.revision, "title": self.title,
                    "blocks": self.blocks or [{"index": 1, "block_type": 2, "text": self.text}]}
        if self.change_during_read:
            self.revision = str(int(self.revision) + 1)
        return snapshot

    def get_revision(self, identity, document_id):
        self.calls.append((identity.actor_id, document_id))
        if identity.actor_id not in self.allowed:
            raise PermissionError("member cannot read the Feishu document")
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
