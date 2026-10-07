# 会议 Agent 输出契约（草案）

> 用途：定义会议 Agent 与自研控制平面之间的稳定输入/输出边界。当前只用于设计和离线验证，不代表主产品接口已经实现。

## 1. 基本原则

- Agent 只提出结构化候选，不直接调用飞书写入接口。
- 每个结论必须关联输入文档的 `document_id`、`revision_id` 和来源块编号。
- Agent 无法判断时使用 `null` 和 `needs_confirmation=true`，不能猜测。
- 可能重复、字段冲突、责任人不明确和截止日期不明确的事项不能直接进入写入队列。
- 控制平面负责权限、审批、版本、幂等和最终执行；Agent 的置信度不能替代这些检查。

## 2. 输出结构

```json
{
  "schema_version": "meeting-agent.v1",
  "source": {
    "document_id_hash": "短哈希",
    "revision_id": "6",
    "source_block_ids": [8, 9, 10, 12]
  },
  "summary": {
    "text": "会议摘要候选",
    "evidence_block_ids": [2, 4]
  },
  "decisions": [
    {
      "text": "决定事项候选",
      "evidence_block_ids": [4]
    }
  ],
  "todos": [
    {
      "todo_id": "todo-1",
      "title": "整理部署文档",
      "assignee_candidate": {
        "display_name": "张三",
        "feishu_open_id": null,
        "status": "candidate"
      },
      "due_date_candidate": {
        "raw_text": "下周三",
        "normalized_date": null,
        "status": "needs_context"
      },
      "evidence_block_ids": [8],
      "needs_confirmation": true,
      "confirmation_reasons": ["relative_date_not_normalized"]
    }
  ],
  "relations": [
    {
      "todo_ids": ["todo-2", "todo-4"],
      "relation": "possible_duplicate_or_conflict",
      "reason": "两条内容都涉及接口联调，但截止日期信息不同",
      "evidence_block_ids": [9, 12],
      "field_conflicts": ["due_date"],
      "needs_human_confirmation": true
    }
  ],
  "agent_notes": "自然语言说明，不具有授权效力"
}
```

## 3. 控制平面校验

收到结果后按顺序检查：

1. `schema_version` 是否受支持；
2. 所有 `evidence_block_ids` 是否属于当前 `document_id + revision_id`；
3. 待办是否有标题和来源；
4. 责任人是否已经从候选身份映射为租户内成员；
5. 相对日期是否已经由人工或明确会议日期转换；
6. `relations` 是否包含重复或冲突候选；
7. 任何关键字段修改后是否生成新的结果版本；
8. 只有审批通过、权限交集仍有效且幂等键生成后，才能进入飞书任务 outbox。

## 4. 人工确认状态

| 状态 | 含义 | 是否允许写任务 |
|---|---|---:|
| `ready_for_review` | Agent 已生成候选，证据完整 | 否 |
| `needs_confirmation` | 缺责任人、日期、可见范围，或存在重复/冲突 | 否 |
| `approved` | 有资格的人员确认了具体版本 | 仍需执行前复核 |
| `invalidated` | 文档版本、权限或关键字段发生变化 | 否 |
| `executing` | 已通过复核，进入受控写入 | 仅由工具网关执行 |

## 5. 本次测试文档的预期语义

对于当前虚构文档，Agent 应识别：

- 第 1 条有责任人，但“下周三”需要会议日期上下文；
- 第 2 条责任人和日期都待确认；
- 第 3 条有责任人，截止日期待确认；
- 第 2 条与第 4 条可能是同一“接口联调”事项，日期信息需要人工确认；
- 任何一条都不能绕过审批直接创建飞书任务。

这个契约将来可以由 Hermes、普通 LLM 或其他 Agent 实现；运行时变化不应改变控制平面的安全校验。
