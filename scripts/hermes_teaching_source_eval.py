"""Run the fictional teaching extraction cases through a configured Hermes model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fde_control_plane.runtime import HermesTeachingSourceAdapter  # noqa: E402
from fde_control_plane.teaching_eval import evaluate_teaching_source_cases  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "teaching_source_cases.json"


def load_cases(group: str) -> tuple[list[dict], str]:
    raw = FIXTURE.read_bytes()
    groups = json.loads(raw)
    cases = groups[group]
    for case in cases:
        blocks = case["source"]["blocks"]
        canonical = json.dumps(blocks, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode()
        case["source"]["content_hash"] = hashlib.sha256(canonical).hexdigest()
    return cases, hashlib.sha256(raw).hexdigest()


def ready() -> bool:
    launcher = os.environ.get("HERMES_PYTHON") or os.environ.get("HERMES_BIN", "hermes")
    return bool(os.environ.get("HERMES_MODEL") and os.environ.get("HERMES_PROVIDER")
                and shutil.which(launcher))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only fictional teaching Agent evaluation")
    parser.add_argument("--group", choices=("development", "holdout"), required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if not ready():
        print("HERMES_EVAL_NOT_CONFIGURED: set HERMES_PROVIDER, HERMES_MODEL and a Hermes launcher",
              file=sys.stderr)
        return 2
    cases, fixture_hash = load_cases(args.group)
    report = evaluate_teaching_source_cases(
        HermesTeachingSourceAdapter(timeout_seconds=args.timeout), cases,
    )
    print(json.dumps({"group": args.group, "fixture_sha256": fixture_hash,
                      "model": os.environ["HERMES_MODEL"],
                      "provider": os.environ["HERMES_PROVIDER"], **report},
                     ensure_ascii=False, sort_keys=True))
    return 0 if report["all_contracts_valid"] and not any(report["totals"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
