"""Evidence-bound preparation checklist from lead-reviewed source extractions."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from .models import IdentityContext
from .teaching import SourceChanged
from .teaching_execution import TeachingSourceExecutionService


_KINDS = {"syllabus": "SYLLABUS", "weekly_plan": "WEEKLY_PLAN",
          "lesson_plan": "LESSON_PLAN"}
_TITLES = {
    "OBJECTIVE_MAPPING_TO_CONFIRM": "讨论该教学目标对应的周次和教案",
    "WEEK_PREPARATION": "核对本周计划与教案的备课安排",
    "LESSON_NOT_ANNOTATED": "核对本周是否缺少教案标注",
    "PLAN_NOT_ANNOTATED": "核对这份教案对应的周计划",
}


class TeachingPreparationService:
    def __init__(self, extraction: TeachingSourceExecutionService) -> None:
        self.extraction = extraction
        self.teaching = extraction.teaching
        self.store = extraction.store

    def _inputs(self, identity: IdentityContext, course_id: str,
                task_ids: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        if (set(task_ids) != set(_KINDS) or len(set(task_ids.values())) != 3
                or any(not isinstance(value, str) or not value for value in task_ids.values())):
            raise ValueError("three distinct extraction references are required")
        references: dict[str, Any] = {}
        results: dict[str, Any] = {}
        for role, kind in _KINDS.items():
            task_id = task_ids[role]
            result = self.extraction.read_candidates(identity, course_id, task_id)
            review = result["review"]
            if (result["source"]["kind"] != kind or result["status"] != "WAITING_REVIEW"
                    or review is None or review["outcome"] != "ACCEPTED"
                    or not review["coverage_confirmed"]
                    or any(item["verdict"] != "CONFIRMED" for item in review["decisions"])):
                raise ValueError("all three source extractions need accepted lead reviews")
            row = self.store.connection.execute(
                "SELECT output_hash FROM teaching_extraction_results WHERE task_run_id = ? "
                "AND tenant_id = ? AND course_id = ?",
                (task_id, identity.tenant_id, course_id),
            ).fetchone()
            review_row = self.store.connection.execute(
                "SELECT output_hash, outcome, reason_code, coverage_confirmed, decisions_json, "
                "reviewed_by FROM teaching_extraction_reviews WHERE task_run_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or review_row is None or row["output_hash"] != review_row["output_hash"]:
                raise SourceChanged("source review no longer matches extraction")
            review_hash = hashlib.sha256(self.store._json(dict(review_row)).encode()).hexdigest()
            references[role] = {"task_run_id": task_id, "source": result["source"],
                                "output_hash": row["output_hash"], "review_hash": review_hash}
            results[role] = result
        return references, results

    @staticmethod
    def _suggestions(results: dict[str, Any]) -> list[dict[str, Any]]:
        def cite(role: str, item: dict[str, Any]) -> dict[str, str]:
            return {"role": role, "item_id": item["item_id"]}

        suggestions = [{"code": "OBJECTIVE_MAPPING_TO_CONFIRM", "week_number": None,
                        "evidence": [cite("syllabus", item)]}
                       for item in results["syllabus"]["items"]]
        weeks = results["weekly_plan"]["items"]
        lessons = results["lesson_plan"]["items"]
        for week in weeks:
            matching = [item for item in lessons if item["week_number"] == week["week_number"]]
            suggestions.append({
                "code": "WEEK_PREPARATION" if matching else "LESSON_NOT_ANNOTATED",
                "week_number": week["week_number"],
                "evidence": [cite("weekly_plan", week)] +
                            [cite("lesson_plan", item) for item in matching],
            })
        planned = {item["week_number"] for item in weeks}
        suggestions.extend({"code": "PLAN_NOT_ANNOTATED", "week_number": item["week_number"],
                            "evidence": [cite("lesson_plan", item)]}
                           for item in lessons
                           if item["week_number"] is not None and item["week_number"] not in planned)
        return suggestions

    def _check_current(self, connection: Any, identity: IdentityContext, course_id: str,
                       references: dict[str, Any]) -> None:
        actor = connection.execute(
            "SELECT active, external_ref_hash FROM actors WHERE tenant_id = ? AND actor_id = ?",
            (identity.tenant_id, identity.actor_id),
        ).fetchone()
        member = connection.execute(
            "SELECT role, active FROM teaching_course_members WHERE tenant_id = ? "
            "AND course_id = ? AND actor_id = ?",
            (identity.tenant_id, course_id, identity.actor_id),
        ).fetchone()
        if (actor is None or not actor["active"] or actor["external_ref_hash"] != identity.subject_ref_hash
                or member is None or not member["active"]):
            raise PermissionError("course access was revoked during preparation")
        for ref in references.values():
            source_ref = ref["source"]
            source = connection.execute(
                "SELECT revision_id, content_hash, kind, submitted_by, data_reviewed FROM teaching_sources "
                "WHERE tenant_id = ? AND course_id = ? AND source_id = ?",
                (identity.tenant_id, course_id, source_ref["source_id"]),
            ).fetchone()
            result = connection.execute(
                "SELECT output_hash FROM teaching_extraction_results WHERE task_run_id = ?",
                (ref["task_run_id"],),
            ).fetchone()
            review = connection.execute(
                "SELECT output_hash, outcome, reason_code, coverage_confirmed, decisions_json, reviewed_by "
                "FROM teaching_extraction_reviews WHERE task_run_id = ?",
                (ref["task_run_id"],),
            ).fetchone()
            if (source is None or not source["data_reviewed"]
                    or any(source[key] != source_ref[key] for key in
                           ("revision_id", "content_hash", "kind"))
                    or result is None or result["output_hash"] != ref["output_hash"]
                    or review is None or review["outcome"] != "ACCEPTED"
                    or hashlib.sha256(self.store._json(dict(review)).encode()).hexdigest()
                    != ref["review_hash"]):
                raise SourceChanged("preparation source or review changed")
            if member["role"] != "LEAD" and source["submitted_by"] != identity.actor_id:
                shared = connection.execute(
                    "SELECT active FROM teaching_source_readers WHERE tenant_id = ? AND course_id = ? "
                    "AND source_id = ? AND actor_id = ?",
                    (identity.tenant_id, course_id, source_ref["source_id"], identity.actor_id),
                ).fetchone()
                if shared is None or not shared["active"]:
                    raise PermissionError("preparation source sharing was revoked")

    def create(self, identity: IdentityContext, course_id: str,
               task_ids: dict[str, str]) -> dict[str, Any]:
        self.teaching._course_member(identity, course_id)
        references, results = self._inputs(identity, course_id, task_ids)
        payload = {"schema_version": "teaching-preparation.v1", "references": references,
                   "suggestions": self._suggestions(results)}
        digest = hashlib.sha256(self.store._json(payload).encode()).hexdigest()
        draft_id = "preparation-" + secrets.token_hex(12)
        event = self.teaching._audit(identity, "TEACHING_PREPARATION_CREATED",
                                     {"course_id": course_id, "draft_id": draft_id,
                                      "input_hash": digest})
        with self.store.teaching_transaction() as connection:
            self._check_current(connection, identity, course_id, references)
            saved = connection.execute(
                "INSERT OR IGNORE INTO teaching_preparation_drafts "
                "(draft_id, tenant_id, course_id, created_by, input_hash, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (draft_id, identity.tenant_id, course_id, identity.actor_id,
                 digest, self.store._json(payload)),
            ).rowcount
            if saved:
                self.store.teaching_audit_in_transaction(connection, event)
            else:
                row = connection.execute(
                    "SELECT draft_id FROM teaching_preparation_drafts WHERE tenant_id = ? "
                    "AND course_id = ? AND created_by = ? AND input_hash = ?",
                    (identity.tenant_id, course_id, identity.actor_id, digest),
                ).fetchone()
                draft_id = row["draft_id"]
        return self._render(draft_id, payload, results)

    @staticmethod
    def _render(draft_id: str, payload: dict[str, Any],
                results: dict[str, Any]) -> dict[str, Any]:
        items = {role: {item["item_id"]: item for item in result["items"]}
                 for role, result in results.items()}
        suggestions = []
        for suggestion in payload["suggestions"]:
            evidence = []
            for ref in suggestion["evidence"]:
                role, item_id = ref["role"], ref["item_id"]
                evidence.append({"role": role, "source": payload["references"][role]["source"],
                                 "item": items[role][item_id]})
            suggestions.append({"code": suggestion["code"],
                                "title": _TITLES[suggestion["code"]],
                                "week_number": suggestion["week_number"], "evidence": evidence})
        return {"draft_id": draft_id, "status": "NEEDS_TEACHER_REVIEW",
                "suggestions": suggestions}

    def read(self, identity: IdentityContext, course_id: str, draft_id: str) -> dict[str, Any]:
        self.teaching._course_member(identity, course_id)
        row = self.store.connection.execute(
            "SELECT input_hash, data_json FROM teaching_preparation_drafts "
            "WHERE tenant_id = ? AND course_id = ? AND draft_id = ?",
            (identity.tenant_id, course_id, draft_id),
        ).fetchone()
        if row is None:
            raise KeyError("preparation draft is not in this course")
        payload = json.loads(row["data_json"])
        if (payload.get("schema_version") != "teaching-preparation.v1"
                or hashlib.sha256(self.store._json(payload).encode()).hexdigest() != row["input_hash"]):
            raise SourceChanged("preparation draft integrity check failed")
        task_ids = {role: ref["task_run_id"] for role, ref in payload["references"].items()}
        references, results = self._inputs(identity, course_id, task_ids)
        if references != payload["references"] or self._suggestions(results) != payload["suggestions"]:
            raise SourceChanged("preparation evidence changed")
        self._check_current(self.store.connection, identity, course_id, references)
        return self._render(draft_id, payload, results)


__all__ = ["TeachingPreparationService"]
