"""Authenticated, read-only-by-default Feishu course API."""

from __future__ import annotations

import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .feishu_oauth_web import SQLiteOAuthSessionIssuer, build_session_dependency
from .meeting_review_web import csrf_token
from .models import IdentityContext
from .teaching import SourceChanged, TeachingService
from .teaching_alignment import TeachingAlignmentService
from .teaching_execution import TeachingSourceExecutionService


class CourseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course_id: StrictStr = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    title: StrictStr = Field(min_length=2, max_length=200)
    term: StrictStr = Field(min_length=2, max_length=100)
    lead_actor_id: StrictStr = Field(min_length=1, max_length=100)


class TeacherGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_id: StrictStr = Field(min_length=1, max_length=100)
    reason: StrictStr = Field(min_length=5, max_length=500)


class SourceRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["SYLLABUS", "WEEKLY_PLAN", "LESSON_PLAN", "MATERIAL"]
    document_url: StrictStr = Field(min_length=20, max_length=1000)
    attested_no_student_data: StrictBool


class TeachingOutlineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: StrictStr = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["OBJECTIVE", "WEEK_PLAN", "LESSON_FRAGMENT"]
    block_index: StrictInt = Field(ge=1, le=1000)
    quote: StrictStr = Field(min_length=2, max_length=1000)
    week_number: StrictInt | None = Field(default=None, ge=1, le=60)
    start_offset: StrictInt | None = Field(default=None, ge=0)


class TeachingOutlineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[TeachingOutlineItem] = Field(min_length=1, max_length=100)
    expected_weeks: list[StrictInt] = Field(default_factory=list, max_length=60)


class AlignmentReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: StrictStr = Field(min_length=1, max_length=100)
    outline_id: StrictStr = Field(min_length=1, max_length=100)


class AlignmentLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective_item_id: StrictStr = Field(min_length=1, max_length=80)
    week_item_id: StrictStr = Field(min_length=1, max_length=80)


class AlignmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    syllabus: AlignmentReference
    weekly_plan: AlignmentReference
    lesson_plan: AlignmentReference
    links: list[AlignmentLink] = Field(default_factory=list, max_length=100)


class AlignmentReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["ACCEPTED", "RETURNED"]
    reason_code: Literal["EVIDENCE_CONFIRMED", "SOURCE_INCOMPLETE",
                         "MAPPING_INCORRECT", "EVIDENCE_INSUFFICIENT"]


class TeachingExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: StrictStr = Field(min_length=1, max_length=100)
    agent_version: StrictInt = Field(ge=1)


