# 飞书 Agent 组织管理系统

面向高校课程组教研的飞书内自主 Agent 组织系统。目标是每日从成员授权的聊天与运行记录中发现可智能化流程，由生态 Super Agent 协调架构师、审核官、运维官和业务 Agent，完成候选构建、评测、受控发布、运行和持续改进。Agent 生命周期治理是这条组织闭环的控制机制；线上消息入口、正式卡片审核及飞书任务持续写入保持关闭。

## 项目文档

- [项目总规划一页版](docs/项目总规划一页版.md)：项目定位、范围和阶段导航。
- [组织管理系统计划书](docs/FDE-Agent-组织管理系统-计划书.md)：目标设计、边界和验收条件。
- [组织管理系统进度记录](docs/FDE-Agent-组织管理系统-进度.md)：唯一的当前状态与实施证据来源。
- `docs/` 中的阶段 0 文档记录外部能力验证和历史依据；被计划书引用的证据文件应与计划书一起保留。

## 本地开发

需要 Python 3.11 或更新版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m pytest -q
```

本地合成生命周期演练只监听回环地址，不连接飞书：

```powershell
uvicorn scripts.local_lifecycle_demo:app --host 127.0.0.1 --port 8212
```

打开 `http://127.0.0.1:8212/local-demo`。演练数据默认写入 `deploy/data/local-lifecycle-demo.sqlite3`，可通过 `FDE_LOCAL_DEMO_DB` 指定独立数据库。

登录后打开工作台的“流程发现”页，可以登记群聊/单聊来源、手动扫描最近 24 小时、人工采纳候选、生成待审核 Agent 草稿或撤销订阅。当前需要成员手动填写飞书会话 ID；暂未提供会话选择器。

本地演练在 `http://127.0.0.1:8212/local-demo` 使用固定合成聊天 Reader，完整演示订阅、扫描、审核、草稿和撤权，不会连接飞书。浏览器验收脚本为 `node scripts/check_workflow_discovery_ui.cjs`，需安装 Playwright 并设置 `FDE_PLAYWRIGHT_MODULE`；默认目标为 8212，可用 `FDE_LOCAL_DEMO_URL` 覆盖。

真实 OAuth 工作台已集成相同界面与 API。线上 OAuth 已配置群聊 `im:message.group_msg:get_as_user` 和单聊 `im:message.p2p_msg:get_as_user` 两个 scope，飞书开放平台也已开通这两项用户身份权限。“流程发现”会显示当前账号的群聊/单聊授权状态；若已有工作台登录会话，点“重新授权聊天读取”即可再次进入 OAuth。状态检查只返回是否可用，不暴露令牌。当前线上用户授权记录为 0，真实群聊/单聊读取尚未验收。消息入口、正式卡片审核和飞书任务写入仍保持关闭。
