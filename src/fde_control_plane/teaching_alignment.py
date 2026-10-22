"""Evidence-bound, human-proposed course alignment for local teaching review."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from typing import Any

from .models import IdentityContext
from .teaching import SourceChanged, TeachingService


ROLES = {"syllabus": "SYLLABUS", "weekly_plan": "WEEKLY_PLAN", "lesson_plan": "LESSON_PLAN"}
RETURN_REASONS = {"SOURCE_INCOMPLETE", "MAPPING_INCORRECT", "EVIDENCE_INSUFFICIENT"}


class TeachingAlignmentService:
    def __init__(self, teaching: TeachingService) -> None:
        self.teaching = teaching
        self.store = teaching.store

    def _read_inputs(self, identity: IdentityContext, course_id: str,
                     references: dict[str, dict[str, str]]) -> tuple[dict[str, Any], dict[str, Any]]:
        if set(references) != set(ROLES):
            raise ValueError("syllabus, weekly plan and lesson plan references are required")
        if len({ref["source_id"] for ref in references.values()}) != len(ROLES):
            raise ValueError("alignment sources must be distinct")
        pinned: dict[str, Any] = {}
        outlines: dict[str, Any] = {}
        for role, kind in ROLES.items():
            ref = references[role]
            outline = self.teaching.read_outline(identity, course_id, ref["source_id"], ref["outline_id"])
            source = self.store.get_teaching_source(identity.tenant_id, course_id, ref["source_id"])
            if source is None or source["kind"] != kind:
                raise ValueError(f"{role} source kind is invalid")
            outlines[role] = outline
            pinned[role] = {
                "source_id": ref["source_id"], "outline_id": ref["outline_id"],
                "source_revision": outline["source_revision"],
                "source_hash": source["content_hash"], "outline_hash": outline["outline_hash"],
            }
        return pinned, outlines

    @staticmethod
    def _item(role: str, item: dict[str, Any]) -> dict[str, Any]:
        return {"role": role, "item_id": item["item_id"], "citation": item["citation"]}

    def _candidates(self, outlines: dict[str, Any], links: list[dict[str, str]],
                    references: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        objectives = {item["item_id"]: item for item in outlines["syllabus"]["items"]}
        weeks = {item["item_id"]: item for item in outlines["weekly_plan"]["items"]}
        lessons = outlines["lesson_plan"]["items"]
        seen: set[tuple[str, str]] = set()
        for link in links:
            pair = (link["objective_item_id"], link["week_item_id"])
            if pair in seen or pair[0] not in objectives or pair[1] not in weeks:
                raise ValueError("alignment link is duplicate or references an unknown item")
            seen.add(pair)
        normalized = [{"objective_item_id": objective, "week_item_id": week}
                      for objective, week in sorted(seen)]
        linked_objectives = {objective for objective, _ in seen}
        linked_weeks = {week for _, week in seen}
        week_numbers = {item["week_number"] for item in weeks.values()}
        lesson_numbers = {item["week_number"] for item in lessons if item["week_number"] is not None}
        issues: list[dict[str, Any]] = []

        def add(code: str, *, week: int | None = None,
                items: list[dict[str, Any]] | None = None) -> None:
            issues.append({"code": code, "week_number": week, "evidence": items or []})

        for item in objectives.values():
            if item["item_id"] not in linked_objectives:
                add("OBJECTIVE_UNLINKED", items=[self._item("syllabus", item)])
        for item in weeks.values():
            if item["item_id"] not in linked_weeks:
                add("WEEK_WITHOUT_OBJECTIVE_LINK", week=item["week_number"],
                    items=[self._item("weekly_plan", item)])
            if item["week_number"] not in lesson_numbers:
                add("WEEK_WITHOUT_LESSON_ANNOTATION", week=item["week_number"],
                    items=[self._item("weekly_plan", item)])
        for item in lessons:
            if item["week_number"] is not None and item["week_number"] not in week_numbers:
                add("LESSON_WITHOUT_WEEK_PLAN_ANNOTATION", week=item["week_number"],
                    items=[self._item("lesson_plan", item)])
        for week in outlines["weekly_plan"]["coverage"]["unannotated_expected_weeks"]:
            add("EXPECTED_WEEK_UNANNOTATED", week=week, items=[{
                "role": "weekly_plan", "source_id": references["weekly_plan"]["source_id"],
                "outline_id": references["weekly_plan"]["outline_id"],
                "source_revision": references["weekly_plan"]["source_revision"],
            }])
        return normalized, issues

    def _check_current(self, connection: sqlite3.Connection, identity: IdentityContext,
                       course_id: str, references: dict[str, Any], *, lead: bool = False) -> None:
        actor = connection.execute(
            "SELECT active, external_ref_hash FROM actors WHERE tenant_id = ? AND actor_id = ?",
            (identity.tenant_id, identity.actor_id),
        ).fetchone()
        member = connection.execute(
            "SELECT role, active FROM teaching_course_members "
            "WHERE tenant_id = ? AND course_id = ? AND actor_id = ?",
            (identity.tenant_id, course_id, identity.actor_id),
        ).fetchone()
        if (actor is None or not actor["active"] or actor["external_ref_hash"] != identity.subject_ref_hash
                or member is None or not member["active"] or (lead and member["role"] != "LEAD")):
            raise PermissionError("course or lead authorization was revoked during alignment")
        for ref in references.values():
            source = connection.execute(
                "SELECT revision_id, content_hash, submitted_by, data_reviewed FROM teaching_sources "
                "WHERE tenant_id = ? AND course_id = ? AND source_id = ?",
                (identity.tenant_id, course_id, ref["source_id"]),
            ).fetchone()
            outline = connection.execute(
                "SELECT outline_hash FROM teaching_outlines WHERE tenant_id = ? AND course_id = ? "
                "AND source_id = ? AND outline_id = ?",
                (identity.tenant_id, course_id, ref["source_id"], ref["outline_id"]),
            ).fetchone()
            if (source is None or not source["data_reviewed"]
                    or source["revision_id"] != ref["source_revision"]
                    or source["content_hash"] != ref["source_hash"]
                    or outline is None or outline["outline_hash"] != ref["outline_hash"]):
                raise SourceChanged("alignment reference changed during review")
            if member["role"] != "LEAD" and source["submitted_by"] != identity.actor_id:
                shared = connection.execute(
                    "SELECT active FROM teaching_source_readers WHERE tenant_id = ? AND course_id = ? "
                    "AND source_id = ? AND actor_id = ?",
                    (identity.tenant_id, course_id, ref["source_id"], identity.actor_id),
                ).fetchone()
                if shared is None or not shared["active"]:
                    raise PermissionError("source sharing was revoked during alignment")

    def create(self, identity: IdentityContext, course_id: str,
               references: dict[str, dict[str, str]], links: list[dict[str, str]]) -> dict[str, Any]:
        self.teaching._course_member(identity, course_id)
        pinned, outlines = self._read_inputs(identity, course_id, references)
        normalized, issues = self._candidates(outlines, links, pinned)
        payload = {"schema_version": "teaching-alignment.v1", "references": pinned,
                   "links": normalized, "issues": issues}
        digest = hashlib.sha256(self.store._json(payload).encode()).hexdigest()
        alignment_id = "alignment-" + secrets.token_hex(12)
        event = self.teaching._audit(identity, "TEACHING_ALIGNMENT_CREATED",
                                     {"course_id": course_id, "alignment_id": alignment_id, "hash": digest})
        with self.store.teaching_transaction() as connection:
            self._check_current(connection, identity, course_id, pinned)
            connection.execute(
                "INSERT INTO teaching_alignments(alignment_id, tenant_id, course_id, input_hash, "
                "data_json, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                (alignment_id, identity.tenant_id, course_id, digest,
                 self.store._json(payload), identity.actor_id),
            )
            self.store.teaching_audit_in_transaction(connection, event)
        return self._render(alignment_id, "PENDING_REVIEW", payload, outlines)

    @staticmethod
    def _render(alignment_id: str, status: str, payload: dict[str, Any],
                outlines: dict[str, Any]) -> dict[str, Any]:
        return {"alignment_id": alignment_id, "status": status,
                "references": payload["references"], "links": payload["links"],
                "issues": payload["issues"],
                "items": {role: outline["items"] for role, outline in outlines.items()}}

    def read(self, identity: IdentityContext, course_id: str, alignment_id: str) -> dict[str, Any]:
        self.teaching._course_member(identity, course_id)
        row = self.store.get_teaching_alignment(identity.tenant_id, course_id, alignment_id)
        if row is None:
            raise KeyError("teaching alignment is not in this course")
        payload = json.loads(row["data_json"])
        if (payload.get("schema_version") != "teaching-alignment.v1"
                or hashlib.sha256(self.store._json(payload).encode()).hexdigest() != row["input_hash"]):
            raise SourceChanged("alignment integrity check failed")
        references = {role: {"source_id": ref["source_id"], "outline_id": ref["outline_id"]}
                      for role, ref in payload["references"].items()}
        current, outlines = self._read_inputs(identity, course_id, references)
        if current != payload["references"]:
            raise SourceChanged("alignment source or annotation changed")
        self._check_current(self.store.connection, identity, course_id, current)
        review = self.store.get_teaching_alignment_review(alignment_id)
        return self._render(alignment_id, review["outcome"] if review else "PENDING_REVIEW", payload, outlines)

    def list(self, identity: IdentityContext, course_id: str) -> list[dict[str, Any]]:
        course = self.teaching._course_member(identity, course_id)
        results = []
        for row in self.store.list_teaching_alignments(identity.tenant_id, course_id):
            payload = json.loads(row["data_json"])
            if (payload.get("schema_version") != "teaching-alignment.v1"
                    or hashlib.sha256(self.store._json(payload).encode()).hexdigest() != row["input_hash"]):
                raise SourceChanged("alignment integrity check failed")
            visible = True
            for ref in payload["references"].values():
                source = self.store.get_teaching_source(identity.tenant_id, course_id, ref["source_id"])
                if source is None:
                    visible = False
                    break
                if course["role"] != "LEAD" and source["submitted_by"] != identity.actor_id:
                    reader = self.store.get_teaching_source_reader(
                        identity.tenant_id, course_id, ref["source_id"], identity.actor_id,
                    )
                    if reader is None or not reader["active"]:
                        visible = False
                        break
            if not visible:
                continue
            review = self.store.get_teaching_alignment_review(row["alignment_id"])
            results.append({"alignment_id": row["alignment_id"],
                            "status": review["outcome"] if review else "PENDING_REVIEW",
                            "issue_count": len(payload["issues"]), "created_by": row["created_by"],
                            "created_at": row["created_at"]})
        return results

    def review(self, identity: IdentityContext, course_id: str, alignment_id: str,
               outcome: str, reason_code: str) -> dict[str, Any]:
        if (outcome not in {"ACCEPTED", "RETURNED"}
                or (outcome == "ACCEPTED" and reason_code != "EVIDENCE_CONFIRMED")
                or (outcome == "RETURNED" and reason_code not in RETURN_REASONS)):
            raise ValueError("alignment review outcome and reason do not match")
        self.teaching._course_member(identity, course_id, lead=True)
        alignment = self.read(identity, course_id, alignment_id)
        if alignment["status"] != "PENDING_REVIEW":
            raise ValueError("alignment was already reviewed")
        event = self.teaching._audit(identity, "TEACHING_ALIGNMENT_REVIEWED",
                                     {"course_id": course_id, "alignment_id": alignment_id,
                                      "outcome": outcome, "reason_code": reason_code})
        with self.store.teaching_transaction() as connection:
            self._check_current(connection, identity, course_id, alignment["references"], lead=True)
            if connection.execute(
                "SELECT 1 FROM teaching_alignment_reviews WHERE alignment_id = ?", (alignment_id,)
            ).fetchone():
                raise ValueError("alignment was already reviewed")
            connection.execute(
                "INSERT INTO teaching_alignment_reviews(alignment_id, tenant_id, course_id, outcome, "
                "reason, reviewed_by) VALUES (?, ?, ?, ?, ?, ?)",
                (alignment_id, identity.tenant_id, course_id, outcome, reason_code, identity.actor_id),
            )
            self.store.teaching_audit_in_transaction(connection, event)
        return {"alignment_id": alignment_id, "status": outcome, "reason_code": reason_code,
                "reviewed_by": identity.actor_id}
