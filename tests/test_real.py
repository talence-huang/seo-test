"""真實網站研究：以 MockTransport 模擬中文網站、Wayback 與 SerpApi（不碰真實網站）。

端對端研究需要真實 Opus（標記 live，以 pytest -m live 明確執行，每次約 US$0.3）；其餘為免費的單元測試。
"""

from datetime import date

import httpx
import pytest

from seo_intel.core.frontier import detect_trap, template_key
from seo_intel.core.runner import run_real
from seo_intel.llm import AnthropicLLM
from seo_intel.real.env import build_task, canonical_host, check_serp, parse_domain
from seo_intel.real.serp import CsvProvider, RealRankClient, SerpHistory
from seo_intel.tools.http import redact
from seo_intel.tools.text import matches_topic, tokens
from tests import realsite
from tests.conftest import TestClock

TODAY = date(2026, 10, 1)


def _run(settings, tmp_path, monkeypatch, **params):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    log: list[str] = []
    p = {"competitors": [f"https://{realsite.SITE}"], "keywords": realsite.KEYWORDS, "serp_source": "serpapi", **params}
    r = run_real(p, settings, lambda d: AnthropicLLM(settings.models), run_root=tmp_path / "runs",
                 client=realsite.client(log), clock=TestClock(), today=TODAY, history=tmp_path / "serp.jsonl")
    return r, log


def test_chinese_tokens_and_url_templates():
    assert {"3a", "露營", "營區"} <= tokens("3A露營區推薦")
    assert matches_topic("3a露營區 評價", ["3a露營區"]) and not matches_topic("烤肉", ["露營區"])
    assert tokens("/%E9%AB%98%E5%B1%B1%E8%8C%B6/") == tokens("高山茶")      # 網址先解碼
    assert template_key("https://t.tw/guide/%E9%AB%98%E5%B1%B1%E8%8C%B6-1/") == "t.tw/guide/{slug}"
    assert template_key("https://t.tw/2024/05/12/hello-world-post") == "t.tw/{n}/{n}/{n}/{slug}"
    assert detect_trap("t.tw/{n}/{n}/{n}/{slug}") is None and detect_trap("t.tw/archive/{n}/{n}/{n}") == "calendar"


def test_task_input_validation_and_domains():
    assert parse_domain("https://2030.tw/abc?x=1") == "2030.tw" and parse_domain("www.Example.com") == "www.example.com"
    with pytest.raises(ValueError):
        parse_domain("不是網址")
    with pytest.raises(ValueError, match="至少需要"):
        build_task({"competitors": ["a.tw"], "keywords": []}, today=TODAY)
    t = build_task({"competitors": ["a.tw"], "keywords": ["高山茶"], "start": "2026-07-01"}, today=TODAY)
    assert t.site_language == "zh-tw" and t.period_hint == (date(2026, 7, 1), TODAY)
    assert "「高山茶」" in t.question and t.allowed_domains == ["a.tw"]          # 沒有自家站


def test_canonical_host_follows_redirect():
    def handle(req):
        if req.url.host == "a.tw":
            return httpx.Response(301, headers={"location": "https://www.a.tw/"})
        return httpx.Response(200, text="ok")
    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)
    assert canonical_host(client, "a.tw") == "www.a.tw"


def test_serp_source_checks(monkeypatch, tmp_path):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="SERPAPI_API_KEY"):
        check_serp({"serp_source": "serpapi"})
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少欄位"):
        check_serp({"serp_source": "csv", "csv_path": str(bad)})


@pytest.mark.parametrize("header", ["Keyword,Position,URL", "關鍵字,排名,網址", "keyword,Current position,Current URL"])
def test_csv_provider_accepts_common_exports(header):
    p = CsvProvider(f"﻿{header}\n高山茶,3,https://t.tw/a\n高山茶,1,https://x.tw/b\n烏龍茶,abc,https://t.tw/c\n")
    assert [r["position"] for r in p.search(" 高山茶 ")] == [1, 3] and p.search("烏龍茶") == []


def test_serp_history_turns_snapshots_into_movement(tmp_path):
    class Fixed:
        name = "csv"

        def __init__(self, ranks):
            self.ranks = ranks

        def search(self, kw):
            return [{"position": p, "url": f"https://{d}/x", "domain": d, "title": ""} for d, p in self.ranks.items()]

    hist = SerpHistory(tmp_path / "h.jsonl")
    first = RealRankClient(Fixed({"a.tw": 8, "b.tw": 1}), date(2026, 9, 1), hist).serp_snapshot(["k"], ["a.tw"])
    assert first["snapshot_only"] and first["gainers"] == ["a.tw"]               # 第一次：進前 10 的競品
    second = RealRankClient(Fixed({"a.tw": 2, "b.tw": 1}), date(2026, 10, 1), hist).serp_snapshot(["k"], ["a.tw"])
    assert not second["snapshot_only"] and second["before"] == "2026-09-01"
    assert next(m for m in second["movement"] if m["domain"] == "a.tw")["gain"] == 6.0


@pytest.mark.live
def test_real_site_research_end_to_end(settings, tmp_path, monkeypatch):
    """真實 Opus 研究模擬的中文茶葉網站：預設的真相是 /guide/ 改版（字數增加、加入 FAQPage）。"""
    r, log = _run(settings, tmp_path, monkeypatch)
    st = r.state
    assert st.status == "done" and st.goal.goal_type == "competitor_content_strategy"
    assert st.goal.topic_keywords[:2] == realsite.KEYWORDS
    assert {n.kind for n in st.info_needs} <= {"serp_competitors", "site_inventory", "page_changes"}
    if st.claims:                                                             # 有結論時必須是正確的範本
        assert st.claims[0].template == f"{realsite.SITE}/guide/{{slug}}"
    hosts = {u.split("/")[2] for u in log}
    assert hosts <= {realsite.SITE, "serpapi.com", "web.archive.org"}             # 只碰任務網站與資料來源
    assert not any("/cart" in u for u in log)                                     # robots 禁止的路徑
    assert "test-key" not in (r.ctx.run_dir / "trace.jsonl").read_text(encoding="utf-8")   # 金鑰不落地
    assert "test-key" not in (r.ctx.run_dir / "run.json").read_text(encoding="utf-8")


@pytest.mark.live
def test_real_site_without_serp_source(settings, tmp_path, monkeypatch):
    r, log = _run(settings, tmp_path, monkeypatch, serp_source="none")
    assert r.state.status == "done" and "serp_competitors" not in {n.kind for n in r.state.info_needs}
    assert not any("serpapi.com" in u for u in log)


def test_redact_hides_secrets():
    assert "SECRET" not in redact("https://serpapi.com/search.json?q=x&api_key=SECRET")
