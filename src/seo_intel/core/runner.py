"""在 SimWeb 情境中執行一個 episode：CLI 與評測共用同一份流程。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from seo_intel.config import PROJECT_ROOT, Settings
from seo_intel.core.context import EpisodeContext
from seo_intel.core.episode import RUNS_DIR, build_context, load_checkpoint, new_state
from seo_intel.core.loop import ResearchLoop, StepHook
from seo_intel.core.models import EpisodeState, TaskInput
from seo_intel.llm.types import LLMClient
from seo_intel.memory.session import MemorySession
from seo_intel.memory.store import MemoryStore
from seo_intel.observe.record import Observer

DEFAULT_MEMORY = PROJECT_ROOT / "data" / "memory.db"


@dataclass
class RunResult:
    state: EpisodeState
    ctx: EpisodeContext
    sim: object
    paused: bool


def scenario_task(sim) -> TaskInput:
    g = sim.spec.goal
    return TaskInput(scenario=sim.spec.id, question=g.question, own_site=g.own_site, competitors=list(g.competitors),
                     today=sim.spec.now, period_hint=g.window)


def open_memory(path: Path | None, ablate: frozenset[str] = frozenset()) -> MemorySession | None:
    """path 為 None 代表不使用記憶（Day 1）。ablate 可關閉 M1 或 M2（消融實驗）。"""
    return None if path is None else MemorySession(MemoryStore(path), ablate)


RUN_CONFIG = "run.json"


def save_run_config(run_dir: Path, memory: Path | None, ablate: list[str], budget: dict) -> dict:
    """記錄這次執行用的記憶與預算覆寫：續跑時必須沿用，否則預算與學習行為會在中途改變。"""
    cfg = {"memory": {"path": str(memory) if memory else None, "ablate": ablate}, "budget": budget}
    (run_dir / RUN_CONFIG).write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return cfg


def load_run_config(run_dir: Path) -> dict:
    path = run_dir / RUN_CONFIG
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    legacy = run_dir / "memory.json"   # Phase 4 的格式
    mem = json.loads(legacy.read_text(encoding="utf-8")) if legacy.is_file() else {"path": None, "ablate": []}
    return {"memory": mem, "budget": {}}


def apply_run_config(settings: Settings, cfg: dict) -> tuple[Settings, MemorySession | None]:
    budget = cfg.get("budget") or {}
    if budget:
        settings = settings.model_copy(update={"budget": settings.budget.model_copy(update=budget)})
    mem = cfg.get("memory") or {}
    path = Path(mem["path"]) if mem.get("path") else None
    return settings, open_memory(path, frozenset(mem.get("ablate", [])))


def write_artifacts(ctx: EpisodeContext, sim=None) -> dict:
    """sim=None：真實網站，沒有伺服器端的違規判定，只記錄 Agent 自己的請求統計。"""
    from seo_intel.observe.timeline import render_timeline
    from seo_intel.observe.trace import read_trace

    if sim is not None:
        log = sim.server.log
        env = {"violations": log.violations(), "flags": dict(log.flag_counts()), "requests_by_host": dict(log.by_host())}
    else:
        env = {"mode": "real", "requests_by_host": dict(ctx.http.host_counts),
               "status_counts": {str(k): v for k, v in ctx.http.status_counts.items()},
               "note": "真實網站沒有環境端的違規判定；robots.txt、抓取間隔與 crawl-delay 由 Agent 端的 PolicyGate 與工具層強制"}
    (ctx.run_dir / "environment.json").write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
    html = render_timeline(read_trace(ctx.run_dir / "trace.jsonl"), ctx.state.model_dump(mode="json"),
                           ctx.frontier.to_dict() if ctx.frontier else None, env)
    (ctx.run_dir / "timeline.html").write_text(html, encoding="utf-8")
    return env


def run_scenario(scenario: str, settings: Settings, llm_factory: Callable[[Path], LLMClient], *,
                 memory: MemorySession | None = None, run_root: Path = RUNS_DIR,
                 observers: Callable[[EpisodeContext], list[Observer]] | None = None,
                 step_hook: StepHook | None = None, artifacts: bool = True, seed: int | None = None) -> RunResult:
    """seed 覆寫 SimWeb 的世界種子（同樣的因果結構、不同的網址與雜訊），評測用來增加樣本。"""
    from seo_intel.observe.trace import TraceWriter
    from simweb import build

    sim = build(scenario, seed=seed)
    state = new_state(scenario_task(sim), sim.clock)
    state.world_seed = seed
    run_dir = run_root / state.id
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = build_context(state, settings, llm_factory(run_dir), sim.client(), sim.clock, run_dir, memory=memory)
    obs = [TraceWriter(run_dir / "trace.jsonl"), *(observers(ctx) if observers else [])]
    loop = ResearchLoop(ctx, obs, step_hook=step_hook)
    try:
        loop.run()
    finally:
        if artifacts:
            write_artifacts(ctx, sim)
    return RunResult(state=state, ctx=ctx, sim=sim, paused=loop.paused)


def prepare_real(params: dict, settings: Settings, llm_factory: Callable[[Path], LLMClient], *,
                 memory: MemorySession | None = None, run_root: Path = RUNS_DIR, run_cfg: dict | None = None,
                 client=None, clock=None, today: date | None = None, history: Path | None = None) -> EpisodeContext:
    """建立真實網站研究的 episode（還沒開始跑）。params：competitors、keywords、own_site、start、end、
    serp_source、csv_path。run_cfg：記憶與預算設定（與環境設定一起寫進 run.json，續跑時沿用）。"""
    from seo_intel.real import env as real

    client = client or real.real_client()
    clock = clock or real.RealClock()
    env = real.env_config(params, Path(params["csv_path"]) if params.get("csv_path") else None)
    real.check_serp(env)
    task = real.build_task(params, client, today)
    state = new_state(task, clock)
    run_dir = run_root / state.id
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"memory": {"path": None, "ablate": []}, "budget": {}, **(run_cfg or {}), "env": env}
    (run_dir / RUN_CONFIG).write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    tools = real.tools_factory(env, task.today, **({"history": history} if history else {}))
    return build_context(state, settings, llm_factory(run_dir), client, clock, run_dir, memory=memory, tools=tools)


def run_real(params: dict, settings: Settings, llm_factory: Callable[[Path], LLMClient], *,
             observers: Callable[[EpisodeContext], list[Observer]] | None = None,
             step_hook: StepHook | None = None, artifacts: bool = True, **kw) -> RunResult:
    """研究真實網站（評測與測試用；CLI 與網頁介面另外接上主控台與資料庫紀錄）。"""
    from seo_intel.observe.trace import TraceWriter

    ctx = prepare_real(params, settings, llm_factory, **kw)
    obs = [TraceWriter(ctx.run_dir / "trace.jsonl"), *(observers(ctx) if observers else [])]
    loop = ResearchLoop(ctx, obs, step_hook=step_hook)
    try:
        loop.run()
    finally:
        if artifacts:
            write_artifacts(ctx)
    return RunResult(state=ctx.state, ctx=ctx, sim=None, paused=loop.paused)


def resume_context(run_dir: Path, settings: Settings, llm_factory: Callable[[Path], LLMClient],
                   memory: MemorySession | None = None) -> tuple[EpisodeContext, object | None]:
    """從檢查點重建研究環境：模擬世界（同一個種子、接續的虛擬時鐘）或真實網站（run.json 的 env 設定）。"""
    state, frontier = load_checkpoint(run_dir)
    if state.task.mode == "real":
        from seo_intel.real import env as real

        env = load_run_config(run_dir).get("env") or {"mode": "real", "serp_source": "none"}
        return build_context(state, settings, llm_factory(run_dir), real.real_client(), real.RealClock(), run_dir,
                             frontier, memory=memory, tools=real.tools_factory(env, state.task.today)), None
    from simweb import VirtualClock, build

    sim = build(state.task.scenario, clock=VirtualClock(start=state.clock_at), seed=state.world_seed)
    return build_context(state, settings, llm_factory(run_dir), sim.client(), sim.clock, run_dir, frontier,
                         memory=memory), sim


def resume_run(run_dir: Path, settings: Settings, llm_factory: Callable[[Path], LLMClient], *,
               memory: MemorySession | None = None,
               observers: Callable[[EpisodeContext], list[Observer]] | None = None,
               step_hook: StepHook | None = None, artifacts: bool = True, now: datetime | None = None) -> RunResult:
    """從檢查點續跑（手動暫停、等待核准、或程序中斷後）。等待核准時先套用人工決定，尚未決定則丟出 ApprovalPending。"""
    from seo_intel.core import approvals
    from seo_intel.observe.trace import TraceWriter, truncate_trace

    ctx, sim = resume_context(run_dir, settings, llm_factory, memory)
    state = ctx.state
    notes = approvals.resume(ctx, now)
    truncate_trace(run_dir / "trace.jsonl", state.seq)
    obs = [TraceWriter(run_dir / "trace.jsonl"), *(observers(ctx) if observers else [])]
    loop = ResearchLoop(ctx, obs, step_hook=step_hook)
    if notes:
        loop.note(f"人工決定 {notes[0]}", notes[1:])
    try:
        loop.run()
    finally:
        if artifacts:
            write_artifacts(ctx, sim)
    return RunResult(state=state, ctx=ctx, sim=sim, paused=loop.paused)
