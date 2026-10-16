"""Send one isolated Card 2.0 form to the already bound test member.

This uses no meeting record, proposal, approval, or task API. The listener's
FDE_CARD_FORM_PROBE_ENABLED branch only reports callback shape and a toast.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import uuid
from pathlib import Path

import lark_oapi as lark

from fde_control_plane.approval_card import build_meeting_review_form_card
from fde_control_plane.feishu_approval import FeishuCardSender


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient-file", type=Path, required=True)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()
    card = build_meeting_review_form_card(
        title="Card 2.0 表单测试",
        todos=[{"title": "核对表单字段", "evidence": "测试数据，不关联任何会议文档",
                "assignee_actor_id": "probe-member", "due_date": "2026-10-20"}],
        members=[{"actor_id": "probe-member", "name": "测试成员"}],
        review_url="https://fde.sixeat.icu/h5#meetings",
    )
    card["body"]["elements"][0]["text"]["content"] = "仅测试卡片表单；提交不会生成审批或飞书任务。"
    card["body"]["elements"][1]["elements"][-1]["name"] = "fde_card_form_probe"
    if not args.send:
        print("CARD_FORM_PROBE_MODE=DRY_RUN card_schema=2.0 fields=2 task_write=0")
        return 0
    app_id, secret = os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not secret:
        raise RuntimeError("Feishu credentials are missing")
    open_id = args.recipient_file.read_text(encoding="utf-8").strip()
    if not open_id:
        raise ValueError("recipient file is empty")
    client = lark.Client.builder().app_id(app_id).app_secret(secret).timeout(12).build()
    key = str(uuid.uuid4())
    message_id = FeishuCardSender(client).send(open_id, card, key)
    print("CARD_FORM_PROBE_SENT message_hash="
          + hashlib.sha256(message_id.encode()).hexdigest()[:12] + " task_write=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
