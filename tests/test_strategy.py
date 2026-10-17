"""策略維度量測的單元測試（不連網、不呼叫 LLM）。"""

from datetime import date

import pytest

from seo_intel.core import strategy

WINDOW = (date(2026, 7, 3), date(2026, 10, 1))


def test_fisher_exact_two_sided():
    assert strategy.fisher_p(3, 0, 0, 3) == pytest.approx(0.1)
    assert strategy.fisher_p(5, 0, 0, 5) == pytest.approx(0.0079, abs=1e-4)
    assert strategy.fisher_p(2, 2, 2, 2) == pytest.approx(1.0)


def test_page_values_and_contrast():
    phrases = strategy.phrases_of(["高山茶"])
    ranking = [strategy.page_values(f"https://a.tw/guide/%E9%AB%98%E5%B1%B1%E8%8C%B6-{i}/",
                                    {"title": "高山茶入門", "h1": "高山茶入門", "words": 1100, "h2": ["a"] * 6,
                                     "faq": 6, "schema": ["Article", "FAQPage"], "meta_desc": "x", "table": True,
                                     "modified": "2026-09-01"}, phrases, WINDOW, inlinks=5) for i in range(6)]
    others = [strategy.page_values(f"https://a.tw/blog/post-{i}-notes/",
                                   {"title": "生活隨筆", "words": 300, "h2": ["a"], "faq": 0, "schema": ["Article"],
                                    "table": True, "modified": "2025-01-01"}, phrases, WINDOW) for i in range(6)]
    assert ranking[0]["kw_title"] and ranking[0]["kw_url"] and ranking[0]["fresh"] and not others[0]["kw_title"]
    faq = strategy.compare(ranking, others, "schema_faq")
    assert faq["group"] == 1.0 and faq["others"] == 0.0 and faq["p"] < 0.01
    table = strategy.compare(ranking, others, "comparison_table")       # 兩邊都有：不是排名頁特有的做法
    assert table["diff"] == 0 and table["p"] == 1.0
    depth = strategy.compare(ranking, others, "content_depth")
    assert depth["ratio"] == pytest.approx(3.67, abs=0.01) and depth["p"] < 0.01
    own = strategy.profile(others)
    assert strategy.gap("schema_faq", 1.0, own["schema_faq"]) == 1.0
    assert strategy.gap("content_depth", 1100, own["content_depth"]) == pytest.approx(0.727, abs=1e-3)
    assert strategy.gap("comparison_table", 1.0, own["comparison_table"]) == 0.0


def test_adoption_and_description():
    before = {"content_depth": 320, "sections": 3, "faq": False, "schema_faq": False, "kw_title": True,
              "comparison_table": True, "fresh": False}
    after = {"content_depth": 1101, "sections": 5, "faq": True, "schema_faq": True, "kw_title": True,
             "comparison_table": False, "fresh": True}
    adopted, dropped = strategy.adoption(before, after)
    assert adopted == ["content_depth", "sections", "faq", "schema_faq"] and dropped == ["comparison_table"]
    assert strategy.adoption({"content_depth": 1000}, {"content_depth": 1100}) == ([], [])     # 小幅增加不算新做法
    line = strategy.describe(after)
    assert line.startswith("1,101 字、5 段 H2；結構化資料：FAQPage；有：FAQ 區塊、title 含關鍵字")
    assert "沒有：比較表" in line and "HowTo" not in line
