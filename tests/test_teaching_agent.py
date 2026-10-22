import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fde_control_plane.runtime import (HermesTeachingSourceAdapter, RunRequest,
                                       RuntimeExecutionError, RuntimeResult)
from fde_control_plane.teaching_agent import TeachingContractError, validate_teaching_source_output
from fde_control_plane.teaching_eval import evaluate_teaching_source_cases


def cases():
    fixture = Path(__file__).parent / "fixtures" / "teaching_source_cases.json"
    groups = json.loads(fixture.read_text(encoding="utf-8"))
    for case in groups["development"] + groups["holdout"]:
        blocks = case["source"]["blocks"]
        encoded = json.dumps(blocks, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        case["source"]["content_hash"] = hashlib.sha256(encoded).hexdigest()
    return groups


def output_for(case):
    source = case["source"]
    return {"schema_version": "teaching-source-agent.v1",
            "source": {key: source[key] for key in
                       ("source_id", "revision_id", "content_hash", "kind")},
            "items": [{"item_id": f"item-{index}", **item}
                      for index, item in enumerate(case["expected_items"])]}


def test_fixture_citations_are_exact_and_output_needs_review():
    for group in cases().values():
        for case in group:
            result = validate_teaching_source_output(case["source"], output_for(case))
            assert len(result["items"]) == len(case["expected_items"])


@pytest.mark.parametrize("mutation", [
    lambda p: p["source"].update(revision_id="stale"),
    lambda p: p["source"].update(content_hash="0" * 64),
    lambda p: p["items"][0].update(quote="invented"),
    lambda p: p["items"][0].update(start_offset=1),
    lambda p: p["items"][0].update(kind="WEEK_PLAN"),
    lambda p: p["items"][0].update(week_number=True),
    lambda p: p["items"][0].update(write_task=True),
    lambda p: p["items"][1].update(item_id=p["items"][0]["item_id"]),
])
def test_contract_rejects_untrusted_candidates(mutation):
    case = cases()["development"][0]
    payload = output_for(case)
    mutation(payload)
    with pytest.raises(TeachingContractError):
        validate_teaching_source_output(case["source"], payload)


def test_contract_rejects_changed_input_blocks():
    case = cases()["development"][0]
    case["source"]["blocks"][0]["text"] = "changed"
    with pytest.raises(TeachingContractError):
        validate_teaching_source_output(case["source"], output_for(case))


def test_teaching_adapter_uses_no_tool_hermes_and_separate_prompt(monkeypatch):
    case = cases()["development"][0]
    seen = {}
    monkeypatch.setenv("FEISHU_APP_SECRET", "excluded")

    def runner(command, **kwargs):
        seen.update(command=command, prompt=kwargs["input"], env=kwargs["env"])
        return subprocess.CompletedProcess(command, 0, json.dumps(output_for(case)), "")

    request = RunRequest("eval-1", "trace-1", "fixture", "teaching-source-agent.v1",
                         case["source"], {"allowed_tools": []}, "offline")
    result = HermesTeachingSourceAdapter(runner=runner).run_once(request)
    assert result.status == "SUCCEEDED"
    assert result.events[-1].summary == "Hermes returned a teaching-source-agent contract"
    assert "teaching-source-agent.v1" in seen["prompt"]
    assert "Do not use tools" in seen["prompt"]
    assert "--safe-mode" in seen["command"]
    assert seen["command"][seen["command"].index("--toolsets") + 1] == "context_engine"
    assert "FEISHU_APP_SECRET" not in seen["env"]
    validate_teaching_source_output(case["source"], result.payload)


def test_teaching_adapter_rejects_write_policy_before_model_call():
    case = cases()["development"][0]

    def runner(command, **kwargs):
        pytest.fail("invalid teaching request reached Hermes")

    request = RunRequest("eval-1", "trace-1", "fixture", "teaching-source-agent.v1",
                         case["source"], {"allowed_tools": ["task.create"]}, "offline")
    with pytest.raises(RuntimeExecutionError, match="INVALID_TEACHING_INPUT"):
        HermesTeachingSourceAdapter(runner=runner).run_once(request)


def test_offline_eval_separates_omissions_false_positives_and_week_errors():
    case = cases()["holdout"][0]
    payload = output_for(case)
    payload["items"].pop(1)
    payload["items"][0]["week_number"] = 2
    payload["items"].append({"item_id": "invented-week", "kind": "WEEK_PLAN",
                             "week_number": 2, "block_index": 3, "start_offset": 0,
                             "end_offset": len(case["source"]["blocks"][2]["text"]),
                             "quote": case["source"]["blocks"][2]["text"]})

    class FakeAdapter:
        def run_once(self, request):
            assert request.tool_policy_snapshot == {"allowed_tools": []}
            return RuntimeResult("SUCCEEDED", payload, None)

    report = evaluate_teaching_source_cases(FakeAdapter(), [case])
    assert report["all_contracts_valid"] is True
    assert report["totals"] == {"matched": 0, "omitted": 1, "false_positive": 1,
                                "week_mismatch": 1}


def test_offline_eval_does_not_count_bad_contract_as_quality_pass():
    case = cases()["development"][0]
    payload = output_for(case)
    payload["items"][0]["quote"] = "invented"

    class FakeAdapter:
        def run_once(self, request):
            return RuntimeResult("SUCCEEDED", payload, None)

    report = evaluate_teaching_source_cases(FakeAdapter(), [case])
    assert report["all_contracts_valid"] is False
    assert report["cases"][0]["error_code"] == "TEACHING_CONTRACT_INVALID"
    assert report["cases"][0]["omitted"] == 2


def test_hermes_teaching_eval_refuses_unconfigured_model():
    env = {key: value for key, value in os.environ.items()
           if key not in {"HERMES_MODEL", "HERMES_PROVIDER", "HERMES_BIN", "HERMES_PYTHON"}}
    script = Path(__file__).parents[1] / "scripts" / "hermes_teaching_source_eval.py"
    completed = subprocess.run([sys.executable, str(script), "--group", "development"],
                               capture_output=True, text=True, env=env, check=False)
    assert completed.returncode == 2
    assert "HERMES_EVAL_NOT_CONFIGURED" in completed.stderr
    assert completed.stdout == ""


def test_teaching_eval_cli_fixture_groups_are_distinct():
    from scripts.hermes_teaching_source_eval import load_cases

    development, dev_hash = load_cases("development")
    holdout, holdout_hash = load_cases("holdout")
    assert dev_hash == holdout_hash
    assert {case["case_id"] for case in development}.isdisjoint(
        {case["case_id"] for case in holdout})
    for case in development + holdout:
        validate_teaching_source_output(case["source"], output_for(case))
