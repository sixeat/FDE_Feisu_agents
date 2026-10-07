import json
from fastapi import FastAPI, HTTPException, Query
from .db import init_db, SessionLocal, Document
from .models import DocumentCreate, DocumentOut, SearchResult, Summary
from .processor import ingest
app=FastAPI(title="FDE Information Agent", version="0.1.0")
init_db()
def out(d): return DocumentOut(id=d.id,url=d.url,title=d.title,status=d.status,markdown_path=d.markdown_path,summary=Summary.model_validate_json(d.summary_json) if d.summary_json else None,error=d.error)
@app.get("/health")
def health(): return {"status":"ok"}
@app.post("/documents",response_model=DocumentOut)
def create(req:DocumentCreate):
    try: return out(ingest(str(req.url)))
    except Exception as e: raise HTTPException(502,str(e))
@app.get("/documents/{doc_id}",response_model=DocumentOut)
def get(doc_id:int):
    with SessionLocal() as db:
        d=db.get(Document,doc_id)
        if not d: raise HTTPException(404,"文档不存在")
        return out(d)
@app.get("/documents")
def list_docs(status:str|None=None,category:str|None=None,limit:int=20,offset:int=0):
    with SessionLocal() as db:
        q=db.query(Document)
        if status:q=q.filter_by(status=status)
        if category:q=q.filter(Document.categories.contains(category))
        return [out(x) for x in q.order_by(Document.created_at.desc()).offset(offset).limit(min(limit,100)).all()]
@app.get("/search",response_model=list[SearchResult])
def search(q: str=Query(min_length=1),limit:int=20):
    with SessionLocal() as db:
        rows=db.query(Document).filter((Document.title.contains(q))|(Document.summary_json.contains(q))|(Document.keywords.contains(q))).limit(min(limit,100)).all()
        return [SearchResult(id=x.id,title=x.title or "",url=x.url,status=x.status,markdown_path=x.markdown_path,snippet=(x.summary_json or "")[:240]) for x in rows]
@app.post("/documents/{doc_id}/retry",response_model=DocumentOut)
def retry(doc_id:int):
    with SessionLocal() as db:d=db.get(Document,doc_id); url=d.url if d else None
    if not url: raise HTTPException(404,"文档不存在")
    try:return out(ingest(url,doc_id))
    except Exception as e: raise HTTPException(502,str(e))
