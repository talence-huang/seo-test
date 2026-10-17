"""Phase 2 研究迴圈測試：Frontier、預算、工具層、萃取與差異、PolicyGate、完整 episode、暫停續跑、可觀察性。"""

from datetime import date

import pytest

from seo_intel.core.budget import BudgetLedger
from seo_intel.core.episode import build_context, load_checkpoint, new_state
from seo_intel.core.frontier import Frontier, detect_trap, normalize_url, template_key
from seo_intel.core.loop import ResearchLoop
from seo_intel.core.models import Action, BudgetState, Phase, TaskInput
from seo_intel.llm.fake_planner import fake_llm
from seo_intel.observe.store import DbRecorder
from seo_intel.observe.timeline import render_timeline
from seo_intel.observe.trace import TraceWriter, read_trace
from seo_intel.store import Database
from seo_intel.tools.diff import diff_features
from seo_intel.tools.extract import extract
from seo_intel.tools.http import HttpToolkit
from seo_intel.tools.rank import RankClient, rank_scan, serp_scan
from simweb import VirtualClock, build
from simweb.render import render_page

WINDOW = (date(2026, 7, 1), date(2026, 10, 1))
KW = ["long-term care insurance"]


def _task(sim) -> TaskInput:
    g = sim.spec.goal
    return TaskInput(scenario=sim.spec.id, question=g.question, own_site=g.own_site, competitors=list(g.competitors),
                     today=sim.spec.now, period_hint=g.window)


def _ctx(sim, settings, run_dir, state=None, frontier=None):
    state = state or new_state(_task(sim), sim.clock)
    return build_context(state, settings, fake_llm(settings.models), sim.client(), sim.clock, run_dir, frontier)


def _toolkit(sim, settings):
    ledger = BudgetLedger(settings.budget, BudgetState(), sim.clock)
    return HttpToolkit(sim.client(), sim.clock, ledger), ledger


# ---------- Frontier ----------

def test_template_keys_and_traps():
    assert template_key("https://a.sim/guide/long-term-care-insurance-tips-18") == "a.sim/guide/{slug}"
    assert detect_trap(template_key("https://a.sim/archive/2026/10/01")) == "calendar"
    assert detect_trap(template_key("https://a.sim/category/tents-3?page=2")) == "query_params"
    assert detect_trap(template_key("https://a.sim/guide/x-1")) is None
    assert normalize_url("https://A.sim/p?utm_source=x&b=2&a=1#top") == "https://a.sim/p?a=1&b=2"


def test_frontier_prefers_relevant_fresh_and_serp_urls():
    fr = Frontier(KW, WINDOW, ["a.sim"])
    fr.add("https://a.sim/blog/travel-insurance-tips-2", source="sitemap", lastmod="2024-01-01")
    fr.add("https://a.sim/guide/long-term-care-insurance-cost-1", source="sitemap", lastmod="2026-08-10")
    fr.add("https://a.sim/faq/long-term-care-insurance-claims-3", source="serp")
    ranked = [e.url for e, _, _ in fr.best(k=3)]
    assert ranked[-1].endswith("travel-insurance-tips-2")
    top_parts = fr.best(k=1)[0][2]
    assert {"relevance", "freshness", "serp", "template_value", "explore"} <= set(top_parts)


def test_frontier_learns_template_value_within_episode():
    fr = Frontier(KW, WINDOW, ["a.sim"])
    for i in range(6):
        fr.add(f"https://a.sim/blog/long-term-care-insurance-tips-{i}", source="sitemap")
        fr.add(f"https://a.sim/guide/long-term-care-insurance-tips-{i}", source="sitemap")
    for i in range(3):
        fr.mark_fetched(f"https://a.sim/blog/long-term-care-insurance-tips-{i}", ok=True, evidence=False, blocked=False)
        fr.mark_fetched(f"https://a.sim/guide/long-term-care-insurance-tips-{i}", ok=True, evidence=True, blocked=False)
    assert fr.best(k=1)[0][0].template == "a.sim/guide/{slug}"
    assert fr.templates["a.sim/guide/{slug}"].value > fr.templates["a.sim/blog/{slug}"].value


