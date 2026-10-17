"""Phase 3：假設、證據、信心更新、紅隊驗證、主張推導與引用驗證。"""

from datetime import date

import pytest

from seo_intel.core import beliefs, probes
from seo_intel.core.claims import validate_citations
from seo_intel.core.episode import build_context, new_state
from seo_intel.core.loop import ResearchLoop
from seo_intel.core.models import EvidenceItem, Hypothesis, TaskInput
from seo_intel.core.steps import _red_team
from seo_intel.llm.fake import FakeLLM
from seo_intel.llm.fake_planner import fake_llm
from seo_intel.llm.roles import VerifierOut
from simweb import build


def _ctx(sim, settings, tmp_path, llm=None):
    g = sim.spec.goal
    task = TaskInput(scenario=sim.spec.id, question=g.question, own_site=g.own_site, competitors=list(g.competitors),
                     today=sim.spec.now, period_hint=g.window)
    return build_context(new_state(task, sim.clock), settings, llm or fake_llm(settings.models), sim.client(),
                         sim.clock, tmp_path)


def _hyp(hid="H1", template="a.sim/guide/{slug}", site="a.sim", prior=0.4, category="competitor_content_change"):
    return Hypothesis(id=hid, round=1, category=category, statement="statement long enough", subject_site=site,
                      template=template, prediction="prediction long", falsification="falsification long",
                      cited=["O-1"], prior=prior, posterior=prior, status="testing")


def _ev(direction, strength, hid="H1"):
    return EvidenceItem(id="EV-1", hypothesis_id=hid, probe="x", direction=direction, strength=strength,
                        observation_ids=["O-1"], note="n")


# ---------- 信念數學 ----------

def test_log_odds_update_is_symmetric_and_bounded():
    assert beliefs.posterior(0.5, []) == 0.5
    up = beliefs.posterior(0.5, [_ev("for", "strong")])
    down = beliefs.posterior(0.5, [_ev("against", "strong")])
    assert up == pytest.approx(1 - down, abs=1e-4) and up == pytest.approx(0.8176, abs=1e-3)
    assert beliefs.posterior(0.5, [_ev("for", "strong")] * 20) == 0.999


def test_priors_are_capped_so_belief_must_be_earned():
    assert beliefs.clamp_prior(0.95) == 0.6
    assert beliefs.clamp_prior(0.01) == 0.1


def test_supported_requires_red_team_and_conflict_blocks_conclusion():
    h = _hyp()
    h.posterior = 0.95
    assert beliefs.status_of(h, []) == "testing"           # 尚未經過紅隊驗證
    h.verified = True
    assert beliefs.status_of(h, []) == "supported"
    assert beliefs.status_of(h, [_ev("for", "strong"), _ev("against", "strong")]) == "testing"
    h.posterior = 0.1
    assert beliefs.status_of(h, []) == "refuted"


def test_unfalsifiable_or_ungrounded_hypotheses_are_rejected(settings, tmp_path):
    ctx = _ctx(build("s1"), settings, tmp_path)
    ctx.state.add_observation("rank_change", "x", {"site": "own-insure.sim"})
    raw = {"category": "competitor_content_change", "statement": "競品改版造成下滑的說明", "subject_site": "shieldguide.sim",
           "template": "shieldguide.sim/guide/{slug}", "mechanism": "", "prediction": "", "falsification": "反證條件寫得夠長",
           "cited": ["O-1", "O-99"], "prior": 0.9}
    fields, reason = beliefs.validate(raw, ctx.state, {"shieldguide.sim/guide/{slug}"})
    assert "prediction" in reason
    assert fields["cited"] == ["O-1"] and fields["prior"] == 0.6
    raw |= {"prediction": "預測寫得夠長也可量測", "template": "shieldguide.sim/invented/{slug}"}
    assert "不在已觀察到的範本" in beliefs.validate(raw, ctx.state, {"shieldguide.sim/guide/{slug}"})[1]


# ---------- 探針判定規則 ----------

def test_burst_separates_background_churn_from_concentrated_change(settings, tmp_path):
    ctx = _ctx(build("s3"), settings, tmp_path)
    rows = [{"template": "integrations/{slug}", "n_urls": 120, "n_recent": 50, "recent_share": 0.417},
            {"template": "tag/{slug}", "n_urls": 6000, "n_recent": 360, "recent_share": 0.06},
            {"template": "blog/{slug}", "n_urls": 8000, "n_recent": 480, "recent_share": 0.06}]
    ctx.state.add_observation("site_inventory", "inv", {"domain": "planhive.sim", "templates": rows})
    hot = probes._burst(ctx, _hyp(template="planhive.sim/integrations/{slug}", site="planhive.sim"))
    churn = probes._burst(ctx, _hyp(template="planhive.sim/tag/{slug}", site="planhive.sim"))
    assert (hot[0].direction, hot[0].strength) == ("for", "moderate")
    assert (churn[0].direction, churn[0].strength) == ("against", "moderate")
    assert "背景變動" in churn[0].note


