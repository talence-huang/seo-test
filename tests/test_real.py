"""真實網站研究：以 MockTransport 模擬中文網站、Wayback 與 SerpApi（不碰真實網站）。

端對端研究需要真實 Opus（標記 live，以 pytest -m live 明確執行，每次約 US$0.3）；其餘為免費的單元測試。
"""

import base64
import json
from datetime import date

import httpx
import pytest

from seo_intel.core.frontier import detect_trap, template_key
from seo_intel.core.runner import prepare_real, run_real
from seo_intel.llm import AnthropicLLM
from seo_intel.real.env import (build_task, canonical_host, check_serp, data_gaps, env_config, load_backlinks,
                                parse_domain)
from seo_intel.real.serp import CsvProvider, RealRankClient, SerpHistory
from seo_intel.tools.http import redact
from seo_intel.tools.text import matches_topic, tokens
from seo_intel.web.jobs import save_uploads
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


BACKLINKS = """Referring page URL,Target URL,Anchor,First seen
https://blog.example.com/p1,https://www.a.tw/guide/x/,高山茶推薦,2026-09-01
https://news.example.net/p2,https://own.tw/,我們,2025-01-01
"""


def test_task_asks_three_questions_and_states_data_gaps():
    t = build_task({"competitors": ["a.tw"], "keywords": ["高山茶"]}, today=TODAY)
    assert all(x in t.question for x in ("On-page", "Off-page", "最值得優先採取的行動"))
    gaps = data_gaps(t)
    assert len(gaps) == 3 and "沒有排名資料" in gaps[0] and "Off-page" in gaps[1] and "自家網址" in gaps[2]
    assert "自家網站 own.tw" in build_task({"competitors": ["a.tw"], "keywords": ["k"], "own_site": "own.tw"},
                                         today=TODAY).question


def test_backlink_csv_coverage_and_errors(tmp_path):
    csv = tmp_path / "ahrefs.csv"
    csv.write_text(BACKLINKS, encoding="utf-8")
    p = {"competitors": ["a.tw", "b.tw"], "keywords": ["高山茶"], "own_site": "own.tw", "serp_source": "csv",
         "backlink_paths": [str(csv)]}
    env = env_config(p)
    data = load_backlinks(env, ["a.tw", "b.tw", "own.tw"])
    t = build_task(p, today=TODAY, backlinks=data)
    assert t.backlink_source == "csv" and t.backlink_domains == ["a.tw", "own.tw"]      # www. 與否視為同一網站
    assert data_gaps(t) == ["反向連結 CSV 沒有 b.tw 的資料：這些競品的 Off-page 策略無法研究"]
    assert load_backlinks(env_config({})) is None
    with pytest.raises(ValueError, match="找不到反向連結 CSV"):
        load_backlinks(env_config({"backlink_paths": [str(tmp_path / "nope.csv")]}))
    with pytest.raises(ValueError, match="沒有指向競品或自家網站"):
        load_backlinks(env, ["c.tw"])
    bad = tmp_path / "bad.csv"
    bad.write_text("Anchor\nx\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少欄位"):
        load_backlinks(env_config({"backlink_paths": [str(bad)]}))


def test_web_uploads_are_saved_as_bytes(tmp_path):
    utf16 = "Referring page URL\tTarget URL\nhttps://s.example.com/a\thttps://a.tw/\n".encode("utf-16")
    files = [{"name": "Ahrefs 匯出 (Excel).csv", "data": base64.b64encode(utf16).decode()}, {"name": "empty.csv", "data": ""}]
    saved = save_uploads(files, "backlinks", root=tmp_path)
    assert len(saved) == 1 and saved[0].read_bytes() == utf16 and saved[0].name.endswith(".csv")
    assert load_backlinks(env_config({"backlink_paths": [str(saved[0])]}), ["a.tw"]).links[0].target_domain == "a.tw"
    with pytest.raises(ValueError, match="無法讀取"):
        save_uploads([{"name": "x.csv", "data": "不是 base64"}], "backlinks", root=tmp_path)


def test_backlink_paths_are_kept_for_resume(settings, tmp_path):
    csv = tmp_path / "links.csv"
    csv.write_text(BACKLINKS.replace("www.a.tw", realsite.SITE), encoding="utf-8")
    ctx = prepare_real({"competitors": [realsite.SITE], "keywords": realsite.KEYWORDS, "backlink_paths": [str(csv)]},
                       settings, lambda d: None, run_root=tmp_path / "runs", client=realsite.client([]),
                       clock=TestClock(), today=TODAY, history=tmp_path / "h.jsonl")
    assert ctx.state.task.backlink_domains == [realsite.SITE]
    env = json.loads((ctx.run_dir / "run.json").read_text(encoding="utf-8"))["env"]
    assert env["backlink_paths"] == [str(csv)]


def test_research_records_strategy_measurements(settings, tmp_path, monkeypatch):
    """不呼叫 LLM 的完整研究：每頁的策略維度寫進紀錄，改版前後算出期間內新採用的做法。"""
    from seo_intel.llm.types import LLMFatalError
    from seo_intel.observe.trace import read_trace

    class NoLLM:
        def call(self, role, *, system, user, schema):
            raise LLMFatalError("不呼叫 LLM")

    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    r = run_real({"competitors": [realsite.SITE], "keywords": realsite.KEYWORDS, "serp_source": "serpapi"},
                 settings, lambda d: NoLLM(), run_root=tmp_path / "runs", client=realsite.client([]), clock=TestClock(),
                 today=TODAY, history=tmp_path / "h.jsonl")
    st = r.state
    guide = [i for u, i in st.fetched.items() if "/guide/" in u and i.get("status") == 200]
    assert guide and all(i["strategy"]["kw_title"] and "internal_links" not in i["strategy"] for i in guide)
    updated = [i for i in guide if i["strategy"]["schema_faq"]]
    assert updated and all(i["strategy"]["content_depth"] > 600 for i in updated)
    diffs = [o for o in st.observations if o.kind == "page_diff" and o.data.get("adopted")]
    assert diffs and {"schema_faq", "faq", "content_depth"} <= set(diffs[0].data["adopted"])
    assert "期間內新採用：" in diffs[0].summary
    lines = [d for rec in read_trace(r.ctx.run_dir / "trace.jsonl") for d in rec.details if d.startswith("策略量測：")]
    assert lines
