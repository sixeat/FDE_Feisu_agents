"""Feishu-only course and fresh-source slice; all documents are synthetic."""

from __future__ import annotations

import hashlib
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
        self.allowed = {"admin", "lead", "teacher"}
        self.revision = "1"
        self.text = "第 2 周：以虚构示例讲授递归。"
        self.calls: list[tuple[str, str]] = []
        self.change_during_read = False

    def read_document(self, identity, document_id):
        self.calls.append((identity.actor_id, document_id))
        if identity.actor_id not in self.allowed:
            raise PermissionError("member cannot read the Feishu document")
        snapshot = {"document_id": document_id, "revision_id": self.revision, "title": "虚构课程周计划",
                    "blocks": [{"index": 1, "block_type": 2, "text": self.text}]}
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
        ("teacher", set(), "tenant"), ("outsider", set(), "tenant"),
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
    source_request = {"kind": "WEEKLY_PLAN", "document_url": DOC_URL}
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
        "kind": "SYLLABUS", "document_url": DOC_URL,
    }, headers=lead_headers).json()
    login("outsider")
    before = len(reader.calls)
    assert client.get(f"/api/teaching/courses/demo/sources/{source['source_id']}").status_code == 403
    assert len(reader.calls) == before
    granted = client.post("/api/teaching/courses/demo/teachers", json={
        "actor_id": "teacher", "reason": "批准成员查看课程资料",
    }, headers=login("lead"))
    assert granted.status_code == 200
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
    source = client.post("/api/teaching/courses/demo/sources", json={
        "kind": "LESSON_PLAN", "document_url": DOC_URL,
    }, headers=login("lead")).json()
    reopened = SQLiteStore(tmp_path / "course.sqlite3")
    assert reopened.get_teaching_course("tenant", "demo")["term"] == "2026 秋"
    assert reopened.get_teaching_source("tenant", "demo", source["source_id"])["revision_id"] == "1"
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
        "kind": "MATERIAL", "document_url": DOC_URL,
    }, headers={"X-CSRF-Token": hashlib.sha256(("meeting-review-csrf:" + token).encode()).hexdigest(),
                "Origin": "https://example.test"}).status_code == 503
    assert cp.store.connection.execute("SELECT count(*) FROM teaching_sources").fetchone()[0] == 0
