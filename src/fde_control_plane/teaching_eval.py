"""Offline, read-only teaching source extraction evaluation."""

from __future__ import annotations

from typing import Any, Iterable

from .runtime import RunRequest, RuntimeAdapter
from .teaching_agent import TeachingContractError, validate_teaching_source_output


_CITATION = ("kind", "block_index", "start_offset", "end_offset", "quote")


def evaluate_teaching_source_cases(adapter: RuntimeAdapter,
                                   cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for case in cases:
        source = case["source"]
        expected = case["expected_items"]
        # Validate fixture source and expected citations before invoking a model.
        expected_payload = {"schema_version": "teaching-source-agent.v1",
                            "source": {key: source[key] for key in
                                       ("source_id", "revision_id", "content_hash", "kind")},
                            "items": [{"item_id": f"expected-{index}", **item}
                                      for index, item in enumerate(expected)]}
        validate_teaching_source_output(source, expected_payload)
        request = RunRequest(
            task_run_id=f"eval-{case['case_id']}", trace_id=f"eval-{case['case_id']}",
            actor_ref="offline-fixture", agent_version="teaching-source-agent.v1",
            input_ref=source, tool_policy_snapshot={"allowed_tools": []}, deadline="offline",
        )
        run = adapter.run_once(request)
        row = {"case_id": case["case_id"], "runtime_status": run.status,
               "contract_valid": False, "matched": 0, "omitted": len(expected),
               "false_positive": 0, "week_mismatch": 0}
        if run.status == "SUCCEEDED" and run.payload is not None:
            try:
                actual = validate_teaching_source_output(source, run.payload)["items"]
            except TeachingContractError:
                row["error_code"] = "TEACHING_CONTRACT_INVALID"
            else:
                row["contract_valid"] = True
                unmatched = list(expected)
                for item in actual:
                    citation = tuple(item[key] for key in _CITATION)
                    position = next((index for index, wanted in enumerate(unmatched)
                                     if tuple(wanted[key] for key in _CITATION) == citation), None)
                    if position is None:
                        row["false_positive"] += 1
                    else:
                        wanted = unmatched.pop(position)
                        if item["week_number"] == wanted["week_number"]:
                            row["matched"] += 1
                        else:
                            row["week_mismatch"] += 1
                row["omitted"] = len(unmatched)
        else:
            row["error_code"] = run.error_code or "RUNTIME_NO_PAYLOAD"
        results.append(row)
    return {"cases": results,
            "totals": {key: sum(row[key] for row in results)
                       for key in ("matched", "omitted", "false_positive", "week_mismatch")},
            "all_contracts_valid": all(row["contract_valid"] for row in results)}


__all__ = ["evaluate_teaching_source_cases"]
