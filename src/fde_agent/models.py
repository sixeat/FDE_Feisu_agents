from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, HttpUrl

CATEGORIES = ["Python 与后端","API 与系统集成","LLM 与 Agent","数据库与数据处理","工程化与部署","评测与可观测性","产品与工作流","待分类"]
class Summary(BaseModel):
    title: str
    source_url: str
    one_line_summary: str
    section_summaries: list[str] = []
    key_facts: list[str] = []
    keywords: list[str] = []
    categories: list[str] = ["待分类"]
    difficulty: Literal["入门","中级","高级"] = "入门"
    review_questions: list[str] = []
    uncertainties: list[str] = []
class DocumentCreate(BaseModel): url: HttpUrl
class DocumentOut(BaseModel):
    id: int; url: str; title: str | None = None; status: str; markdown_path: str | None = None; summary: Summary | None = None; error: str | None = None
class SearchResult(BaseModel): id: int; title: str; url: str; status: str; markdown_path: str | None = None; snippet: str | None = None
