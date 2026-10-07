"""Feishu approval card payloads and verified action dispatch."""

from __future__ import annotations

from typing import Any

from .models import ApprovalRequest, IdentityContext
from .service import ControlPlane


ACTION = "meeting_task_approval"


def build_meeting_approval_card(
    approval: ApprovalRequest,
    *,
    assignee_name: str,
    approve_label: str = "批准创建任务",
) -> dict[str, Any]:
    """Render an approval-bound proposal with exact action version and hash."""
    arguments = approval.proposal.arguments
    title = str(arguments.get("title") or "会议待办")
    due_date = str(arguments.get("due_date") or "待确认")
    origin = arguments.get("origin") or {}
    evidence = ", ".join(str(item) for item in origin.get("evidence_block_ids") or [])
    common = {
        "action": ACTION,
        "approval_id": approval.approval_id,
        "action_version": approval.proposal.action_version,
        "proposal_hash": approval.proposal.proposal_hash,
    }
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "会议待办审批"}},
        "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": title}},
            {"tag": "div", "text": {"tag": "plain_text", "content": f"责任人：{assignee_name}  截止：{due_date}"}},
            {"tag": "div", "text": {"tag": "plain_text", "content": f"来源块：{evidence}"}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": approve_label},
                        "type": "primary",
                        "value": {**common, "decision": "approve"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "default",
                        "value": {**common, "decision": "reject"},
                    },
                ],
            },
        ],
    }


def build_meeting_decided_card(
    approval: ApprovalRequest,
    *,
    assignee_name: str,
    test_mode: bool = False,
) -> dict[str, Any]:
    """Replace actionable buttons with the persisted decision, not a success claim."""
    if approval.status.value == "APPROVED":
        label = "已确认测试，未创建任务" if test_mode else "已批准，等待任务执行"
        template = "green"
    elif approval.status.value == "REJECTED":
        label = "已拒绝"
        template = "grey"
    else:
        raise ValueError("approval has no final decision")
    card = build_meeting_approval_card(approval, assignee_name=assignee_name)
    card["header"]["title"]["content"] = "会议待办审批 · " + label
    card["header"]["template"] = template
    card["elements"][-1] = {"tag": "div", "text": {"tag": "plain_text", "content": label}}
    return card


def handle_meeting_approval_action(
    control_plane: ControlPlane,
    *,
    event_id: str,
    verified_operator: IdentityContext,
    value: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch an authenticated card event through the approval fence.

    The Feishu gateway must validate the callback and map operator.open_id to
    ``verified_operator`` before calling this function.
    """
    if not event_id or value.get("action") != ACTION:
        raise ValueError("unsupported approval card action")
    if value.get("decision") not in {"approve", "reject"}:
        raise ValueError("invalid approval decision")
    approval_id = value.get("approval_id")
    proposal_hash = value.get("proposal_hash")
    action_version = value.get("action_version")
    if not isinstance(approval_id, str) or not approval_id:
        raise ValueError("approval id is required")
    if not isinstance(proposal_hash, str) or not proposal_hash:
        raise ValueError("proposal hash is required")
    if not isinstance(action_version, int) or isinstance(action_version, bool):
        raise ValueError("action version must be an integer")
    actor = control_plane.verify_identity(verified_operator)
    return control_plane.handle_approval_callback(
        event_id,
        approval_id,
        actor.actor_id,
        value["decision"] == "approve",
        action_version,
        proposal_hash,
    )


__all__ = ["build_meeting_approval_card", "build_meeting_decided_card", "handle_meeting_approval_action"]
