"""評測協定（docs/05 §4.4）：Day 1 → 訓練（含腳本化的人工糾偏）→ Day N → 消融 → 緊縮預算。

- 測試集（s5、s6）**永不寫回記憶**：Day N 的每個 episode 都用凍結快照的副本，跑完即丟。
- 無人值守時的核准政策預設為拒絕（不自動延長預算），並記錄請求次數。
- 腳本化的「人類」只在主張與標準答案不符時糾偏，理由由標準答案產生（例如「這是干擾項，不是原因」）。
- 真實 LLM 模式需要給花費上限；累計 LLM 花費超過上限時中止。
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from seo_intel.config import Settings
from seo_intel.core import approvals
from seo_intel.core.episode import write_checkpoint
from seo_intel.core.runner import RunResult, open_memory, resume_run, run_scenario
from seo_intel.evals.scorer import score
from seo_intel.llm.types import LLMClient
from seo_intel.memory.correct import correct
from seo_intel.memory.store import MemoryStore
from simweb.sim import list_scenarios

SEED_STEP = 1000


class BudgetExceeded(Exception):
    """評測累計的 LLM 花費超過上限。"""


@dataclass
class EvalConfig:
    out_dir: Path
    seeds: int = 3
    tight_budgets: tuple[int, ...] = (20, 30)
    on_approval: str = "reject"
    ablations: tuple[str, ...] = ("M1", "M2")
    max_usd: float | None = None          # 真實 LLM 時必填
    test_offset: int = 0                  # 測試集種子位移：用從沒看過的世界做獨立驗證（避免依測試結果調參造成洩漏）
    progress: Callable[[str], None] = print


@dataclass
class EvalState:
    scores: list[dict] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)
    spent: float = 0.0


def splits() -> dict[str, list[tuple[str, int]]]:
    out: dict[str, list[tuple[str, int]]] = {"train": [], "test": []}
    for spec in list_scenarios():
        out[spec.split].append((spec.id, spec.seed))
    return out


def seeds_of(base: int, n: int) -> list[int]:
    return [base + SEED_STEP * i for i in range(n)]


def _settle(r: RunResult, settings: Settings, llm_factory, policy: str, memory) -> RunResult:
    """無人值守：依政策處理核准請求（預設拒絕），直到 episode 結束。"""
    while r.state.status == "waiting_approval":
        approvals.decide(r.state, None, policy == "approve", f"評測政策：自動{'核准' if policy == 'approve' else '拒絕'}")
        write_checkpoint(r.ctx.run_dir, r.state, r.ctx.frontier)
        r = resume_run(r.ctx.run_dir, settings, llm_factory, memory=memory)
    return r


def run_one(sid: str, seed: int, settings: Settings, llm_factory, ev: EvalState, cfg: EvalConfig, *,
            memory: Path | None = None, frozen: bool = False, ablate: frozenset[str] = frozenset(),
            **labels) -> tuple[RunResult, dict]:
    path = memory
    if memory is not None and frozen:   # 測試集：用凍結快照的副本，跑完即丟
        tmp = cfg.out_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        path = MemoryStore.copy(memory, tmp / f"{sid}-{seed}-{time.time_ns()}.db")
    mem = open_memory(path, ablate)
    try:
        r = run_scenario(sid, settings, llm_factory, memory=mem, run_root=cfg.out_dir / "runs", seed=seed)
        r = _settle(r, settings, llm_factory, cfg.on_approval, mem)
    finally:
        if mem:
            mem.store.close()
        if frozen and path is not None:
            path.unlink(missing_ok=True)
    s = score(r, **labels)
    ev.scores.append(s)
    ev.spent += s["llm_usd"]
    cfg.progress(f"{labels.get('condition', ''):<14} {sid} seed={seed:<5} {'✓' if s['success'] else '✗'} "
                 f"抓取={s['fetches']:<3} 首個關鍵證據={s['first_key_evidence']} LLM=${s['llm_usd']:.4f} "
                 f"經驗={s['lessons_applied']}")
    if cfg.max_usd is not None and ev.spent > cfg.max_usd:
        raise BudgetExceeded(f"評測累計 LLM 花費 US${ev.spent:.2f} 超過上限 US${cfg.max_usd:.2f}")
    return r, s


def scripted_correction(r: RunResult, s: dict, memory: Path, llm: LLMClient | None) -> dict | None:
    """腳本化的人類：主張與標準答案不符時，以標準答案寫出糾偏理由。"""
    st, truth = r.state, r.sim.truth
    if s["success"] or not st.claims:
        return None
    c = st.claims[0]
    d = next((d for d in truth.distractors if (d.subject_site, d.template_prefix) == (c.subject_site, c.template_prefix)),
             None)
    kind = f"干擾項（{'、'.join(d.change_types) or '無排名效應'}）" if d else "與排名變化無關的變動"
    reason = f"{c.subject_site} 的 {c.template_prefix} 是{kind}，不是原因，應忽略"
    store = MemoryStore(memory)
    try:
        res = correct(store, st, c.id, "wrong", reason, llm)
    finally:
        store.close()
    return {"episode": st.id, "scenario": st.task.scenario, "seed": r.sim.spec.seed, "claim": c.id,
            "claim_template": c.template, "reason": reason, "lesson": res.lesson.id if res.lesson else None,
            "lines": res.lines}


def protocol(settings: Settings, llm_factory: Callable[[Path], LLMClient], cfg: EvalConfig,
             reflector: LLMClient | None) -> EvalState:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    ev = EvalState()
    sp = splits()
    test = [(sid, seed) for sid, base in sp["test"] for seed in seeds_of(base + cfg.test_offset, cfg.seeds)]
    say = cfg.progress

    say("== Day 1：空記憶，測試集 ==")
    for sid, seed in test:
        run_one(sid, seed, settings, llm_factory, ev, cfg, condition="day1", split="test")

    say("== 訓練：訓練集（共用記憶；答錯時腳本化的人工糾偏）==")
    train_mem = cfg.out_dir / "memory-train.db"
    train_mem.unlink(missing_ok=True)
    order = 0
    for i in range(cfg.seeds):
        for sid, base in sp["train"]:
            order += 1
            r, s = run_one(sid, seeds_of(base, cfg.seeds)[i], settings, llm_factory, ev, cfg, memory=train_mem,
                           condition="train", split="train", order=order)
            if (fix := scripted_correction(r, s, train_mem, reflector)) is not None:
                ev.corrections.append(fix)
                say(f"   人工糾偏 {fix['claim_template']} → 經驗 {fix['lesson']}")
    day_n = cfg.out_dir / "memory-dayN.db"
    MemoryStore.copy(train_mem, day_n)

    say("== Day N：學習後的記憶（凍結），同一測試集 ==")
    for sid, seed in test:
        run_one(sid, seed, settings, llm_factory, ev, cfg, memory=day_n, frozen=True, condition="dayN", split="test")
    for a in cfg.ablations:
        say(f"== 消融：關閉 {a} ==")
        for sid, seed in test:
            run_one(sid, seed, settings, llm_factory, ev, cfg, memory=day_n, frozen=True, ablate=frozenset({a}),
                    condition=f"dayN-no{a}", split="test")
    for cap in cfg.tight_budgets:
        tight = settings.model_copy(update={"budget": settings.budget.model_copy(update={"max_fetches": cap})})
        say(f"== 緊縮預算：抓取上限 {cap}（核准請求一律{'核准' if cfg.on_approval == 'approve' else '拒絕'}）==")
        for sid, seed in test:
            run_one(sid, seed, tight, llm_factory, ev, cfg, condition=f"day1-{cap}", split="test", budget=cap)
            run_one(sid, seed, tight, llm_factory, ev, cfg, memory=day_n, frozen=True, condition=f"dayN-{cap}",
                    split="test", budget=cap)
    shutil.rmtree(cfg.out_dir / "tmp", ignore_errors=True)
    return ev


def save(ev: EvalState, cfg: EvalConfig, meta: dict) -> Path:
    path = cfg.out_dir / "results.json"
    path.write_text(json.dumps({"meta": meta, "scores": ev.scores, "corrections": ev.corrections},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path