def test_frontier_exclusions_and_trap_filter():
    fr = Frontier(KW, WINDOW, ["a.sim"])
    assert not fr.add("https://other.sim/guide/x-1", source="link")
    assert not fr.add("https://a.sim/search?q=x", source="link", allowed=False)
    assert fr.excluded == {"off_target": 1, "robots": 1}
    fr.add("https://a.sim/archive/2026/10/01", source="link")
    assert fr.best(k=5, exclude_traps=True) == []
    assert fr.best(k=5)[0][2]["trap"] < 0


def test_frontier_checkpoint_roundtrip():
    fr = Frontier(KW, WINDOW, ["a.sim"])
    for i in range(5):
        fr.add(f"https://a.sim/guide/long-term-care-insurance-tips-{i}", source="sitemap", lastmod="2026-08-01")
    fr.mark_fetched("https://a.sim/guide/long-term-care-insurance-tips-0", ok=True, evidence=True, blocked=False)
    again = Frontier.from_dict(fr.to_dict())
    assert [e.url for e, _, _ in again.best(k=3)] == [e.url for e, _, _ in fr.best(k=3)]


# ---------- 預算 ----------

def test_phase_fetch_caps_are_cumulative(settings):
    ledger = BudgetLedger(settings.budget, BudgetState(), VirtualClock())
    assert [ledger.fetch_cap(p) for p in Phase] == [30, 150, 255, 300]


def test_budget_exhaustion_reasons(settings):
    assert BudgetLedger(settings.budget, BudgetState(fetches=300), VirtualClock()).exhausted_reason() == "max_fetches"
    assert BudgetLedger(settings.budget, BudgetState(llm_usd=2.5), VirtualClock()).exhausted_reason() == "max_llm_usd"
    assert BudgetLedger(settings.budget, BudgetState(), VirtualClock()).exhausted_reason() is None


# ---------- 工具層 ----------

def test_toolkit_stays_polite_under_load(settings):
    sim = build("s1")
    http, ledger = _toolkit(sim, settings)
    for i in range(25):
        http.get_site(sim.world.page("shieldguide.sim", "blog", i).url)
    assert not any(sim.server.log.violations().values())
    assert ledger.state.fetches >= 26  # 含 robots.txt


def test_toolkit_never_requests_robots_disallowed_paths(settings):
    sim = build("s1")
    http, _ = _toolkit(sim, settings)
    assert http.get_site("https://shieldguide.sim/search?q=x").error == "robots_disallowed"
    assert not any(r.path.startswith("/search") for r in sim.server.log.records)


def test_toolkit_does_not_retry_403(settings):
    sim = build("s1")
    http, _ = _toolkit(sim, settings)
    res = http.get_site("https://shieldguide.sim/members/area")
    assert res.status == 403 and res.attempts == 1


def test_toolkit_retries_transient_failures(settings):
    from simweb.rng import unit

    sim = build("s1")
    w = sim.world
    url = next(p.url for p in w.iter_pages("shieldguide.sim", "blog") if unit(w.seed, p.url, "flaky") < 0.03)
    http, _ = _toolkit(sim, settings)
    res = http.get_site(url)
    assert res.ok and res.attempts >= 2


# ---------- 萃取、差異、排名工具 ----------

def test_extract_and_diff_recover_planted_change_types():
    sim = build("s1")
    w = sim.world
    ev = sim.spec.event("ev-guide-refresh")
    page = w.page("shieldguide.sim", "guide", w.event_pages[ev.id][0])
    before = extract(render_page(w, page, w.version_at(page, ev.date)), page.url)
    after = extract(render_page(w, page, w.current(page)), page.url)
    assert after.word_count > before.word_count
    assert "FAQPage" in after.schema_types and after.faq_count == 6
    d = diff_features(before, after)
    assert set(d["change_types"]) == set(sim.truth.key_claims[0].change_types)


