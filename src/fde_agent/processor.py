import json, re, time
from pathlib import Path
import httpx
from bs4 import BeautifulSoup
from .config import settings
from .db import Document, SessionLocal
from .models import Summary
from .prompts import SYSTEM_PROMPT, USER_TEMPLATE, PROMPT_VERSION

def fetch_text(url: str) -> tuple[str,str]:
    for attempt in range(3):
        try:
            with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent":"FDE-Info-Agent/0.1"}) as c:
                r=c.get(url); r.raise_for_status(); break
        except Exception:
            if attempt == 2: raise
    soup=BeautifulSoup(r.text,"html.parser")
    for x in soup(["script","style","nav","footer","header","aside"]): x.decompose()
    title=(soup.title.get_text(" ",strip=True) if soup.title else url)
    text=re.sub(r"\s+"," ",soup.get_text(" ",strip=True))
    if len(text)<100: raise ValueError("正文过短或无法提取")
    return title,text
class LLMProvider:
    def summarize(self,url,text,title):
        if not settings.llm_api_key: return Summary(title=title, source_url=url, one_line_summary=text[:180], section_summaries=[text[:500]], key_facts=[text[:300]], keywords=[], categories=["待分类"], review_questions=["这篇文章的核心观点是什么？"])
        payload={"model":settings.llm_model,"messages":[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":USER_TEMPLATE.format(url=url,text=text[:30000])}],"temperature":0,"response_format":{"type":"json_object"}}
        with httpx.Client(timeout=90) as c:
            r=c.post(settings.llm_base_url.rstrip("/")+"/chat/completions",headers={"Authorization":f"Bearer {settings.llm_api_key}"},json=payload); r.raise_for_status(); data=r.json()
        return Summary.model_validate_json(data["choices"][0]["message"]["content"])
def write_markdown(s: Summary, doc_id:int) -> Path:
    p=settings.data_dir/"markdown"; p.mkdir(parents=True,exist_ok=True); path=p/f"{doc_id}-{re.sub(r'[^a-zA-Z0-9一-龥_-]','-',s.title)[:60]}.md"
    meta=f"---\ntitle: {s.title}\nsource_url: {s.source_url}\ncategories: {', '.join(s.categories)}\nkeywords: {', '.join(s.keywords)}\ndifficulty: {s.difficulty}\nprompt_version: {PROMPT_VERSION}\n---\n"
    body=f"\n## 一句话摘要\n{s.one_line_summary}\n\n## 分段摘要\n"+"\n".join(f"- {x}" for x in s.section_summaries)+"\n\n## 关键事实\n"+"\n".join(f"- {x}" for x in s.key_facts)+"\n\n## 复习卡片\n"+"\n".join(f"- Q: {x}" for x in s.review_questions)+"\n\n## 不确定内容\n"+"\n".join(f"- {x}" for x in s.uncertainties)
    path.write_text(meta+body,encoding="utf-8"); return path
def ingest(url:str, retry_id:int|None=None):
    db=SessionLocal(); start=time.time(); doc=None
    try:
        doc=db.get(Document,retry_id) if retry_id else db.query(Document).filter_by(url=url).first()
        if not doc: doc=Document(url=url,status="processing"); db.add(doc); db.commit(); db.refresh(doc)
        else: doc.status="processing"; doc.error=None; db.commit()
        title,text=fetch_text(url); summary=LLMProvider().summarize(url,text,title); path=write_markdown(summary,doc.id)
        doc.title=summary.title; doc.summary_json=summary.model_dump_json(); doc.categories=json.dumps(summary.categories,ensure_ascii=False); doc.keywords=json.dumps(summary.keywords,ensure_ascii=False); doc.markdown_path=str(path); doc.status="success"; doc.duration_ms=int((time.time()-start)*1000); db.commit(); return doc
    except Exception as e:
        if doc: doc.status="failed"; doc.error=str(e); doc.error_stage="processing"; doc.duration_ms=int((time.time()-start)*1000); db.commit()
        raise
    finally: db.close()
