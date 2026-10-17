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


# ---------- 步驟 4：策略探針、假設驗證、結論、優先行動、報告 ----------

from types import SimpleNamespace  # noqa: E402

from seo_intel.core import beliefs, priority, probes  # noqa: E402
from seo_intel.core.claims import inconclusive_reasons, render_markdown, validate_citations  # noqa: E402
from seo_intel.core.models import Claim, EpisodeState, Hypothesis  # noqa: E402
from seo_intel.real.env import build_task  # noqa: E402


def _state(serp: bool = True) -> EpisodeState:
    task = build_task({"competitors": ["a.tw"], "keywords": ["高山茶"], "own_site": "own.tw", "resolve": False,
                       "serp_source": "csv" if serp else "none"}, today=date(2026, 10, 1))
    st = EpisodeState(id="E-1", task=task)
    rank = [{"content_depth": 1100, "faq": True, "schema_faq": True, "kw_title": True}] * 6
    others = [{"content_depth": 300, "faq": False, "schema_faq": False, "kw_title": True}] * 6
    rows = [strategy.compare(rank, others, k) for k in ("content_depth", "faq", "schema_faq", "kw_title")]
    st.add_observation("onpage_profile", "p", {
        "site": "a.tw", "has_serp": serp, "n_ranking": 6 if serp else 0, "n_others": 6,
        "ranking_profile": strategy.profile(rank) if serp else {}, "others_profile": strategy.profile(others),
        "contrast": rows if serp else [], "ranking_templates": {"a.tw/guide/{slug}": 6},
        "adopted_in_window": {"schema_faq": 3}, "n_diffed": 4, "new_in_window": 1})
    st.add_observation("link_profile", "l", {"site": "a.tw", "own": False, "n_links": 10, "n_ref_domains": 8,
                                             "new_ref_domains_in_window": 6, "baseline_in_same_length": 1.0,
                                             "growth_ratio": 6.0, "anchor_share": {"keyword": 0.6},
                                             "share_to_ranking_pages": 0.05})
    return st


def _hyp(hid: str, category: str, dimension: str) -> Hypothesis:
    return Hypothesis(id=hid, round=1, category=category, statement="競品採用這個做法", subject_site="a.tw",
                      dimension=dimension, prediction="預測說明文字", falsification="反證說明文字", prior=0.5,
                      posterior=0.5)


@pytest.mark.parametrize("kind, dimension, expected", [
    ("ranking_contrast", "schema_faq", ["for:strong"]),
    ("ranking_contrast", "kw_title", ["against:moderate"]),          # 兩邊一樣普遍：不是排名頁特有的做法
    ("ranking_contrast", "content_depth", ["for:strong"]),
    ("ranking_prevalence", "faq", ["for:moderate"]),
    ("adoption_timing", "schema_faq", ["for:moderate"]),
    ("link_signal", "link_growth", ["for:strong"]),
    ("link_signal", "anchor_keyword", ["for:strong"]),
    ("link_signal", "link_to_ranking", ["against:moderate"]),
])
def test_strategy_probes(kind, dimension, expected):
    st = _state()
    h = _hyp("H1", "offpage_strategy" if kind == "link_signal" else "onpage_strategy", dimension)
    st.hypotheses = [h]
    drafts = probes.evaluate(SimpleNamespace(state=st), probes.Probe(id="P1", kind=kind, hypothesis_ids=["H1"]))
    assert [f"{d.direction}:{d.strength}" for d in drafts] == expected


def test_without_serp_contrast_is_not_evidence():
    st = _state(serp=False)
    st.hypotheses = [_hyp("H1", "onpage_strategy", "faq")]
    ctx = SimpleNamespace(state=st)
    contrast = probes.evaluate(ctx, probes.Probe(id="P1", kind="ranking_contrast", hypothesis_ids=["H1"]))
    assert [d.direction for d in contrast] == ["none"]
    prevalence = probes.evaluate(ctx, probes.Probe(id="P2", kind="ranking_prevalence", hypothesis_ids=["H1"]))
    assert [(d.direction, d.strength) for d in prevalence] == [("against", "moderate")]      # 主題頁 0%：證據力較弱