def test_timing_uses_earliest_change_so_gradual_rollouts_are_not_penalised(settings, tmp_path):
    """回歸測試：漸進式上線（7/1～7/31）時，中位日晚於排名起始日，但最早的頁面已先上線。"""
    ctx = _ctx(build("s6"), settings, tmp_path)
    st = ctx.state
    st.add_observation("rank_change", "r", {"site": "flowboard.sim", "aggregate": {"onset_median": "2026-07-11", "n_queries": 8}})
    tkey = "flowboard.sim/compare/{slug}"
    for i, day in enumerate((1, 9, 18, 24, 29)):
        url = f"https://flowboard.sim/compare/x-{i}"
        st.fetched[url] = {"template": tkey, "change": "new", "published": f"2026-07-{day:02d}", "on_topic": True}
        st.template_changes.setdefault(tkey, []).append(url)
    from seo_intel.core.models import ResearchGoal
    st.goal = ResearchGoal(restated_question="q", goal_type="competitor_content_strategy", topic_keywords=["gantt charts"],
                           focus_domains=["flowboard.sim"], window_start=date(2026, 6, 1), window_end=date(2026, 10, 1),
                           success_criteria=[])
    drafts = probes._timing(ctx, _hyp(template=tkey, site="flowboard.sim", category="competitor_new_pages"))
    assert drafts[0].direction == "for"
    for url in st.template_changes[tkey]:
        st.fetched[url]["published"] = "2026-08-20"     # 連最早的變動都晚於起始日
    assert probes._timing(ctx, _hyp(template=tkey, site="flowboard.sim"))[0].strength == "strong"


# ---------- 紅隊與引用驗證 ----------

def test_red_team_objections_need_valid_citations(settings, tmp_path):
    def verifier(system, user, schema):
        assert schema is VerifierOut
        assert "mechanism" not in user        # 驗證者看不到提出者的推理
        return {"assessment": "weak", "summary": "s", "requested_probes": [],
                "objections": [{"text": "有引用的反駁", "cited_observations": ["O-1"], "severity": "major"},
                               {"text": "沒有引用的反駁", "cited_observations": ["O-404"], "severity": "major"}]}

    sim = build("s1")
    ctx = _ctx(sim, settings, tmp_path, llm=FakeLLM(settings.models, {"verifier": verifier}))
    ctx.state.add_observation("rank_change", "r", {"site": "own-insure.sim"})
    h = _hyp(template="shieldguide.sim/guide/{slug}", site="shieldguide.sim")
    h.mechanism = "提出者的推理（不應給驗證者看）"
    ctx.state.hypotheses.append(h)
    _, lines = _red_team(ctx, h)
    assert h.verified
    assert [e.strength for e in ctx.state.evidence] == ["moderate"]
    assert any("不計分" in x for x in lines)


def test_red_team_cannot_outvote_evidence_by_volume(settings, tmp_path):
    """回歸測試：一次審查提出再多反駁，最多只計入 2 項（一中一弱）。"""
    many = [{"text": f"反駁 {i}", "cited_observations": ["O-1"], "severity": "major"} for i in range(6)]
    verifier = lambda s, u, sc: {"assessment": "weak", "summary": "s", "requested_probes": [], "objections": many}  # noqa: E731
    ctx = _ctx(build("s1"), settings, tmp_path, llm=FakeLLM(settings.models, {"verifier": verifier}))
    ctx.state.add_observation("rank_change", "r", {"site": "own-insure.sim", "aggregate": {}})
    h = _hyp(template="shieldguide.sim/guide/{slug}", site="shieldguide.sim")
    ctx.state.hypotheses.append(h)
    _, lines = _red_team(ctx, h)
    assert [e.strength for e in ctx.state.evidence] == ["moderate", "weak"]
    assert sum("記錄但不計分" in x for x in lines) == 4


def test_sample_rate_uses_binomial_test_against_background(settings, tmp_path):
    from seo_intel.core.models import Probe

    ctx = _ctx(build("s3"), settings, tmp_path)
    st = ctx.state
    rows = [{"template": "tag/{slug}", "n_urls": 1000, "n_recent": 60, "recent_share": 0.06}]
    st.add_observation("site_inventory", "inv", {"domain": "planhive.sim", "templates": rows})
    h = _hyp(template="planhive.sim/tag/{slug}", site="planhive.sim")
    st.hypotheses.append(h)

    def outcome(changed, total):
        urls = [f"https://planhive.sim/tag/x-{i}" for i in range(total)]
        st.probes = [Probe(id="P1", kind="template_change_sample", hypothesis_ids=["H1"], sample=urls, status="executed")]
        st.fetched = {u: {"status": 200, "evidence": i < changed, "change": "new" if i < changed else None}
                      for i, u in enumerate(urls)}
        d = probes._sample(ctx, h)[0]
        return d.direction, d.strength

    assert outcome(1, 6)[0] == "none"                  # 6 頁中 1 頁：與 6% 背景分不開
    assert outcome(5, 6) == ("for", "strong")           # 遠高於背景
    assert outcome(0, 8) == ("against", "moderate")     # 顯著低於策略性改版的 30%