def test_extract_flags_injection_js_shell_and_malformed():
    s1 = build("s1")
    inj = s1.world.injection_page("shieldguide.sim")
    f = extract(render_page(s1.world, inj, s1.world.current(inj)), inj.url)
    assert f.suspicious_instructions
    s3 = build("s3")
    app_page = next(s3.world.iter_pages("planhive.sim", "app"))
    assert extract(render_page(s3.world, app_page, s3.world.current(app_page)), app_page.url).js_shell
    broken = extract("<html><body><h1>Title</h1><p>unfinished <b>text", "https://a.sim/x")
    assert broken.h1 == "Title" and broken.malformed


def test_rank_tools_measure_planted_effect(settings):
    sim = build("s1")
    http, _ = _toolkit(sim, settings)
    rc = RankClient(http)
    topic = rank_scan(rc, "own-insure.sim", KW, WINDOW, "topic")["aggregate"]
    control = rank_scan(rc, "own-insure.sim", KW, WINDOW, "control")["aggregate"]
    assert topic["mean_delta"] == pytest.approx(5.2, abs=1.0)
    assert control["mean_delta"] == pytest.approx(1.2, abs=1.0)
    serp = serp_scan(rc, topic["top_movers"], date(2026, 8, 1), date(2026, 9, 29), ["shieldguide.sim", "carewise.sim"])
    assert serp["gainers"][0] == "shieldguide.sim"


# ---------- PolicyGate ----------

def test_policy_gate_rules(settings, tmp_path):
    sim = build("s1")
    ctx = _ctx(sim, settings, tmp_path)
    fetch = lambda url: Action(tool="fetch", args={"url": url}, phase=Phase.SAMPLING, score=1, reason="t")  # noqa: E731
    assert ctx.policy.check(fetch("https://shieldguide.sim/guide/x-1")).decision == "allow"
    deny = ctx.policy.check(fetch("https://shieldguide.sim/search?q=x"))
    assert (deny.decision, deny.rule) == ("deny", "ignore_robots")
    off = ctx.policy.check(fetch("https://elsewhere.sim/page"))
    assert (off.decision, off.rule) == ("require_approval", "off_target_domain")
    ctx.state.budget.fetches = settings.budget.max_fetches
    over = ctx.policy.check(fetch("https://shieldguide.sim/guide/x-2"))
    assert (over.decision, over.rule) == ("require_approval", "exceed_budget")


# ---------- 完整 episode ----------

def test_episode_s1_finds_true_template_without_violations(settings, tmp_path):
    sim = build("s1")
    ctx = _ctx(sim, settings, tmp_path)
    ResearchLoop(ctx, [TraceWriter(tmp_path / "trace.jsonl")]).run()
    st = ctx.state
    assert (st.status, st.end_reason) == ("done", "hypothesis_supported")
    page_need = st.needs_of("page_changes")[0]
    assert page_need.resolution.startswith("shieldguide.sim/guide/{slug}")
    assert not any(sim.server.log.violations().values())
    assert len(read_trace(tmp_path / "trace.jsonl")) == st.seq
    steps = [r.step for r in read_trace(tmp_path / "trace.jsonl")]
    assert steps[:3] == ["DEFINE_GOAL", "PLAN_INFO_NEEDS", "SELECT_ACTION"] and steps[-1] == "REPORT"