def build_teaching_router(service: TeachingService, sessions: SQLiteOAuthSessionIssuer,
                          *, public_origin: str,
                          extraction: TeachingSourceExecutionService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/teaching")
    require_identity = build_session_dependency(sessions)
    alignment = TeachingAlignmentService(service)

    def check_csrf(request: Request) -> None:
        origin = request.headers.get("origin")
        expected = csrf_token(request.cookies.get("fde_auth_session", ""))
        if (origin is not None and origin != public_origin) or not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), expected,
        ):
            raise HTTPException(403, "请求校验失败，请重新加载页面")

    def call(action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except SourceChanged as exc:
            raise HTTPException(409, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            status = 409 if str(exc) in {
                "course already exists", "teacher is already active", "teacher is not active",
                "source reader is already active", "source reader is not active",
                "alignment was already reviewed",
            } else 422
            raise HTTPException(status, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, "当前无法以成员授权读取飞书文档") from exc

    @router.get("/courses")
    def courses(request: Request, identity: IdentityContext = Depends(require_identity)):
        return {"courses": call(service.list_courses, identity),
                "csrf_token": csrf_token(request.cookies["fde_auth_session"])}

    @router.post("/courses")
    def create_course(body: CourseCreateRequest, request: Request,
                      identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.create_course, identity, body.course_id, body.title, body.term, body.lead_actor_id)

    @router.get("/courses/{course_id}")
    def course_detail(course_id: str, identity: IdentityContext = Depends(require_identity)):
        return call(service.course_detail, identity, course_id)

    @router.post("/courses/{course_id}/teachers")
    def grant_teacher(course_id: str, body: TeacherGrantRequest, request: Request,
                      identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.set_teacher, identity, course_id, body.actor_id, active=True, reason=body.reason)

    @router.delete("/courses/{course_id}/teachers/{teacher_actor_id}")
    def revoke_teacher(course_id: str, teacher_actor_id: str, reason: str, request: Request,
                       identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        if not 5 <= len(reason) <= 500:
            raise HTTPException(422, "撤权原因长度不合法")
        return call(service.set_teacher, identity, course_id, teacher_actor_id,
                    active=False, reason=reason)

    @router.get("/courses/{course_id}/sources")
    def list_sources(course_id: str, identity: IdentityContext = Depends(require_identity)):
        return {"sources": call(service.list_sources, identity, course_id)}

    @router.post("/courses/{course_id}/sources")
    def register_source(course_id: str, body: SourceRegisterRequest, request: Request,
                        identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.register_source, identity, course_id, body.kind, body.document_url,
                    attested_no_student_data=body.attested_no_student_data)

    @router.get("/courses/{course_id}/sources/{source_id}")
    def read_source(course_id: str, source_id: str, identity: IdentityContext = Depends(require_identity)):
        return call(service.read_source, identity, course_id, source_id)

    @router.get("/courses/{course_id}/sources/{source_id}/diagnostics")
    def diagnose_source(course_id: str, source_id: str,
                        identity: IdentityContext = Depends(require_identity)):
        return call(service.diagnose_source, identity, course_id, source_id)

    @router.get("/courses/{course_id}/sources/{source_id}/readers")
    def list_source_readers(course_id: str, source_id: str,
                            identity: IdentityContext = Depends(require_identity)):
        return {"readers": call(service.list_source_readers, identity, course_id, source_id)}

    @router.post("/courses/{course_id}/sources/{source_id}/readers")
    def grant_source_reader(course_id: str, source_id: str, body: TeacherGrantRequest, request: Request,
                            identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.set_source_reader, identity, course_id, source_id, body.actor_id,
                    active=True, reason=body.reason)

    @router.delete("/courses/{course_id}/sources/{source_id}/readers/{teacher_actor_id}")
    def revoke_source_reader(course_id: str, source_id: str, teacher_actor_id: str, reason: str,
                             request: Request, identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        if not 5 <= len(reason) <= 500:
            raise HTTPException(422, "撤权原因长度不合法")
        return call(service.set_source_reader, identity, course_id, source_id, teacher_actor_id,
                    active=False, reason=reason)

    @router.get("/courses/{course_id}/sources/{source_id}/outlines")
    def list_outlines(course_id: str, source_id: str, identity: IdentityContext = Depends(require_identity)):
        return {"outlines": call(service.list_outlines, identity, course_id, source_id)}

    @router.post("/courses/{course_id}/sources/{source_id}/outlines")
    def create_outline(course_id: str, source_id: str, body: TeachingOutlineRequest, request: Request,
                       identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(service.create_outline, identity, course_id, source_id,
                    [item.model_dump() for item in body.items], body.expected_weeks)

    @router.get("/courses/{course_id}/sources/{source_id}/outlines/{outline_id}")
    def read_outline(course_id: str, source_id: str, outline_id: str,
                     identity: IdentityContext = Depends(require_identity)):
        return call(service.read_outline, identity, course_id, source_id, outline_id)

    if extraction is not None:
        @router.post("/courses/{course_id}/sources/{source_id}/extractions")
        def execute_source(course_id: str, source_id: str, body: TeachingExtractionRequest,
                           request: Request, identity: IdentityContext = Depends(require_identity)):
            check_csrf(request)
            return call(extraction.execute, identity, course_id, source_id,
                        body.agent_id, body.agent_version)

        @router.get("/courses/{course_id}/extractions/{task_run_id}")
        def read_extraction(course_id: str, task_run_id: str,
                            identity: IdentityContext = Depends(require_identity)):
            return call(extraction.read_candidates, identity, course_id, task_run_id)

        @router.post("/courses/{course_id}/extractions/{task_run_id}/reconcile")
        def reconcile_extraction(course_id: str, task_run_id: str, request: Request,
                                 identity: IdentityContext = Depends(require_identity)):
            check_csrf(request)
            return call(extraction.reconcile_stale, identity, course_id, task_run_id)

    @router.post("/courses/{course_id}/alignments")
    def create_alignment(course_id: str, body: AlignmentCreateRequest, request: Request,
                         identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        references = {role: getattr(body, role).model_dump()
                      for role in ("syllabus", "weekly_plan", "lesson_plan")}
        return call(alignment.create, identity, course_id, references,
                    [link.model_dump() for link in body.links])

    @router.get("/courses/{course_id}/alignments")
    def list_alignments(course_id: str, identity: IdentityContext = Depends(require_identity)):
        return {"alignments": call(alignment.list, identity, course_id)}

    @router.get("/courses/{course_id}/alignments/{alignment_id}")
    def read_alignment(course_id: str, alignment_id: str,
                       identity: IdentityContext = Depends(require_identity)):
        return call(alignment.read, identity, course_id, alignment_id)

    @router.post("/courses/{course_id}/alignments/{alignment_id}/review")
    def review_alignment(course_id: str, alignment_id: str, body: AlignmentReviewRequest,
                         request: Request, identity: IdentityContext = Depends(require_identity)):
        check_csrf(request)
        return call(alignment.review, identity, course_id, alignment_id,
                    body.outcome, body.reason_code)

    return router
