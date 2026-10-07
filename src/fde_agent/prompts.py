SYSTEM_PROMPT = "你是严谨的 FDE 学习助理。只根据给定正文总结，不要编造事实；不确定内容放入 uncertainties。"
USER_TEMPLATE = """请将以下文章输出为 JSON，字段必须符合 Summary schema：title, source_url, one_line_summary, section_summaries, key_facts, keywords, categories, difficulty, review_questions, uncertainties。categories 只能使用：Python 与后端、API 与系统集成、LLM 与 Agent、数据库与数据处理、工程化与部署、评测与可观测性、产品与工作流、待分类。\nURL: {url}\n正文：\n{text}"""
PROMPT_VERSION = "v1"