def test_rich_keyword_lists_do_not_dilute_topic_matching(settings, tmp_path):
    """回歸測試：真實 LLM 會給多組關鍵字（含變體），不應讓主題頁被誤判為非主題頁。"""
    from seo_intel.llm.fake import FakeLLM
    from seo_intel.llm.fake_planner import _responder
    from seo_intel.llm.roles import GoalOut

    def richer(system, user, schema):
        out = _responder(system, user, schema)
        if schema is GoalOut:
            out["topic_keywords"] += ["long term care insurance cost", "long-term care insurance policy",
                                      "long-term care coverage", "ltc insurance"]
        return out

    sim = build("s1")
    state = new_state(_task(sim), sim.clock)
    ctx = build_context(state, settings, FakeLLM(settings.models, {r: richer for r in settings.models.roles}),
                        sim.client(), sim.clock, tmp_path)
    ResearchLoop(ctx, []).run()
    assert len(ctx.state.goal.topic_keywords) == 5
    assert ctx.state.end_reason == "hypothesis_supported"
    assert ctx.state.needs_of("page_changes")[0].resolution.startswith("shieldguide.sim/guide/{slug}")


def test_frontier_skips_non_html_and_singleton_templates():
    fr = Frontier(KW, WINDOW, ["a.sim"])
    assert not fr.add("https://a.sim/sitemap_index.xml", source="link")
    fr.add("https://a.sim/", source="link")
    assert fr.best(k=5, under_sampled=3) == []   # 首頁是單一頁面，不是需要抽樣的範本


@pytest.mark.parametrize("sid", ["s1", "s2", "s3", "s4", "s5", "s6"])
def test_every_scenario_respects_budget_and_policy(settings, tmp_path, sid):
    sim = build(sid)
    ctx = _ctx(sim, settings, tmp_path)
    ResearchLoop(ctx, []).run()
    st = ctx.state
    assert st.status == "done"
    assert st.budget.fetches <= settings.budget.max_fetches
    assert st.budget.api_calls <= settings.budget.max_api_calls
    assert all(v <= settings.budget.max_fetches_per_domain for v in st.budget.fetches_by_domain.values())
    assert not any(sim.server.log.violations().values())


def test_pause_and_resume_reaches_same_conclusion(settings, tmp_path):
    baseline_sim = build("s1")
    baseline = _ctx(baseline_sim, settings, tmp_path / "a")
    ResearchLoop(baseline, []).run()

    sim = build("s1")
    ctx = _ctx(sim, settings, tmp_path / "b")
    loop = ResearchLoop(ctx, [], step_hook=lambda r: r.seq < 40)
    loop.run()
    assert loop.paused and ctx.state.status == "running"

    state, frontier = load_checkpoint(tmp_path / "b")
    resumed_sim = build("s1", clock=VirtualClock(start=state.clock_at))
    resumed = _ctx(resumed_sim, settings, tmp_path / "b", state=state, frontier=frontier)
    ResearchLoop(resumed, []).run()
    assert resumed.state.status == "done"
    assert resumed.state.end_reason == baseline.state.end_reason
    assert resumed.state.needs_of("page_changes")[0].resolution == baseline.state.needs_of("page_changes")[0].resolution


def test_observability_artifacts(settings, tmp_path):
    sim = build("s1")
    ctx = _ctx(sim, settings, tmp_path)
    db = Database(tmp_path / "intel.db")
    ResearchLoop(ctx, [TraceWriter(tmp_path / "trace.jsonl"), DbRecorder(db, ctx.state)]).run()
    st = ctx.state
    assert db.query("SELECT COUNT(*) FROM steps WHERE episode_id = ?", (st.id,))[0][0] == st.seq
    assert db.query("SELECT COUNT(*) FROM llm_calls WHERE episode_id = ?", (st.id,))[0][0] == st.budget.llm_calls
    assert db.query("SELECT status FROM episodes WHERE id = ?", (st.id,))[0][0] == "done"
    assert db.query("SELECT COUNT(*) FROM hypotheses WHERE episode_id = ?", (st.id,))[0][0] == len(st.hypotheses)
    assert db.query("SELECT COUNT(*) FROM claims WHERE episode_id = ?", (st.id,))[0][0] == len(st.claims) >= 1
    html = render_timeline(read_trace(tmp_path / "trace.jsonl"), st.model_dump(mode="json"), ctx.frontier.to_dict(),
                           {"violations": sim.server.log.violations()})
    assert "資訊需求" in html and "候選行動" in html and st.id in html
    db.close()
