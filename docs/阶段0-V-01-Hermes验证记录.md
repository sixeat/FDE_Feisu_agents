# 阶段 0 / V-01：Hermes Runtime 验证记录

> 验证日期：2026-09-25（Asia/Shanghai）  
> 当前结论：**部分通过，仍需完成受控工具和外部任务恢复实验**

## 1. 验证范围

本记录只验证 Hermes 是否适合作为候选 Runtime，以及它是否提供足够的外部控制边界。没有接入真实飞书租户，没有把飞书凭证放入 Hermes，也没有修改主项目代码。

## 2. 固定版本与环境

| 项目 | 记录 |
|---|---|
| 上游仓库 | `NousResearch/hermes-agent` |
| 固定版本 | `v2026.9.24` |
| Hermes 版本 | `0.21.5` |
| 许可证 | MIT（以该版本仓库文件为准，实施前仍需锁定 commit 复核） |
| 源码目录 | `D:\Temp\hermes-src\NousResearch-hermes-agent-e3dd27e` |
| 隔离环境 | `D:\Temp\hermes-venv` |
| Python | `3.12.10` |
| Node.js | `24.18.1` |
| Docker/Podman | 未安装；本项验证不依赖容器 |

安装方式为在临时虚拟环境中执行：

```powershell
python -m pip install -e D:\Temp\hermes-src\NousResearch-hermes-agent-e3dd27e
```

## 3. 已执行命令与证据

### 3.1 CLI、服务和 MCP 入口

```text
hermes --version
Hermes Agent v0.21.5 (2026.9.24)
Python: 3.12.10
OpenAI SDK: 2.24.0
```

`hermes serve --help` 显示 Hermes 提供无浏览器界面的 JSON-RPC/WebSocket 后端服务，默认监听 `127.0.0.1:9119`，支持 `--isolated` profile 服务；`hermes mcp serve --help` 显示可以启动 MCP server。

静态源码和帮助信息还确认存在：

- A2A v1.0 plugin，包含 Agent Card、JSON-RPC、流式消息、任务查询/取消/订阅、peer token、trusted peers、限流、反循环和审计日志；
- MCP server/client 能力；
- profile、session、memory、skills、tools 等运行时对象；
- Feishu/Lark 平台插件入口，但当前没有配置租户或用户凭证。

### 3.2 诊断命令

`hermes doctor` 结果：Python、依赖、目录结构和安全检查通过；SQLite 版本存在 WAL-reset bug 提示；缺少 `.env` 和模型凭证；Docker/Podman 缺失但被标为可选；A2A、Feishu 等插件因系统依赖或凭证未配置而没有进入真实运行状态。

没有执行 `hermes setup`，避免在能力验证阶段写入未审查的外部凭证。

### 3.3 A2A 协议和安全测试

在 Hermes 临时虚拟环境中补装 `pytest` 后运行：

```powershell
python -m pytest -q tests\plugins\test_a2a_plugin.py tests\plugins\test_a2a_phase23.py tests\plugins\test_a2a_tools_gate.py -k "not forward_to_profile_first_contact_creates_then_resumes_fake_hermes"
```

结果：

```text
139 passed, 18 deselected
```

覆盖的证据包括：

- 无 token 时 A2A 只允许 localhost；扩大监听地址需要共享 token 或 peer token；
- bearer token、trusted peers 和工具注册门控；
- 入站提示注入过滤、斜杠命令隔离和出站凭证脱敏；
- Agent Card、Task、Part、JSON-RPC 错误码和上下文；
- 任务持久化、会话历史、审计 JSONL；
- A2A 客户端调用、流式响应、任务查询/取消和 HTTP 往返。

完整测试集曾额外出现 1 个失败：测试夹具创建了没有 `.exe` 扩展名的 Unix 风格伪 `hermes` 脚本，Windows `CreateProcess` 返回 `[WinError 2]`。该失败发生在测试夹具启动阶段，不能据此证明 Hermes A2A 实现失败；但它也说明 Windows 下 profile 子进程启动路径仍需要单独验证。

另外单独运行 A2A 集成测试：

```powershell
python -m pytest -q tests\plugins\test_a2a_plugin.py tests\plugins\test_a2a_phase23.py -m integration
```

结果为 `17 passed, 134 deselected`。这些测试在本机启动真实 `http.server`，读取 Agent Card，并通过 JSON-RPC 完成消息发送、任务查询/列表、流式和推送配置等路径；Agent 回复由测试替身提供，不需要模型 API key。