def test_template_sample_is_random_not_freshness_ranked(settings, tmp_path):
    """回歸測試：比例估計的樣本不可用 frontier 排序（偏好最近更新的頁面）選出。"""
    from seo_intel.core.frontier import Frontier

    ctx = _ctx(build("s1"), settings, tmp_path)
    ctx.frontier = Frontier(["long-term care insurance"], (date(2026, 7, 1), date(2026, 10, 1)), ["a.sim"])
    for i in range(40):
        lastmod = "2026-08-01" if i < 10 else "2024-01-01"   # 前 10 頁「最近更新」，frontier 會優先選它們
        ctx.frontier.add(f"https://a.sim/tag/long-term-care-insurance-{i}", source="sitemap", lastmod=lastmod)
    sample = probes._random_sample(ctx, "a.sim/tag/{slug}", 6)
    fresh_first = [e.url for e, _, _ in ctx.frontier.best(k=6, templates=["a.sim/tag/{slug}"])]
    assert len(sample) == 6 and sample != fresh_first
    assert sum(int(u.rsplit("-", 1)[1]) < 10 for u in sample) < 6


def test_invalid_citations_and_claims_are_removed(settings, tmp_path):
    ctx = _ctx(build("s1"), settings, tmp_path)
    ctx.state.add_observation("rank_change", "r", {"site": "own-insure.sim"})
    raw = {"headline": "結論 [C1] [O-1] [O-77]", "summary": "", "caveats": ["[EV-9]"], "recommendations": [],
           "findings": [{"claim_id": "C9", "explanation": "x"}], "excluded": [{"hypothesis_id": "H9", "reason": "x"}]}
    cleaned, stats = validate_citations(raw, ctx.state, [])
    assert cleaned["headline"].strip() == "結論  [O-1]".strip()
    assert stats == {"valid": 1, "invalid_removed": 3, "dropped_findings": 1, "dropped_excluded": 1}


# ---------- 完整 episode：主張必須與標準答案一致 ----------

@pytest.mark.parametrize("sid", ["s1", "s2", "s3", "s4", "s5", "s6"])
def test_claims_match_planted_truth(settings, tmp_path, sid):
    sim = build(sid)
    ctx = _ctx(sim, settings, tmp_path)
    ResearchLoop(ctx, []).run()
    st = ctx.state
    truth = sim.truth.key_claims[0]
    assert st.end_reason == "hypothesis_supported"
    assert len(st.claims) == 1, [c.template for c in st.claims]
    c = st.claims[0]
    assert (c.subject_site, c.template_prefix) == (truth.subject_site, truth.template_prefix)
    assert set(c.change_types) == set(truth.change_types)
    assert c.validated and c.confidence >= beliefs.SUPPORTED_AT
    distractor_sites = {(d.subject_site, d.template_prefix) for d in sim.truth.distractors}
    assert not any((x.subject_site, x.template_prefix) in distractor_sites for x in st.claims)
    assert not any(sim.server.log.violations().values())
    assert (tmp_path / "report.md").is_file()


def test_llm_failure_while_forming_hypotheses_cannot_loop_forever(settings, tmp_path):
    """回歸測試：假設提出者失敗時，最多重試 MAX_HYPOTHESIS_ROUNDS 輪，以 no_valid_hypotheses 結束。"""
    from seo_intel.llm.fake_planner import _responder
    from seo_intel.llm.types import LLMTransientError

    def broken(system, user, schema):
        if schema.__name__ == "HypothesesOut":
            raise LLMTransientError("模擬 API 錯誤")
        return _responder(system, user, schema)

    sim = build("s1")
    ctx = _ctx(sim, settings, tmp_path, llm=FakeLLM(settings.models, {r: broken for r in settings.models.roles}))
    ResearchLoop(ctx, []).run()
    st = ctx.state
    assert (st.status, st.end_reason) == ("done", "no_valid_hypotheses")
    assert st.hypothesis_round == 2 and st.claims == []
    assert st.budget.iterations < 400


def test_phase2_failure_s3_is_fixed_by_competing_hypotheses(settings, tmp_path):
    """Phase 2 時 s3 被標籤頁的背景變動誤導；加入競爭假設與鑑別性證據後，結論應為 /integrations/。"""
    sim = build("s3")
    ctx = _ctx(sim, settings, tmp_path)
    ResearchLoop(ctx, []).run()
    assert ctx.state.claims[0].template == "planhive.sim/integrations/{slug}"
    probes_used = {e.probe for e in ctx.state.evidence}
    assert {"serp_template_share", "template_burst", "timing_alignment"} <= probes_used
