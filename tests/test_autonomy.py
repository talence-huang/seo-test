"""Phase 5：自治邊界（PolicyGate 判定矩陣、fail-closed、一次性授權、拒絕不再詢問）與人工核准流程。

全部使用 FakeLLM 與 SimWeb，結果確定、免費。
"""

import json
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from seo_intel.core import approvals
from seo_intel.core.episode import CHECKPOINT, build_context, load_checkpoint, new_state
from seo_intel.core.frontier import Frontier
from seo_intel.core.models import Action, EpisodeState, Phase
from seo_intel.core.runner import resume_run, run_scenario, scenario_task
from seo_intel.llm.fake_planner import fake_llm
from simweb import build

GUIDE = "https://shieldguide.sim/guide/long-term-care-insurance-guide-1"


def _llm(settings):
    return lambda run_dir: fake_llm(settings.models)


def _tight(settings, **budget):
    return settings.model_copy(update={"budget": settings.budget.model_copy(update=budget)})


def _ctx(settings, tmp_path, known: set[str] | None = None):
    sim = build("s1")
    st = new_state(scenario_task(sim), sim.clock)
    ctx = build_context(st, settings, fake_llm(settings.models), sim.client(), sim.clock, tmp_path)
    ctx.frontier = Frontier(["long-term care insurance"], (sim.spec.goal.window[0], sim.spec.now), st.task.allowed_domains)
    for u in known or {GUIDE}:
        ctx.frontier.add(u, source="sitemap")
    return ctx


def _act(tool="fetch", **args):
    return Action(tool=tool, args=args, phase=Phase.SAMPLING, score=1.0, reason="測試")


# ---------- PolicyGate 判定矩陣 ----------

@pytest.mark.parametrize("args, rule", [
    ({"url": GUIDE, "user_agent": "Googlebot"}, "spoof_user_agent"),
    ({"url": GUIDE, "headers": {"x-forwarded-for": "1.2.3.4"}}, "spoof_user_agent"),
    ({"url": GUIDE, "proxy": "socks5://x"}, "rotate_ip"),
    ({"url": GUIDE, "captcha_token": "abc"}, "bypass_captcha"),
    ({"url": "https://shieldguide.sim/account/orders"}, "access_login_gated"),
    ({"url": "https://shieldguide.sim/search?q=all"}, "follow_page_instructions"),   # 只出現在注入文字中的網址
])
def test_forbidden_actions_are_denied(settings, tmp_path, args, rule):
    ctx = _ctx(settings, tmp_path)
    v = ctx.policy.check(_act(**args))
    assert (v.decision, v.rule) == ("deny", rule)


def test_robots_disallowed_url_is_denied_even_if_known(settings, tmp_path):
    url = "https://shieldguide.sim/search?q=all"
    v = _ctx(settings, tmp_path, known={url}).policy.check(_act(url=url))
    assert (v.decision, v.rule) == ("deny", "ignore_robots")


@pytest.mark.parametrize("action, rule", [
    (_act(url="https://attacker.sim/collect"), "off_target_domain"),
    (_act(url=GUIDE, rate_interval=0.5), "raise_rate"),
    (_act(url=GUIDE, pages=5000), "bulk_crawl"),
])
def test_risky_actions_require_approval(settings, tmp_path, action, rule):
    v = _ctx(settings, tmp_path, known={GUIDE, "https://attacker.sim/collect"}).policy.check(action)
    assert (v.decision, v.rule) == ("require_approval", rule)


def test_budget_and_block_rate_require_approval(settings, tmp_path):
    ctx = _ctx(settings, tmp_path)
    ctx.policy.block_rate = lambda d: (0.5, 12)
    assert ctx.policy.check(_act(url=GUIDE)).rule == "continue_after_blocks"
    ctx.state.budget.fetches = settings.budget.max_fetches
    assert ctx.policy.check(_act(url=GUIDE)).rule == "exceed_budget"


def test_unknown_rule_fails_closed(settings, tmp_path):
    ctx = _ctx(settings, tmp_path)
    ctx.policy.cfg = ctx.policy.cfg.model_copy(update={"require_approval": frozenset()})
    v = ctx.policy.check(_act(url="https://attacker.sim/x"))
    assert v.decision == "require_approval" and "保守處理" in v.reason


def test_llm_cannot_invent_tools():
    """行動只能由程式產生；工具名稱是封閉的型別，例如「寫入政策檔」根本無法表達。"""
    with pytest.raises(ValidationError):
        Action(tool="write_file", args={"path": "config/policy.yaml"}, phase=Phase.SAMPLING, score=1, reason="x")


