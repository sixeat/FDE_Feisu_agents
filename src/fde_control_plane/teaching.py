"""Feishu-only course membership and fresh, member-authorized teaching sources."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Protocol

from .feishu_review import FeishuMeetingReviewGateway
from .models import ActorType, AuditEvent, IdentityContext
from .service import ControlPlane
from .user_document_access import DOC_READ_SCOPE


class UserTeachingDocumentReader(Protocol):
    def read_document(self, identity: IdentityContext, document_id: str) -> dict[str, Any]: ...
    def get_revision(self, identity: IdentityContext, document_id: str) -> str: ...


class SourceChanged(RuntimeError):
    """The pinned source is no longer the document revision and content read now."""


class TeachingService:
    def __init__(self, control_plane: ControlPlane, reader: UserTeachingDocumentReader | None) -> None:
        self.cp = control_plane
        self.store = control_plane.store
        self.reader = reader

    def _member(self, identity: IdentityContext):
        actor = self.cp.verify_identity(identity)
        binding = self.store.find_actor_by_external_hash(identity.tenant_id, identity.subject_ref_hash)
        if (identity.auth_mode != "user_oauth" or actor.actor_type != ActorType.USER
                or not actor.external_ref_hash or binding is None or binding["actor_id"] != actor.actor_id):
            raise PermissionError("a bound Feishu member OAuth identity is required")
        return actor

    def _course_member(self, identity: IdentityContext, course_id: str, *, lead: bool = False) -> dict[str, Any]:
        self._member(identity)
        course = self.store.get_teaching_course(identity.tenant_id, course_id)
        membership = self.store.get_teaching_member(identity.tenant_id, course_id, identity.actor_id)
        if course is None or membership is None or not membership["active"]:
            denied = self._audit(identity, "TEACHING_ACCESS_DENIED", {"course_id": course_id}, outcome="DENIED")
            self.store.save_audit(denied)
            raise PermissionError("course membership is missing or revoked")
        if lead and membership["role"] != "LEAD":
            denied = self._audit(identity, "TEACHING_ACCESS_DENIED",
                                 {"course_id": course_id, "role": "LEAD"}, outcome="DENIED")
            self.store.save_audit(denied)
            raise PermissionError("course lead authorization is required")
        return {"course_id": course_id, "title": course["title"], "term": course["term"],
                "role": membership["role"]}

    @staticmethod
    def _audit(identity: IdentityContext, event_type: str, payload: dict[str, Any],
               *, outcome: str = "SUCCEEDED") -> AuditEvent:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return AuditEvent(
            audit_id="teaching-" + secrets.token_hex(12), event_type=event_type,
            tenant_id=identity.tenant_id, actor_id=identity.actor_id, task_run_id=None,
            correlation_id=None, payload_hash=digest, outcome=outcome,
        )

    def create_course(self, identity: IdentityContext, course_id: str, title: str,
                      term: str, lead_actor_id: str) -> dict[str, Any]:
        actor = self._member(identity)
        if "admin" not in actor.roles:
            raise PermissionError("administrator authorization is required")
        lead = self.cp._actor(lead_actor_id)
        if (not lead.active or lead.tenant_id != identity.tenant_id
                or lead.actor_type != ActorType.USER or not lead.external_ref_hash):
            raise PermissionError("course lead must be an active, bound member of this tenant")
        event = self._audit(identity, "TEACHING_COURSE_CREATED", {"course_id": course_id, "lead": lead_actor_id})
        with self.store.teaching_transaction() as connection:
            admin = connection.execute(
                "SELECT active, roles_json FROM actors WHERE actor_id = ? AND tenant_id = ?",
                (identity.actor_id, identity.tenant_id),
            ).fetchone()
            current_lead = connection.execute(
                "SELECT active, actor_type, external_ref_hash FROM actors WHERE actor_id = ? AND tenant_id = ?",
                (lead_actor_id, identity.tenant_id),
            ).fetchone()
            if admin is None or not admin["active"] or "admin" not in json.loads(admin["roles_json"]):
                raise PermissionError("administrator authorization was revoked")
            if (current_lead is None or not current_lead["active"] or current_lead["actor_type"] != "USER"
                    or not current_lead["external_ref_hash"]):
                raise PermissionError("course lead authorization was revoked")
            if connection.execute(
                "SELECT 1 FROM teaching_courses WHERE tenant_id = ? AND course_id = ?",
                (identity.tenant_id, course_id),
            ).fetchone():
                raise ValueError("course already exists")
            connection.execute(
                "INSERT INTO teaching_courses(tenant_id, course_id, title, term, created_by) VALUES (?, ?, ?, ?, ?)",
                (identity.tenant_id, course_id, title, term, identity.actor_id),
            )
            connection.execute(
                "INSERT INTO teaching_course_members(tenant_id, course_id, actor_id, role, active) "
                "VALUES (?, ?, ?, 'LEAD', 1)",
                (identity.tenant_id, course_id, lead_actor_id),
            )
            self.store.teaching_audit_in_transaction(connection, event)
        return {"course_id": course_id, "title": title, "term": term, "lead_actor_id": lead_actor_id}

    def list_courses(self, identity: IdentityContext) -> list[dict[str, Any]]:
        self._member(identity)
        return [{"course_id": row["course_id"], "title": row["title"], "term": row["term"],
                 "role": row["role"]} for row in self.store.list_teaching_courses(identity.tenant_id, identity.actor_id)]

    def course_detail(self, identity: IdentityContext, course_id: str) -> dict[str, Any]:
        course = self._course_member(identity, course_id)
        course["members"] = [dict(row) for row in self.store.list_teaching_members(identity.tenant_id, course_id)]
        return course

    def set_teacher(self, identity: IdentityContext, course_id: str, teacher_actor_id: str,
                    *, active: bool, reason: str) -> dict[str, Any]:
        self._course_member(identity, course_id, lead=True)
        if active:
            teacher = self.cp._actor(teacher_actor_id)
            if (not teacher.active or teacher.tenant_id != identity.tenant_id
                    or teacher.actor_type != ActorType.USER or not teacher.external_ref_hash):
                raise PermissionError("teacher must be an active, bound member of this tenant")
        event = self._audit(identity, "TEACHING_TEACHER_GRANTED" if active else "TEACHING_TEACHER_REVOKED",
                            {"course_id": course_id, "teacher": teacher_actor_id, "reason": reason})
        with self.store.teaching_transaction() as connection:
            lead = connection.execute(
                "SELECT role, active FROM teaching_course_members WHERE tenant_id = ? AND course_id = ? AND actor_id = ?",
                (identity.tenant_id, course_id, identity.actor_id),
            ).fetchone()
            if lead is None or lead["role"] != "LEAD" or not lead["active"]:
                raise PermissionError("course lead authorization was revoked")
            if active:
                target = connection.execute(
                    "SELECT active, actor_type, external_ref_hash FROM actors WHERE actor_id = ? AND tenant_id = ?",
                    (teacher_actor_id, identity.tenant_id),
                ).fetchone()
                if (target is None or not target["active"] or target["actor_type"] != "USER"
                        or not target["external_ref_hash"]):
                    raise PermissionError("teacher authorization was revoked")
            current = connection.execute(
                "SELECT role, active FROM teaching_course_members WHERE tenant_id = ? AND course_id = ? AND actor_id = ?",
                (identity.tenant_id, course_id, teacher_actor_id),
            ).fetchone()
            if current is not None and current["role"] != "TEACHER":
                raise PermissionError("course lead cannot be changed through teacher membership")
            if active:
                if current is not None and current["active"]:
                    raise ValueError("teacher is already active")
                connection.execute(
                    "INSERT INTO teaching_course_members(tenant_id, course_id, actor_id, role, active) "
                    "VALUES (?, ?, ?, 'TEACHER', 1) ON CONFLICT(tenant_id, course_id, actor_id) "
                    "DO UPDATE SET active = 1, updated_at = CURRENT_TIMESTAMP",
                    (identity.tenant_id, course_id, teacher_actor_id),
                )
            else:
                if current is None or not current["active"]:
                    raise ValueError("teacher is not active")
                connection.execute(
                    "UPDATE teaching_course_members SET active = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = ? AND course_id = ? AND actor_id = ?",
                    (identity.tenant_id, course_id, teacher_actor_id),
                )
            self.store.teaching_audit_in_transaction(connection, event)
        return {"course_id": course_id, "teacher_actor_id": teacher_actor_id, "active": active}

    @staticmethod
    def _snapshot(document: dict[str, Any], document_id: str) -> tuple[str, str, list[dict[str, Any]]]:
        if document.get("document_id") != document_id:
            raise ValueError("document identity does not match the requested source")
        revision = str(document.get("revision_id") or "")
        raw_blocks = document.get("blocks")
        if not revision or not isinstance(raw_blocks, list) or not 1 <= len(raw_blocks) <= 1000:
            raise ValueError("document revision or blocks are invalid")
        blocks: list[dict[str, Any]] = []
        total_size = 0
        for block in raw_blocks:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                raise ValueError("document block is invalid")
            text = block["text"]
            total_size += len(text.encode("utf-8"))
            if total_size > 1_000_000:
                raise ValueError("document is too large for this read-only slice")
            try:
                blocks.append({"index": int(block["index"]), "block_type": int(block["block_type"]), "text": text})
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("document block fields are invalid") from exc
        if [block["index"] for block in blocks] != list(range(1, len(blocks) + 1)):
            raise ValueError("document block indices are invalid")
        digest = hashlib.sha256(json.dumps(blocks, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        return revision, digest, blocks

    def _fresh_read(self, identity: IdentityContext, document_id: str) -> tuple[str, str, list[dict[str, Any]], str]:
        if DOC_READ_SCOPE not in identity.scopes:
            raise PermissionError("Feishu document read scope is required")
        if self.reader is None:
            raise RuntimeError("member document reading is not configured")
        document = self.reader.read_document(identity, document_id)
        revision, digest, blocks = self._snapshot(document, document_id)
        if self.reader.get_revision(identity, document_id) != revision:
            raise SourceChanged("document changed during the read")
        return revision, digest, blocks, str(document.get("title") or "未命名文档")

    def register_source(self, identity: IdentityContext, course_id: str, kind: str,
                        document_url: str) -> dict[str, Any]:
        self._course_member(identity, course_id)
        document_id = FeishuMeetingReviewGateway.document_id_from_url(document_url)
        revision, digest, blocks, _ = self._fresh_read(identity, document_id)
        source_id = "source-" + secrets.token_hex(12)
        event = self._audit(identity, "TEACHING_SOURCE_REGISTERED",
                            {"course_id": course_id, "source_id": source_id, "revision": revision, "hash": digest})
        with self.store.teaching_transaction() as connection:
            actor = connection.execute(
                "SELECT active, external_ref_hash FROM actors WHERE actor_id = ? AND tenant_id = ?",
                (identity.actor_id, identity.tenant_id),
            ).fetchone()
            if actor is None or not actor["active"] or actor["external_ref_hash"] != identity.subject_ref_hash:
                raise PermissionError("member identity was revoked during the read")
            member = connection.execute(
                "SELECT active FROM teaching_course_members WHERE tenant_id = ? AND course_id = ? AND actor_id = ?",
                (identity.tenant_id, course_id, identity.actor_id),
            ).fetchone()
            if member is None or not member["active"]:
                raise PermissionError("course membership was revoked during the read")
            connection.execute(
                "INSERT INTO teaching_sources(source_id, tenant_id, course_id, kind, document_id, revision_id, "
                "content_hash, block_count, submitted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, identity.tenant_id, course_id, kind, document_id,
                 revision, digest, len(blocks), identity.actor_id),
            )
            self.store.teaching_audit_in_transaction(connection, event)
        return {"source_id": source_id, "course_id": course_id, "kind": kind,
                "revision_id": revision, "content_hash": digest, "block_count": len(blocks)}

    def list_sources(self, identity: IdentityContext, course_id: str) -> list[dict[str, Any]]:
        self._course_member(identity, course_id)
        return [dict(row) for row in self.store.list_teaching_sources(identity.tenant_id, course_id)]

    def read_source(self, identity: IdentityContext, course_id: str, source_id: str) -> dict[str, Any]:
        self._course_member(identity, course_id)
        row = self.store.get_teaching_source(identity.tenant_id, course_id, source_id)
        if row is None:
            raise KeyError("teaching source is not in this course")
        revision, digest, blocks, title = self._fresh_read(identity, row["document_id"])
        if revision != row["revision_id"] or digest != row["content_hash"]:
            raise SourceChanged("source revision or content changed; register a new snapshot")
        # Membership can be revoked while the remote read is in progress.
        self._course_member(identity, course_id)
        self.store.save_audit(self._audit(identity, "TEACHING_SOURCE_READ",
                                         {"course_id": course_id, "source_id": source_id,
                                          "revision": revision, "hash": digest}))
        return {"source_id": source_id, "course_id": course_id, "kind": row["kind"],
                "revision_id": revision, "content_hash": digest, "title": title,
                "blocks": blocks}

    @staticmethod
    def _week_coverage(items: list[dict[str, Any]], expected_weeks: list[int]) -> dict[str, list[int]]:
        annotated = {item["week_number"] for item in items if item.get("week_number") is not None}
        expected = set(expected_weeks)
        return {"expected_weeks": sorted(expected), "annotated_weeks": sorted(annotated),
                "unannotated_expected_weeks": sorted(expected - annotated),
                "unexpected_weeks": sorted(annotated - expected) if expected_weeks else []}

    def create_outline(self, identity: IdentityContext, course_id: str, source_id: str,
                       items: list[dict[str, Any]], expected_weeks: list[int]) -> dict[str, Any]:
        source = self.read_source(identity, course_id, source_id)
        source_kind = source["kind"]
        item_kind = {"SYLLABUS": "OBJECTIVE", "WEEKLY_PLAN": "WEEK_PLAN",
                     "LESSON_PLAN": "LESSON_FRAGMENT"}.get(source_kind)
        if item_kind is None:
            raise ValueError("this source kind cannot be structured as a teaching outline")
        if not 1 <= len(items) <= 100 or len(expected_weeks) != len(set(expected_weeks)):
            raise ValueError("outline items or expected weeks are invalid")
        if source_kind == "WEEKLY_PLAN":
            if not expected_weeks or any(not 1 <= week <= 60 for week in expected_weeks):
                raise ValueError("weekly plan needs explicit expected weeks")
        elif expected_weeks:
            raise ValueError("expected weeks belong only to a weekly plan")
        block_by_index = {block["index"]: block for block in source["blocks"]}
        references: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in items:
            item_id, kind = item["item_id"], item["kind"]
            if item_id in seen_ids or kind != item_kind:
                raise ValueError("outline item IDs or kinds are invalid for this source")
            seen_ids.add(item_id)
            week = item.get("week_number")
            if (kind == "WEEK_PLAN" and week is None) or (kind == "OBJECTIVE" and week is not None):
                raise ValueError("outline week number does not match item kind")
            block = block_by_index.get(item["block_index"])
            if block is None:
                raise ValueError("outline citation block does not exist")
            text, quote = block["text"], item["quote"]
            offset = item.get("start_offset")
            if offset is None:
                offset = text.find(quote)
                if offset < 0 or text.find(quote, offset + 1) >= 0:
                    raise ValueError("outline quote is missing or ambiguous in the cited block")
            if offset < 0 or text[offset:offset + len(quote)] != quote:
                raise ValueError("outline quote does not match the cited position")
            references.append({
                "item_id": item_id, "kind": kind, "week_number": week,
                "block_index": block["index"], "start_offset": offset,
                "end_offset": offset + len(quote),
                "block_hash": hashlib.sha256(text.encode()).hexdigest(),
                "quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
            })
        payload = {"items": references, "expected_weeks": sorted(expected_weeks)}
        outline_hash = hashlib.sha256(self.store._json(payload).encode()).hexdigest()
        outline_id = "outline-" + secrets.token_hex(12)
        event = self._audit(identity, "TEACHING_OUTLINE_CREATED",
                            {"course_id": course_id, "source_id": source_id,
                             "outline_id": outline_id, "hash": outline_hash})
        with self.store.teaching_transaction() as connection:
            actor = connection.execute(
                "SELECT active, external_ref_hash FROM actors WHERE actor_id = ? AND tenant_id = ?",
                (identity.actor_id, identity.tenant_id),
            ).fetchone()
            member = connection.execute(
                "SELECT active FROM teaching_course_members WHERE tenant_id = ? AND course_id = ? AND actor_id = ?",
                (identity.tenant_id, course_id, identity.actor_id),
            ).fetchone()
            pinned = connection.execute(
                "SELECT revision_id, content_hash FROM teaching_sources "
                "WHERE tenant_id = ? AND course_id = ? AND source_id = ?",
                (identity.tenant_id, course_id, source_id),
            ).fetchone()
            if (actor is None or not actor["active"] or actor["external_ref_hash"] != identity.subject_ref_hash
                    or member is None or not member["active"]):
                raise PermissionError("course or member authorization was revoked during annotation")
            if (pinned is None or pinned["revision_id"] != source["revision_id"]
                    or pinned["content_hash"] != source["content_hash"]):
                raise SourceChanged("source snapshot changed during annotation")
            connection.execute(
                "INSERT INTO teaching_outlines(outline_id, tenant_id, course_id, source_id, source_revision, "
                "source_hash, outline_hash, data_json, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (outline_id, identity.tenant_id, course_id, source_id, source["revision_id"],
                 source["content_hash"], outline_hash, self.store._json(payload), identity.actor_id),
            )
            self.store.teaching_audit_in_transaction(connection, event)
        return {"outline_id": outline_id, "source_id": source_id, "source_revision": source["revision_id"],
                "outline_hash": outline_hash, "item_count": len(references),
                "coverage": self._week_coverage(references, expected_weeks)}

    def list_outlines(self, identity: IdentityContext, course_id: str, source_id: str) -> list[dict[str, Any]]:
        self._course_member(identity, course_id)
        if self.store.get_teaching_source(identity.tenant_id, course_id, source_id) is None:
            raise KeyError("teaching source is not in this course")
        return [dict(row) for row in self.store.list_teaching_outlines(identity.tenant_id, course_id, source_id)]

    def read_outline(self, identity: IdentityContext, course_id: str, source_id: str,
                     outline_id: str) -> dict[str, Any]:
        self._course_member(identity, course_id)
        row = self.store.get_teaching_outline(identity.tenant_id, course_id, source_id, outline_id)
        if row is None:
            raise KeyError("teaching outline is not in this course and source")
        source = self.read_source(identity, course_id, source_id)
        if (row["source_revision"] != source["revision_id"]
                or row["source_hash"] != source["content_hash"]):
            raise SourceChanged("outline source has changed")
        payload = json.loads(row["data_json"])
        if hashlib.sha256(self.store._json(payload).encode()).hexdigest() != row["outline_hash"]:
            raise SourceChanged("outline integrity check failed")
        blocks = {block["index"]: block["text"] for block in source["blocks"]}
        resolved = []
        for reference in payload["items"]:
            block_text = blocks.get(reference["block_index"])
            if block_text is None or hashlib.sha256(block_text.encode()).hexdigest() != reference["block_hash"]:
                raise SourceChanged("outline citation block changed")
            quote = block_text[reference["start_offset"]:reference["end_offset"]]
            if hashlib.sha256(quote.encode()).hexdigest() != reference["quote_hash"]:
                raise SourceChanged("outline citation quote changed")
            resolved.append({"item_id": reference["item_id"], "kind": reference["kind"],
                             "week_number": reference["week_number"], "quote": quote,
                             "citation": {"source_id": source_id, "revision_id": source["revision_id"],
                                          "block_index": reference["block_index"],
                                          "start_offset": reference["start_offset"],
                                          "end_offset": reference["end_offset"]}})
        self.store.save_audit(self._audit(identity, "TEACHING_OUTLINE_READ",
                                         {"course_id": course_id, "source_id": source_id,
                                          "outline_id": outline_id, "hash": row["outline_hash"]}))
        return {"outline_id": outline_id, "source_id": source_id, "source_revision": source["revision_id"],
                "outline_hash": row["outline_hash"], "created_by": row["created_by"],
                "items": resolved, "coverage": self._week_coverage(payload["items"], payload["expected_weeks"])}
