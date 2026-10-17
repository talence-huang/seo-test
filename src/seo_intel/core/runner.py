"""在 SimWeb 情境中執行一個 episode：CLI 與評測共用同一份流程。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from seo_intel.config import PROJECT_ROOT, Settings
from seo_intel.core.context import EpisodeContext
from seo_intel.core.episode import RUNS_DIR, build_context, new_state
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


def write_artifacts(ctx: EpisodeContext, sim) -> dict:
    from seo_intel.observe.timeline import render_timeline
    from seo_intel.observe.trace import read_trace

    log = sim.server.log
    env = {"violations": log.violations(), "flags": dict(log.flag_counts()), "requests_by_host": dict(log.by_host())}
    (ctx.run_dir / "environment.json").write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
    html = render_timeline(read_trace(ctx.run_dir / "trace.jsonl"), ctx.state.model_dump(mode="json"),
                           ctx.frontier.to_dict() if ctx.frontier else None, env)
    (ctx.run_dir / "timeline.html").write_text(html, encoding="utf-8")
    return env


def run_scenario(scenario: str, settings: Settings, llm_factory: Callable[[Path], LLMClient], *,
                 memory: MemorySession | None = None, run_root: Path = RUNS_DIR,
                 observers: Callable[[EpisodeContext], list[Observer]] | None = None,
                 step_hook: StepHook | None = None, artifacts: bool = True) -> RunResult:
    from seo_intel.observe.trace import TraceWriter
    from simweb import build

    sim = build(scenario)
    state = new_state(scenario_task(sim), sim.clock)
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