### 3.4 自定义 MCP 工具真实往返

使用 Hermes 固定版本声明的 `mcp==2.0.0`，在临时环境中启动一个无副作用 stdio MCP 夹具。通过 Hermes 的 `register_mcp_servers` 发现并注册 `parity_canary`，再从 Hermes 工具注册表 dispatch：

```text
REGISTERED ["mcp__parity__parity_canary", ...]
RESULT {"result": "SAFE-CANARY:nonce-123", "structuredContent": {"result": "SAFE-CANARY:nonce-123"}}
```

这证明 Hermes 可以接入自定义 MCP 工具、生成结构化参数 schema、启动 Windows 下的 stdio 子进程并取得结构化结果。实验脚本位于 `D:\Temp\hermes-mcp-probe.py`，不是主项目代码。

### 3.5 写工具拒绝实验

第二个临时 MCP 夹具声明一个写入型 `gated_write` 工具，并将服务器配置为 `trust: untrusted`。把审批回调替身固定为拒绝后 dispatch，结果为：

```text
The user did not approve running write-capable MCP tool 'gated_write' ... The command was NOT run.
MARKER_EXISTS False
```

标记文件不存在，说明拒绝发生在 MCP 传输调用之前。实验脚本位于 `D:\Temp\hermes-mcp-gated-probe.py`，无真实外部副作用。

### 3.6 控制平面边界探针

为了验证本项目的控制平面契约，在 Hermes 工具注册表外包了一层临时 `ControlBoundary`，只允许三种结果：

```text
策略拒绝       → DENIED             → 不触发 Runtime 工具
需要人工确认   → WAITING_APPROVAL   → 生成 approval_id，不触发 Runtime 工具
策略允许       → SUCCEEDED          → 触发 MCP 工具，返回结构化结果
```

探针输出了 `task_id`、`status`、`trace` 和 `audit`，并验证只有允许分支取得 `SAFE-CANARY:allow`。脚本位于 `D:\Temp\hermes-control-boundary-probe.py`。这是控制平面契约的可行性证据，不是主项目实现，也没有验证 Hermes 外部 API 的审批恢复能力。

### 3.7 A2A 外部任务状态与异步完成

临时启动 localhost-only A2A HTTP 服务，让测试处理器在收到任务后等待一个外部事件。另一个 HTTP 调用在处理器等待期间查询 `tasks/get` 和 `tasks/list`，随后释放事件，再等待原始 `message/send` 返回：

```text
working_state: TASK_STATE_WORKING
list_state:    TASK_STATE_WORKING
final_state:   TASK_STATE_COMPLETED
final_text:    approved reply
```

实验脚本位于 `D:\Temp\hermes-a2a-wait-probe.py`。这证明外部调用方可以在任务执行期间查询状态，并在后续事件触发后取得同一任务的终态；它没有证明 Hermes 进程重启后能够自动恢复等待中的任务。

### 3.8 重启后任务恢复边界

在任务处于 `TASK_STATE_WORKING` 时断开第一个 A2A adapter，随后在同一端口启动一个新的 adapter，再用原任务 ID 查询：

```text
before_restart: TASK_STATE_WORKING
after_restart:  error -32001, task not found
```

实验脚本位于 `D:\Temp\hermes-a2a-restart-probe.py`。这确认 Hermes 当前 A2A `TaskStore` 是进程内状态，不能承担本项目的 durable TaskRun。由此形成架构决策：审批等待、任务状态、重启恢复和最终对账必须由自研控制平面持久化；Hermes 只作为可替换的短任务 Runtime，通过 Adapter 接入。

### 3.9 Profile 写入隔离回归测试

在固定版本源码中运行上游 profile 写入隔离套件：

```text
python -m pytest -q tests/conformance/test_profile_write_tripwire.py
→ 4 passed, 0 failed
```

该套件在临时 profile 下触发配置、状态数据库、记忆和 cron 写入，并检查默认 profile 树没有被修改。它比手工 alpha/beta 哨兵实验覆盖更广，支持“当前版本的 profile 写入路径未观察到跨 profile 写漏”的结论；它仍然不是本项目的成员权限、飞书资源授权或凭证注入测试。

## 4. 已证明的能力

当前证据足以证明：

