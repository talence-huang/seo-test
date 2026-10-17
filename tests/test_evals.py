"""Phase 6：評測工具本身的測試（評分器、協定、報告）。FakeLLM、免費。"""

import hashlib

from seo_intel.core.runner import run_scenario
from seo_intel.evals.report import bootstrap, render
from seo_intel.evals.runner import EvalConfig, protocol, seeds_of, splits
from seo_intel.evals.scorer import score
from seo_intel.llm.fake_planner import fake_llm


def test_splits_and_seeds_follow_scenario_files():
    sp = splits()
    assert [s for s, _ in sp["train"]] == ["s1", "s2", "s3", "s4"] and [s for s, _ in sp["test"]] == ["s5", "s6"]
    assert seeds_of(505, 3) == [505, 1505, 2505]


def test_scorer_reads_truth_and_server_log_not_agent_claims(settings, tmp_path):
    r = run_scenario("s1", settings, lambda d: fake_llm(settings.models), run_root=tmp_path)
    s = score(r)
    assert s["success"] and s["f1"] == 1.0 and s["violations"] == 0
    assert 0 < s["first_key_evidence"] <= s["fetches"] and 0 <= s["waste_rate"] < 1
    r.state.claims[0] = r.state.claims[0].model_copy(update={"subject_site": "carewise.sim", "template_prefix": "/tag/"})
    wrong = score(r)                                    # 主張與標準答案不符 → 失敗，不論 Agent 多有信心
    assert not wrong["success"] and wrong["precision"] == 0.0 and wrong["recall"] == 0.0


def test_bootstrap_interval_contains_mean():
    m, lo, hi = bootstrap([-12.0, -8.0, -10.0, -9.0])
    assert lo <= m <= hi and hi < 0


def test_protocol_never_writes_test_episodes_into_memory(settings, tmp_path):
    cfg = EvalConfig(out_dir=tmp_path, seeds=1, ablations=(), tight_budgets=(), progress=lambda m: None)
    ev = protocol(settings, lambda d: fake_llm(settings.models), cfg, fake_llm(settings.models))
    conds = [s["condition"] for s in ev.scores]
    assert conds == ["day1"] * 2 + ["train"] * 4 + ["dayN"] * 2
    day_n = tmp_path / "memory-dayN.db"
    digest = hashlib.sha256(day_n.read_bytes()).hexdigest()
    cfg2 = EvalConfig(out_dir=tmp_path / "again", seeds=1, ablations=(), tight_budgets=(), progress=lambda m: None)
    cfg2.out_dir.mkdir()
    from seo_intel.evals.runner import EvalState, run_one

    run_one("s5", 505, settings, lambda d: fake_llm(settings.models), EvalState(), cfg2, memory=day_n, frozen=True,
            condition="dayN")
    assert hashlib.sha256(day_n.read_bytes()).hexdigest() == digest      # 凍結快照沒有被測試 episode 改寫
    md = render({"meta": {"llm": "fake", "seeds": 1, "on_approval": "reject", "test": ["s5", "s6"],
                          "train": ["s1", "s2", "s3", "s4"]}, "scores": ev.scores, "corrections": ev.corrections},
                tmp_path)
    for heading in ("## 1. 測試集彙總", "## 2. Day 1 → Day N 配對比較", "## 4. 訓練過程", "## 5. Day N 的記憶"):
        assert heading in md
