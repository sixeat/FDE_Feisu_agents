"""Feishu approval card payloads and verified action dispatch."""

from __future__ import annotations

from typing import Any

from .models import ApprovalRequest, IdentityContext
from .service import ControlPlane


ACTION = "meeting_task_approval"
MEETING_REVIEW_FORM_ACTION = "meeting_review_submit"


def build_meeting_intake_failure_card(*, authorization_required: bool, authorization_url: str | None = None) -> dict[str, Any]:
    """Give a member an actionable failure without exposing document or runtime data."""
    if authorization_required:
        message = "当前账号的文档读取授权缺失或已过期。请重新授权，然后重新发送文档链接。"
    else:
        message = "会议暂未处理成功，未创建任务。请确认当前账号能查看该文档；如需排查，请联系管理员核对运行记录。"
    elements: list[dict[str, Any]] = [{"tag": "div", "text": {"tag": "plain_text", "content": message}}]
    if authorization_required and authorization_url:
        elements.append({"tag": "action", "actions": [{
            "tag": "button", "text": {"tag": "plain_text", "content": "重新授权"},
            "type": "primary", "url": authorization_url,
        }]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "会议助手需要你处理"}},
        "elements": elements,
    }


def build_meeting_intake_card(
    *, title: str, todos: list[dict[str, Any]], review_url: str,
) -> dict[str, Any]:
    """Show Agent draft output without offering a premature write approval."""
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": f"{title}：提取 {len(todos)} 条待办，请核对后提交。"}},
    ]
    for index, todo in enumerate(todos[:8], 1):
        label = str(todo.get("title") or "未命名待办")
        due = str(todo.get("due_date") or "待确认")
        assignee = "已匹配" if todo.get("assignee_actor_id") else "待确认"
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": f"{index}. {label}｜负责人：{assignee}｜截止：{due}"}})
    if len(todos) > 8:
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": f"另有 {len(todos) - 8} 条，请在审核页查看。"}})
    elements.append({"tag": "action", "actions": [{
        "tag": "button", "text": {"tag": "plain_text", "content": "审核待办"},
        "type": "primary", "url": review_url,
    }]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "会议 Agent 已完成提取"}},
        "elements": elements,
    }


def build_meeting_review_form_card(
    *, title: str, todos: list[dict[str, Any]], members: list[dict[str, str]], review_url: str,
) -> dict[str, Any]:
    """Bounded Card 2.0 form for simple meeting drafts; caller stores its snapshot."""
    if not 1 <= len(todos) <= 3 or not members:
        raise ValueError("card review requires one to three todos and a member directory")
    options = [{
        "text": {"tag": "plain_text", "content": member["name"]},
        "value": member["actor_id"],
    } for member in members]
    fields: list[dict[str, Any]] = []
    for index, todo in enumerate(todos):
        fields.extend([
            {"tag": "div", "text": {"tag": "plain_text", "content": f"{index + 1}. {todo['title']}"}},
            {"tag": "div", "text": {"tag": "plain_text", "content": f"来源：{todo['evidence']}"}},
            {"tag": "div", "text": {"tag": "plain_text", "content": "选择责任人"}},
            {"tag": "select_static", "name": f"assignee_{index}",
             "placeholder": {"tag": "plain_text", "content": "选择企业成员"},
             "options": options, "required": True},
            {"tag": "div", "text": {"tag": "plain_text", "content": "选择截止日期"}},
            {"tag": "date_picker", "name": f"due_{index}",
             "placeholder": {"tag": "plain_text", "content": "选择日期"}, "required": True},
        ])
        if todo.get("assignee_actor_id"):
            fields[-3]["initial_option"] = todo["assignee_actor_id"]
        if todo.get("due_date"):
            fields[-1]["initial_date"] = todo["due_date"]
    fields.append({
        "tag": "button", "name": MEETING_REVIEW_FORM_ACTION,
        "text": {"tag": "plain_text", "content": "提交会议审核"},
        "type": "primary_filled", "form_action_type": "submit",
    })
    return {
        "schema": "2.0",
        "config": {"update_multi": False, "enable_forward": False},
        "header": {"title": {"tag": "plain_text", "content": f"会议待办审核 · {title}"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": "请核对标题和来源证据，补齐责任人与日期。提交后只生成待审批提案，不会立即创建飞书任务。"}},
            {"tag": "form", "name": "meeting_review", "elements": fields},
            {"tag": "button", "text": {"tag": "plain_text", "content": "需要修改标题或处理冲突？打开审核页"},
             "type": "default", "url": review_url},
        ]},
    }


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


__all__ = ["MEETING_REVIEW_FORM_ACTION", "build_meeting_approval_card", "build_meeting_decided_card", "build_meeting_intake_card", "build_meeting_intake_failure_card", "build_meeting_review_form_card", "handle_meeting_approval_action"]
