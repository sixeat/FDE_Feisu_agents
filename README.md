# FDE Information Agent

网页链接 → 正文提取 → 结构化总结 → Markdown 知识库 + SQLite 索引。

## 运行

```bash
python -m venv .venv
pip install -e .
copy .env.example .env   # Windows
fde-agent ingest-url https://example.com/article
uvicorn fde_agent.api:app --reload
```

未配置 `LLM_API_KEY` 时使用本地 mock 总结，便于验证流程；配置后调用 OpenAI 兼容 `/chat/completions` 接口。

数据默认写入 `data/`，失败任务会保留在 SQLite 中并可通过 `retry` 重试。
