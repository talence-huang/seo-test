"""Phase 4：學習機制（M1 經驗庫、M2 URL 價值、REFLECT、人工糾偏、經驗治理）。

全部使用 FakeLLM 與 SimWeb，結果確定、免費。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from seo_intel.core.runner import open_memory, run_scenario
from seo_intel.llm.fake import FakeLLM
from seo_intel.llm.fake_planner import _responder, fake_llm
from seo_intel.llm.roles import LessonOut
from seo_intel.memory import priors
from seo_intel.memory.correct import correct
from seo_intel.memory.models import Bias, Lesson
from seo_intel.memory.reflect import maybe_retire
from seo_intel.memory.store import MemoryStore

CORRECTION = "shieldguide.sim 的 /guide/ 只是例行更新，不是原因，應忽略"


def _run(sid: str, settings, tmp: Path, memory: Path | None, ablate: frozenset[str] = frozenset()):
    mem = open_memory(memory, ablate)
    r = run_scenario(sid, settings, lambda run_dir: fake_llm(settings.models), memory=mem, run_root=tmp / "runs",
                     artifacts=False)
    if mem:
        mem.store.close()
    truth = r.sim.truth.key_claims[0]
    c = r.state.claims[0] if r.state.claims else None
    r.correct = c is not None and (c.subject_site, c.template_prefix) == (truth.subject_site, truth.template_prefix)
    r.blocked = r.sim.server.log.flag_counts().get("waf_blocked", 0)
    return r


def _lesson(lid: str, path: str, source: str = "failure", status: str = "active") -> Lesson:
    return Lesson(id=lid, signature=f"avoid_path|*|{path}|test", source=source, scope={"goal_type": "*"}, trigger="t",
                  lesson=f"{path} 應避開", biases=[Bias(type="avoid_path", path=path, reason="waf")], status=status,
                  support=2, provenance={"episodes": ["E-x", "E-y"]})


# ---------- 資料模型與儲存 ----------

@pytest.mark.parametrize("kwargs", [
    {"type": "avoid_path", "path": "reviews"},
    {"type": "hypothesis_prior", "category": "sitewide_factor", "delta": 0.5},
    {"type": "info_need_priority", "kind": "unknown_kind", "priority": 1},
    {"type": "skip_costly_probes", "threshold": 0.5},
])
def test_bias_vocabulary_is_validated(kwargs):
    with pytest.raises(ValidationError):
        Bias(**kwargs)


def test_store_roundtrip_events_and_snapshot(tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    store.save_lesson(_lesson("L-1", "/reviews/"))
    store.event("L-1", "E-1", "created", "測試")
    store.add_reward("E-1", "site", "g|a.sim/x/{slug}", "a.sim/x/{slug}", 3, 1)
    snap = store.snapshot(tmp_path / "snap.db")
    store.close()
    copy = MemoryStore(snap)
    assert copy.lesson("L-1").biases[0].path == "/reviews/"
    assert [e["event"] for e in copy.events("L-1")] == ["created"]
    assert copy.stat("site", "g|a.sim/x/{slug}")[:2] == (3, 1)
    assert copy.next_lesson_id() == "L-2"
    copy.close()


def test_priors_fall_back_from_site_to_path_to_features(tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    g, tkey = "ranking_drop_cause", "new.sim/guide/{slug}"
    assert priors.lookup(store, g, tkey, 500, 2.5) is None             # 沒有經驗：使用 Beta(1,1)
    store.add_reward("E-1", "path", f"{g}|guide/{{slug}}", "old.sim/guide/{slug}", 4, 1)
    info = priors.lookup(store, g, tkey, 500, 2.5)
    assert info.source.startswith("同類路徑") and info.mean == pytest.approx(0.8)  # 新網站：借用其他網站同類路徑的經驗
    store.add_reward("E-2", "site", f"{g}|{tkey}", tkey, 0, 6)
    assert priors.lookup(store, g, tkey, 500, 2.5).source.startswith("同網站範本")   # 同網站的經驗優先
    store.close()


# ---------- 學習曲線 ----------

def test_learning_curve_on_repeated_scenario(settings, tmp_path):
    """同一情境連跑三次：抓取次數遞減、結論不變；成功經驗需要 2 個 episode 支持才生效。"""
    mem = tmp_path / "m.db"
    runs = [_run("s1", settings, tmp_path, mem) for _ in range(3)]
    assert all(r.correct for r in runs)
    fetches = [r.state.budget.fetches for r in runs]
    assert fetches[0] > fetches[1] > fetches[2], fetches
    assert not runs[0].state.lessons_applied and not runs[1].state.lessons_applied   # 第 2 次只有 M2 先驗
    assert "early_hypotheses" in runs[2].state.lessons_applied["L-1"]                  # 訊號已足夠：直接進入假設檢驗
    store = MemoryStore(mem)
    assert store.lesson("L-1").status == "active" and store.lesson("L-1").support == 3
    assert store.lesson("L-2").status == "active"                                      # 跳過付費探針
    store.close()


def test_ablation_disables_learning(settings, tmp_path):
    mem = tmp_path / "m.db"
    off = frozenset({"M1", "M2"})
    a, b = (_run("s1", settings, tmp_path, mem, off) for _ in range(2))
    assert a.state.budget.fetches == b.state.budget.fetches
    store = MemoryStore(mem)
    assert store.lessons() == [] and store.top_stats() == []
    store.close()


def test_failure_lesson_avoids_waf_path(settings, tmp_path):
    """被 WAF 封鎖的路徑：第 1 次成為候選、第 2 次生效、第 3 次直接避開（封鎖歸零）。"""
    mem = tmp_path / "m.db"
    runs = [_run("s5", settings, tmp_path, mem) for _ in range(3)]
    assert all(r.correct for r in runs)
    assert runs[0].blocked > 0 and runs[2].blocked == 0
    store = MemoryStore(mem)
    waf = store.by_signature("avoid_path|*|/reviews/|waf")
    assert waf.status == "active" and waf.source == "failure"
    store.close()
    assert any(p.startswith("avoid:") and "/reviews/" in p for p in runs[2].state.lessons_applied[waf.id])
    assert runs[2].state.budget.fetches < runs[0].state.budget.fetches


# ---------- 經驗治理 ----------

def test_poisoned_lesson_is_retired_by_evidence(settings, tmp_path):
    """錯誤的自動經驗（避開真正的答案路徑）：與證據衝突 2 次後被淘汰，之後恢復正常效率。"""
    mem = tmp_path / "m.db"
    store = MemoryStore(mem)
    store.save_lesson(_lesson("L-1", "/guide/"))
    store.close()
    runs = [_run("s1", settings, tmp_path, mem) for _ in range(3)]
    assert all(r.correct for r in runs)        # 答案有 SERP 等多重訊號，錯誤經驗擋不住證據
    store = MemoryStore(mem)
    poisoned = store.lesson("L-1")
    assert poisoned.status == "retired" and poisoned.conflicts == 2
    assert [e["event"] for e in store.events("L-1")][-1] == "retired"
    store.close()
    assert "L-1" not in runs[2].state.lessons_applied


def test_human_lessons_are_flagged_not_retired():
    lines: list[str] = []
    human = _lesson("L-9", "/guide/", source="human_correction")
    human.conflicts, human.applied = 5, 5

    class Dummy:
        def event(self, *a):
            raise AssertionError("人工經驗不應被 Agent 淘汰")

    maybe_retire(Dummy(), human, "E-1", lines)
    assert human.status == "active" and "建議人工檢視" in lines[0]


# ---------- 人工糾偏 ----------

def test_fake_reflector_translates_correction_into_biases(settings):
    llm = FakeLLM(settings.models, {r: _responder for r in settings.models.roles})
    user = f"<task>{{}}</task><verdict>wrong</verdict><correction>\n{CORRECTION}\n</correction>"
    out = llm.call("reflector", system="", user=user, schema=LessonOut).parsed
    assert [(b.type, b.path, b.domain) for b in out.biases] == [("avoid_path", "/guide/", "shieldguide.sim")]


def test_invalid_reflector_bias_is_dropped(settings, tmp_path):
    def bad(system, user, schema):
        out = _responder(system, user, schema)
        out["biases"] = [{**out["biases"][0], "path": "guide"},                       # 不是 /xxx/ 形式
                         {**out["biases"][0], "type": "hypothesis_prior", "category": "sitewide_factor", "delta": 0.9}]
        return out

    r = _run("s1", settings, tmp_path, tmp_path / "m.db")
    store = MemoryStore(tmp_path / "m.db")
    res = correct(store, r.state, "C1", "wrong", CORRECTION, FakeLLM(settings.models, {x: bad for x in settings.models.roles}))
    assert res.lesson.biases == [] and sum("略過無效偏置" in x for x in res.lines) == 2
    store.close()


def test_human_correction_reverses_learning_and_is_followed(settings, tmp_path):
    mem = tmp_path / "m.db"
    _run("s1", settings, tmp_path, mem)
    second = _run("s1", settings, tmp_path, mem)
    store = MemoryStore(mem)
    key = "ranking_drop_cause|shieldguide.sim/guide/{slug}"
    alpha_before = store.stat("site", key)[0]
    res = correct(store, second.state, "C1", "wrong", CORRECTION, fake_llm(settings.models))
    # M2：撤回該 episode 的命中
    assert store.stat("site", key)[0] == pytest.approx(alpha_before - 4)
    # M1：從被糾正的 episode 學到的支持被撤回
    assert store.lesson("L-1").status == "candidate" and store.lesson("L-1").support == 1
    assert store.lesson("L-2").status == "retired"
    assert store.outcome(second.state.id)["corrected"] == 1 and store.outcome(second.state.id)["success"] == 0
    # 人工經驗直接生效
    human = res.lesson
    assert human.source == "human_correction" and human.status == "active"
    assert [(b.type, b.path, b.domain) for b in human.biases] == [("avoid_path", "/guide/", "shieldguide.sim")]
    assert [r["lesson_id"] for r in store.corrections()] == [human.id]
    with pytest.raises(ValueError, match="已經糾偏過"):
        correct(store, second.state, "C1", "wrong", CORRECTION, None)
    store.close()
    # 下一次研究遵守人工經驗；證據仍指向 /guide/ 時，只標記「建議人工檢視」而不自行淘汰
    third = _run("s1", settings, tmp_path, mem)
    assert third.state.lessons_applied[human.id] == ["avoid:shieldguide.sim/guide/{slug}"]
    assert any("建議人工檢視" in x for x in third.state.reflection["lines"])
    store = MemoryStore(mem)
    assert store.lesson(human.id).status == "active" and store.lesson(human.id).conflicts == 1
    store.close()


def test_correct_verdict_leaves_memory_untouched(settings, tmp_path):
    mem = tmp_path / "m.db"
    r = _run("s1", settings, tmp_path, mem)
    store = MemoryStore(mem)
    before = [dict(x) for x in store.top_stats(100)]
    res = correct(store, r.state, "C1", "correct", "同意", None)
    assert res.lesson is None and [dict(x) for x in store.top_stats(100)] == before
    assert store.outcome(r.state.id)["corrected"] == 0
    store.close()
