"""Runtime adapters for Agent semantic execution.

The first adapter is deliberately a short-task Hermes bridge. It sends only a
sanitized meeting input to Hermes and expects a ``meeting-agent.v1`` JSON
contract back. Permissions, approvals, Feishu credentials and side effects
remain outside this module in the control plane.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


class RuntimeExecutionError(RuntimeError):
    """A Runtime invocation failed before producing a trusted contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RunRequest:
    task_run_id: str
    trace_id: str
    actor_ref: str
    agent_version: str
    input_ref: Mapping[str, Any]
    tool_policy_snapshot: Mapping[str, Any]
    deadline: str
    approval_context: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RunEvent:
    task_run_id: str
    trace_id: str
    sequence: int
    event_type: str
    occurred_at: str
    summary: str


@dataclass(frozen=True)
class RuntimeResult:
    status: str
    payload: dict[str, Any] | None
    external_run_ref: str | None
    events: tuple[RunEvent, ...] = ()
    error_code: str | None = None
    error_detail: str | None = None


class RuntimeAdapter(Protocol):
    def run_once(self, request: RunRequest) -> RuntimeResult: ...

    def health(self) -> Mapping[str, Any]: ...


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(request: RunRequest, sequence: int, event_type: str, summary: str) -> RunEvent:
    return RunEvent(
        task_run_id=request.task_run_id,
        trace_id=request.trace_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=_now(),
        summary=summary,
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Extract one JSON object from Hermes' final response.

    Hermes may wrap a response in Markdown fences even when prompted for JSON.
    The response remains untrusted: the control-plane contract gate validates
    source, evidence, permissions and confirmation state after this parser.
    """

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeExecutionError("INVALID_JSON", "Hermes response did not contain a JSON object")


def _meeting_prompt(request: RunRequest) -> str:
    input_ref = dict(request.input_ref)
    blocks = input_ref.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise RuntimeExecutionError("INPUT_BLOCKS_REQUIRED", "meeting input must include non-empty blocks")
    source = {
        "document_id_hash": input_ref.get("document_id_hash"),
        "revision_id": input_ref.get("revision_id"),
        "blocks": blocks,
    }
    schema = {
        "schema_version": "meeting-agent.v1",
        "source": {
            "document_id_hash": "string",
            "revision_id": "string",
            "source_block_ids": ["integer"],
        },
        "summary": {"text": "string", "evidence_block_ids": ["integer"]},
        "decisions": [],
        "todos": [
            {
                "todo_id": "string",
                "title": "string",
                "assignee_candidate": {"display_name": "string|null", "status": "candidate|missing"},
                "due_date_candidate": {
                    "raw_text": "string|null",
                    "normalized_date": "string|null",
                    "status": "normalized|needs_context|missing",
                },
                "evidence_block_ids": ["integer"],
                "needs_confirmation": "boolean",
                "confirmation_reasons": ["string"],
            }
        ],
        "relations": [],
        "agent_notes": "string",
    }
    return (
        "You are the Meeting Agent in an enterprise control plane.\n"
        "Analyze the supplied meeting blocks and return exactly one JSON object.\n"
        "Do not use tools, do not invent names or dates, and do not authorize any write.\n"
        "Every summary, decision and todo must cite source block indexes. Use null and "
        "needs_confirmation=true when a field is ambiguous. Mark possible duplicate or "
        "conflict relations for human review. Do not silently omit an action item: "
        "sentences containing responsibility, follow-up, deadlines or assigned work "
        "must become todo candidates, even when their assignee or date needs confirmation.\n"
        "For every relation, `todo_ids` must exactly match the emitted todo_id strings, "
        "and `evidence_block_ids` must be integer indexes from the supplied blocks. "
        "Never use display names, array positions, or invented block numbers in relations.\n\n"
        f"Required output shape:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"Meeting input:\n{json.dumps(source, ensure_ascii=False, indent=2)}"
    )


class HermesRuntimeAdapter:
    """Invoke Hermes for one bounded, no-tool meeting-agent run."""

    def __init__(
        self,
        *,
        executable: str | Path | None = None,
        python_executable: str | Path | None = None,
        model: str | None = None,
        provider: str | None = None,
        timeout_seconds: float = 120.0,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.executable = str(executable or os.environ.get("HERMES_BIN", "hermes"))
        self.python_executable = str(python_executable or os.environ.get("HERMES_PYTHON") or "")
        self.model = model or os.environ.get("HERMES_MODEL")
        self.provider = provider or os.environ.get("HERMES_PROVIDER")
        self.timeout_seconds = timeout_seconds
        self._runner = runner or subprocess.run

    def health(self) -> Mapping[str, Any]:
        return {
            "runtime": "hermes",
            "executable": self.executable,
            "mode": "short_task_json",
            "tools_enabled": False,
            "model_configured": bool(self.model or self.provider),
        }

    def _command(self) -> list[str]:
        launcher = (
            [self.python_executable, "-c", "from hermes_cli.main import main; main()"]
            if self.python_executable else [self.executable]
        )
        command = launcher + [
            "chat",
            "--query-file",
            "-",
            "--oneshot",
            "--quiet",
            "--format",
            "text",
            "--safe-mode",
            "--ignore-user-config",
            "--ignore-rules",
            "--source",
            "fde-control-plane",
        ]
        if self.model:
            command.extend(["--model", self.model])
        if self.provider:
            command.extend(["--provider", self.provider])
        return command

    def run_once(self, request: RunRequest) -> RuntimeResult:
        prompt = _meeting_prompt(request)
        events = [_event(request, 1, "accepted", "Hermes run accepted")]
        try:
            completed = self._runner(
                self._command(),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            events.append(_event(request, 2, "unknown", "Hermes execution timed out"))
            return RuntimeResult(
                status="UNKNOWN",
                payload=None,
                external_run_ref=None,
                events=tuple(events),
                error_code="RUNTIME_TIMEOUT",
                error_detail=str(exc),
            )
        except OSError as exc:
            events.append(_event(request, 2, "failed", "Hermes process could not start"))
            return RuntimeResult(
                status="FAILED",
                payload=None,
                external_run_ref=None,
                events=tuple(events),
                error_code="RUNTIME_START_FAILED",
                error_detail=str(exc),
            )

        if completed.returncode != 0:
            events.append(_event(request, 2, "failed", "Hermes returned a non-zero exit code"))
            detail = (completed.stderr or completed.stdout or "").strip()[:500]
            return RuntimeResult(
                status="FAILED",
                payload=None,
                external_run_ref=None,
                events=tuple(events),
                error_code="RUNTIME_EXIT_NONZERO",
                error_detail=detail,
            )
        try:
            payload = _extract_json(completed.stdout)
        except RuntimeExecutionError as exc:
            events.append(_event(request, 2, "failed", "Hermes returned invalid JSON"))
            return RuntimeResult(
                status="FAILED",
                payload=None,
                external_run_ref=None,
                events=tuple(events),
                error_code=exc.code,
                error_detail=exc.detail,
            )
        events.extend([
            _event(request, 2, "completed", "Hermes returned a meeting-agent contract"),
        ])
        return RuntimeResult(
            status="SUCCEEDED",
            payload=payload,
            external_run_ref=None,
            events=tuple(events),
        )


__all__ = [
    "HermesRuntimeAdapter",
    "RunEvent",
    "RunRequest",
    "RuntimeAdapter",
    "RuntimeExecutionError",
    "RuntimeResult",
]