1. Hermes 可以安装在隔离 Python 环境中，并提供 CLI、headless server 和 MCP 入口。
2. Hermes 已实现 A2A v1.0 的主要协议形状和安全基础设施。
3. Hermes 有本地 profile/session/memory 目录和持久化状态，可以作为候选运行时研究；固定版本的 profile 写入隔离回归测试 `4 passed`。
4. A2A 测试明确展示了 localhost 默认限制、认证、信任列表、审计和敏感信息脱敏，这些机制与本项目的跨 Runtime 适配边界一致。
5. 在 localhost-only 条件下，A2A HTTP 适配器可以完成无模型的实际任务往返，并保留任务供后续查询。
6. Hermes 的 MCP 连接层可以发现并调用自定义 stdio 工具，并把工具结果转换为结构化 JSON。
7. Hermes 对不可信 MCP 服务的写工具有默认的审批门控，拒绝时不会发起底层工具调用。
8. 在不调用模型的条件下，本项目拟定的拒绝/待审批/允许三态可以包住 Hermes 工具调用，并输出可审计的任务状态和执行轨迹。
9. A2A HTTP 层能在任务执行期间保持 `TASK_STATE_WORKING`，并在异步事件后将同一任务转为 `TASK_STATE_COMPLETED`。
10. Hermes A2A 任务在 adapter 重启后无法按原任务 ID 查询，不能替代控制平面的 durable TaskRun。

## 5. 尚未证明的能力

以下事项仍不能从本次结果推导出来：

- 本项目控制平面能否在 Hermes 之外统一绑定 ToolPolicy、用户权限、审批记录和审计 ID；本次只证明了 Hermes 自身的 MCP 发现与信任门控；
- 能否返回本项目需要的结构化执行轨迹和工具调用证据；
- 任务等待人工审批时，能否结束进程并在回调后可靠恢复；
- Hermes 外部 server/API 是否能把待审批任务挂起、释放执行资源并在回调后恢复；
- Hermes 进程或机器重启后，等待中的 A2A 任务不能由 Runtime 自身恢复；控制平面如何重新绑定或补偿短任务仍需在阶段 1 设计中验证；
- profile 的工作目录、会话、记忆和凭证是否能满足本项目的成员级隔离；上游写入隔离测试不能替代权限交集、凭证注入和并发运行验证；
- Feishu plugin 在目标租户中的事件、卡片、文件和身份流程；
- 没有模型 key 时无法验证真实端到端推理行为；
- Windows 环境下真实 profile 子进程的启动和停止行为。

## 6. 对项目架构的影响

V-01 目前不能把 Hermes 定为系统核心依赖。建议保留以下边界：

```text
自研控制平面：权限、审批、TaskRun、状态、审计、凭证隔离
Hermes：候选通用 Runtime，通过 Adapter 接入
A2A：跨进程或跨 Runtime 时使用的协议适配边界
飞书：交互、企业资源和人工确认入口
```

如果后续受控工具实验无法通过，会议业务仍使用自研 Worker/LLM Adapter 完成，Hermes 降级为可选 Runtime；不改变权限、审批、审计和任务状态模型。

## 7. Profile 文件边界实验

在不使用真实凭证的临时目录 `D:\Temp\hermes-profile-probe-20260925` 中设置 `HERMES_HOME`，创建两个 profile：`alpha` 和 `beta`。两者均使用 `--ignore-user-config --no-alias --no-skills`，避免读取用户环境或建立外部集成。

观察结果：

```text
alpha → D:\Temp\hermes-profile-probe-20260925\profiles\alpha
beta  → D:\Temp\hermes-profile-probe-20260925\profiles\beta
```

`profile show` 对两个 profile 都显示独立路径，并各自存在 `.env`、`SOUL.md` 和 `profile.yaml`。随后只在 alpha 的 `.env` 写入无敏感含义的 `PROBE_ALPHA_ONLY=profile-alpha` 哨兵，beta 的 `.env` 中没有该字符串。

这证明 Hermes profile 至少提供了文件级配置边界。它没有证明：

- 控制平面如何阻止成员权限交叉读取；
- 运行时子进程、记忆、工作目录和外部凭证在并发情况下是否完全隔离；
- profile 内的 `.env` 是否会被模型或任意工具读取。

因此，成员级权限、凭证注入和工具访问仍必须由本项目控制平面负责，不能把 Hermes profile 当成完整的企业安全边界。

## 8. 下一步实验

1. 继续补充完整结构化执行轨迹和 Windows profile 子进程生命周期证据。
2. 在不配置真实飞书凭证的前提下，保持 localhost-only 的 A2A 和受控工具实验。
3. 只有控制平面 Adapter 的边界明确后，才进入 V-02 飞书租户能力验证。