def test_grant_is_one_time_and_denial_is_remembered(settings, tmp_path):
    ctx = _ctx(settings, tmp_path, known={GUIDE, "https://attacker.sim/a", "https://attacker.sim/b"})
    st, a = ctx.state, _act(url="https://attacker.sim/a")
    st.grants.append({"approval": "A-1", "rule": "off_target_domain", "signature": a.signature})
    assert ctx.policy.check(a).decision == "allow"
    assert ctx.policy.check(a).decision == "require_approval"   # 一次性：用掉就失效
    st.denials.append({"approval": "A-2", "rule": "off_target_domain", "scope": "attacker.sim"})
    v = ctx.policy.check(_act(url="https://attacker.sim/b"))
    assert v.decision == "deny" and "不再詢問" in v.reason          # 同一範圍不再詢問


# ---------- 核准流程 ----------

def _decide(run_dir, approve, note=None, now=None):
    """模擬 seo-intel approvals approve / reject：決定寫回檢查點。"""
    data = json.loads((run_dir / CHECKPOINT).read_text(encoding="utf-8"))
    st = EpisodeState.model_validate(data["state"])
    a = approvals.decide(st, None, approve, note, now=now)
    data["state"] = st.model_dump(mode="json")
    (run_dir / CHECKPOINT).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return a


def _paused(settings, tmp_path, **budget):
    tight = _tight(settings, **budget)
    r = run_scenario("s1", tight, _llm(settings), run_root=tmp_path, artifacts=False)
    assert r.state.status == "waiting_approval"
    return r, tight


def test_budget_exhaustion_pauses_with_estimate(settings, tmp_path):
    r, _ = _paused(settings, tmp_path, max_fetches=30)
    a = r.state.pending_approval
    assert a.rule == "exceed_budget" and a.kind == "budget"
    assert a.estimate["fetches"] > 0 and a.extend["max_fetches"] > a.estimate["fetches"]   # 含重試餘裕
    assert load_checkpoint(r.ctx.run_dir)[0].status == "waiting_approval"                  # 硬暫停：已存檔
    with pytest.raises(approvals.ApprovalPending):
        resume_run(r.ctx.run_dir, _tight(settings, max_fetches=30), _llm(settings), artifacts=False)


def test_approve_extends_budget_once_and_finishes(settings, tmp_path):
    r, tight = _paused(settings, tmp_path, max_fetches=30)
    _decide(r.ctx.run_dir, True, "同意")
    done = resume_run(r.ctx.run_dir, tight, _llm(settings), artifacts=False)
    st = done.state
    assert st.status == "done" and st.end_reason == "hypothesis_supported"
    assert [a.status for a in st.approvals] == ["used"]                 # 一次核准就夠
    assert st.budget.fetches <= 30 + st.budget.extra["max_fetches"]
    assert not st.limitations


def test_reject_degrades_and_records_limitation(settings, tmp_path):
    r, tight = _paused(settings, tmp_path, max_fetches=30)
    _decide(r.ctx.run_dir, False, "預算到此為止")
    done = resume_run(r.ctx.run_dir, tight, _llm(settings), artifacts=False)
    st = done.state
    assert st.status == "done" and st.budget.fetches == 30 and len(st.approvals) == 1   # 不再詢問
    assert any("人工拒絕" in x for x in st.limitations)
    assert any("人工拒絕" in x for x in st.report["caveats"])                           # 報告寫明限制
    assert st.claims[0].template == "shieldguide.sim/guide/{slug}"                       # 以既有證據下結論


def test_expired_approval_is_not_honoured(settings, tmp_path):
    r, tight = _paused(settings, tmp_path, max_fetches=30)
    a = r.state.pending_approval
    created = datetime.fromisoformat(a.created_at)
    _decide(r.ctx.run_dir, True, now=created)
    later = datetime.fromisoformat(a.expires_at) + timedelta(minutes=1)
    again = resume_run(r.ctx.run_dir, tight, _llm(settings), artifacts=False, now=later)
    st = again.state
    assert [x.status for x in st.approvals] == ["expired", "pending"] and st.status == "waiting_approval"
    assert st.budget.extra == {}


def test_llm_budget_exhaustion_asks_before_stopping(settings, tmp_path):
    r, tight = _paused(settings, tmp_path, max_llm_usd=0.02)
    a = r.state.pending_approval
    assert a.extend == {"max_llm_usd": 0.01}                            # 延長設定值的 50%
    _decide(r.ctx.run_dir, False)
    st = resume_run(r.ctx.run_dir, tight, _llm(settings), artifacts=False).state
    assert st.status == "done" and st.end_reason == "budget:max_llm_usd"
    assert any("LLM 花費" in x for x in st.limitations)


def test_human_decision_appears_in_trace(settings, tmp_path):
    from seo_intel.observe.trace import read_trace

    r, tight = _paused(settings, tmp_path, max_fetches=30)
    _decide(r.ctx.run_dir, True, "同意延長")
    resume_run(r.ctx.run_dir, tight, _llm(settings), artifacts=False)
    records = read_trace(r.ctx.run_dir / "trace.jsonl")
    human = [x for x in records if x.step == "HUMAN_DECISION"]
    assert len(human) == 1 and "延長" in " ".join(human[0].details) and "同意延長" in " ".join(human[0].details)
    assert [x.seq for x in records] == list(range(1, len(records) + 1))
