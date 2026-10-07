from fde_agent.models import Summary
from fde_agent.processor import write_markdown
def test_summary_and_markdown(tmp_path,monkeypatch):
    from fde_agent import config
    monkeypatch.setattr(config.settings,"data_dir",tmp_path)
    s=Summary(title="测试文章",source_url="https://example.com",one_line_summary="摘要",key_facts=["事实"])
    p=write_markdown(s,1)
    assert p.exists() and "摘要" in p.read_text(encoding="utf-8")