def test_strategy_hypothesis_validation():
    st = _state()
    raw = {"category": "onpage_strategy", "statement": "排名頁普遍有 FAQPage", "subject_site": "a.tw", "template": "",
           "dimension": "schema_faq", "prediction": "排名頁多數有 FAQPage", "falsification": "排名頁少於一半有",
           "cited": ["O-1"], "prior": 0.5}
    fields, reason = beliefs.validate(raw, st, set())
    assert reason is None and fields["dimension"] == "schema_faq"
    assert "不是 onpage 的策略維度" in beliefs.validate({**raw, "dimension": "link_growth"}, st, set())[1]
    assert "沒有 b.tw" in beliefs.validate({**raw, "subject_site": "b.tw"}, st, set())[1]


def _claim(cid: str, dim: str, competitor: float, side: str = "onpage") -> Claim:
    return Claim(id=cid, claim_type=f"{side}_strategy", subject_site="a.tw", template="a.tw/guide/{slug}",
                 template_prefix="/guide/", cluster="k", change_types=[dim], date_window=None,
                 direction="competitor_gain", confidence=0.9, hypothesis_id="H1", evidence_ids=["EV-1"],
                 observation_ids=["O-1"], validated=True, dimension=dim, measure={"competitor": competitor})


def test_priority_actions_rank_by_gap_and_effort():
    claims = [_claim("C1", "schema_faq", 1.0), _claim("C2", "content_depth", 1100),
              _claim("C3", "kw_title", 1.0), _claim("C4", "link_growth", 6, "offpage")]
    own = {"schema_faq": 0.0, "content_depth": 300, "kw_title": 1.0, "link_growth": 0}
    actions = priority.rank(claims, own, "O-9")
    assert [a.dimension for a in actions] == ["schema_faq", "content_depth", "link_growth", "kw_title"]
    assert actions[0].id == "A1" and actions[0].status == "do" and actions[-1].status == "maintain"
    assert actions[1].gap == pytest.approx(0.727, abs=1e-3) and "O-9" in actions[0].observation_ids
    unknown = priority.rank(claims[:1], None)
    assert unknown[0].status == "unknown" and unknown[0].gap is None


def test_report_sections_citations_and_reasons():
    st = _state()
    st.end_reason = "inconclusive"
    h = _hyp("H1", "onpage_strategy", "faq")
    h.posterior, h.status = 0.72, "inconclusive"
    st.hypotheses = [h]
    reasons = inconclusive_reasons(st)
    assert reasons[0].startswith("提出的假設都沒有達到成立門檻") and any("H1（信心 0.72" in r for r in reasons)
    claims = [_claim("C1", "schema_faq", 1.0)]
    st.actions = priority.rank(claims, {"schema_faq": 0.0})
    raw = {"headline": "", "summary": "排名頁普遍有 FAQPage [C1]。", "findings": [], "excluded": [], "caveats": [],
           "recommendations": ["先加上 FAQPage [A1]", "沒有依據的建議"]}
    cleaned, stats = validate_citations(raw, st, claims, st.actions)
    assert cleaned["recommendations"] == ["先加上 FAQPage [A1]"] and stats["uncited_recommendations"] == 1
    assert cleaned["headline"].startswith("a.tw 的關鍵策略：FAQPage 結構化資料；最優先的行動")
    md = render_markdown(st, claims, {**cleaned, "inconclusive_reasons": [], "data_gaps": ["缺排名"]}, stats, None)
    for section in ("## 1. On-page 策略", "## 2. Off-page 策略", "## 3. 優先行動", "## 資料缺口"):
        assert section in md
    assert "| A1 | 為 FAQ 加上 FAQPage 結構化資料 |" in md
