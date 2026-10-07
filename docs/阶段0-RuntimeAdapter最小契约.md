# 阶段 0：RuntimeAdapter 最小契约

> 用途：把 Hermes/OpenClaw 等候选 Runtime 放在可替换边界后面。本文是设计契约，不是主产品实现，也不把 A2A 当作安全边界。

## 1. 边界原则

```text
控制平面拥有：Actor、权限交集、ToolPolicy、Approval、TaskRun、审计、恢复和对账
RuntimeAdapter 拥有：启动短任务、传递无凭证输入、接收结构化结果、报告运行状态
Runtime 不拥有：飞书 token、最终授权、审批结论、任务持久化、成员私有范围判定
```

Runtime 只能看到经过控制平面筛选的输入和工具描述。飞书工具由控制平面网关提供，不能把原始应用凭证注入模型上下文或任意 Runtime 环境。

## 2. 输入与输出

### `RunRequest`

| 字段 | 必填 | 说明 |
|---|---:|---|
| `task_run_id` | 是 | 控制平面生成的稳定 ID，重试和对账使用 |
| `trace_id` | 是 | 一次委托链路的追踪 ID |
| `actor_ref` | 是 | 触发用户/服务身份的内部引用，不直接暴露令牌 |
| `agent_version` | 是 | 不可变 Agent 版本 |
| `input_ref` | 是 | 输入内容或受控引用；需经过可见范围检查 |
| `tool_policy_snapshot` | 是 | 本次运行允许的工具、资源范围和风险级别快照 |
| `approval_context` | 否 | 已有审批的引用；不能由 Runtime 自行生成有效审批 |
| `deadline` | 是 | 控制平面定义的执行期限 |

### `RunEvent`

Adapter 至少产生以下事件：

```text
accepted       → Runtime 已接收
working        → 正在执行
tool_proposed  → 请求工具，但尚未代表已授权
waiting_approval → 等待控制平面审批
tool_started   → 网关已允许并开始工具调用
tool_finished  → 工具返回，附脱敏结果摘要和远端 ID（如有）
completed      → 返回结构化产物
failed         → 明确错误类别
unknown        → 结果可能已产生但无法确认，交给对账流程
cancelled      → 由控制平面取消
```

事件必须包含 `task_run_id`、`trace_id`、单调递增序号、时间和来源。重复事件由控制平面去重，乱序事件不能覆盖已经确认的终态。

## 3. 生命周期操作

Adapter 的最小接口语义为：

```text
start(request) -> external_run_ref
poll(external_run_ref) -> RunEvent[]
cancel(external_run_ref) -> acknowledgement
health() -> capability_report
```

`resume` 不要求 Runtime 自己提供。审批等待、进程重启和网络中断后，控制平面从 durable `TaskRun` 恢复；必要时重新启动一个无副作用短任务或进入人工对账。Runtime 的进程内任务表不能作为恢复依据。

## 4. 安全不变量

1. 没有有效的权限交集，Adapter 不得启动工具调用。
2. `WAITING_APPROVAL` 不得产生飞书写入。
3. 审批绑定的动作内容、资源范围或 Agent 版本变化后，旧审批失效。
4. Runtime 返回的文本、消息和产物都按不可信输入处理，不能改变 `ToolPolicy`。
5. 远端结果不确定时标记 `UNKNOWN`，不得盲目重试可能已经成功的写操作。
6. Adapter 日志记录引用和摘要，不记录 access token、refresh token 或完整私有正文。

## 5. 当前证据映射

| 契约部分 | 当前证据 | 结论 |
|---|---|---|
| 外部任务查询和状态 | Hermes A2A wait probe | `WORKING → COMPLETED` 可观察 |
| 自定义工具调用 | Hermes MCP probe | 结构化工具结果可取得 |
| 写工具门控 | Hermes gated probe | 拒绝时底层工具未执行 |
| 重启后恢复 | Hermes restart probe | Runtime TaskStore 不可恢复，控制平面必须持久化 |
| profile 文件边界 | profile tripwire `4 passed` + alpha/beta probe | 有文件级隔离证据，仍不足以证明企业权限隔离 |
| 完整控制平面 Adapter | 尚无主项目实现 | 后续阶段 1 实现并验收 |

