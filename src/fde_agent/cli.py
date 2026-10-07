import json, typer
from .db import init_db,SessionLocal,Document
from .processor import ingest
app=typer.Typer(help="个人信息处理 Agent")
@app.callback()
def setup(): init_db()
@app.command("ingest")
def ingest_url(url:str):
    """处理网页并生成 Markdown。"""
    try:
        d=ingest(url); typer.echo(f"[{d.status}] #{d.id} {d.markdown_path}")
    except Exception as e: typer.echo(f"[failed] {e}",err=True); raise typer.Exit(1)
@app.command()
def search(query:str):
    with SessionLocal() as db:
        for d in db.query(Document).filter((Document.title.contains(query))|(Document.summary_json.contains(query))).all(): typer.echo(f"#{d.id} {d.title} [{d.status}] {d.markdown_path}")
@app.command()
def show(doc_id:int):
    with SessionLocal() as db:
        d=db.get(Document,doc_id)
        if d: typer.echo(d.summary_json or d.error or "无结果")
@app.command()
def retry(doc_id:int):
    with SessionLocal() as db:d=db.get(Document,doc_id); url=d.url if d else None
    if not url: raise typer.BadParameter("文档不存在")
    d=ingest(url,doc_id); typer.echo(f"[{d.status}] {d.markdown_path}")
@app.command()
def stats():
    with SessionLocal() as db:
        rows=db.query(Document).all(); ok=sum(x.status=="success" for x in rows); typer.echo(json.dumps({"total":len(rows),"success":ok,"failure":len(rows)-ok,"success_rate":ok/len(rows) if rows else 0},ensure_ascii=False,indent=2))
if __name__=="__main__": app()
