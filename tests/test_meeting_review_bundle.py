import copy
import sqlite3

import pytest

from fde_control_plane import Actor, ActorType, ControlPlane, SQLiteStore
from scripts.meeting_review_bundle import import_bundle
from test_feishu_review import setup_review, DOCUMENT_ID, SUBJECT


def bundle_setup(tmp_path):
    source, _, _, _, task_id, _ = setup_review(tmp_path)
    bundle = {"schema_version": "meeting-transfer.v1", "source_member_hash": SUBJECT,
              "record": source.store.get_meeting_record("record"),
              "task": source.store.get_task_run(task_id),
              "drafts": source.store.list_meeting_todos("record"),
              "task_steps": source.store.list_task_steps(task_id),
              "delegations": source.store.list_delegations(task_id),
              "runtime_events": source.store.list_runtime_events(task_id),
              "source": {"document_id": DOCUMENT_ID, "revision_id": "5", "title": "会议纪要",
                         "evidence": [{"block_id": 2, "text": "张三整理部署文档"}]}}
    target = ControlPlane(SQLiteStore(tmp_path / "target.sqlite3"))
    target.register_actor(Actor("member-primary", "demo", ActorType.USER, external_ref_hash=SUBJECT))
    return target, bundle


def do_import(cp, bundle):
    return import_bundle(cp, bundle, tenant_id="demo", member_id="member-primary", member_hash=SUBJECT)


def test_import_remaps_verified_submitter_preserves_evidence_and_is_idempotent(tmp_path):
    cp, bundle = bundle_setup(tmp_path)
    assert do_import(cp, bundle)
    assert cp.store.get_meeting_record("record")["submitted_by"] == "member-primary"
    assert cp.store.get_meeting_record("record")["payload_hash"] == bundle["record"]["payload_hash"]
    task_id = bundle["task"]["task_run_id"]
    assert cp._task(task_id).tenant_id == "demo"
    assert cp.store.get_meeting_source("record") == bundle["source"]
    assert cp.store.list_tool_calls(task_id) == []
    assert not do_import(cp, bundle)
    assert len(cp.store.list_meeting_todos("record")) == 1


def test_mismatched_identity_and_missing_evidence_fail_before_any_import(tmp_path):
    cp, bundle = bundle_setup(tmp_path)
    invalid = {**bundle, "source_member_hash": "other-member"}
    with pytest.raises(PermissionError):
        do_import(cp, invalid)
    invalid = copy.deepcopy(bundle)
    invalid["source"]["evidence"] = []
    with pytest.raises(ValueError):
        do_import(cp, invalid)
    assert cp.store.get_meeting_record("record") is None
    assert cp._actor("member-primary").capabilities == frozenset()


def test_failed_import_rolls_back_member_grants_and_graph(tmp_path):
    cp, bundle = bundle_setup(tmp_path)
    bundle["task_steps"].append(bundle["task_steps"][0])
    with pytest.raises(sqlite3.IntegrityError):
        do_import(cp, bundle)
    assert cp.store.get_meeting_record("record") is None
    assert cp.store.get_task_run(bundle["task"]["task_run_id"]) is None
    assert cp._actor("member-primary").capabilities == frozenset()
