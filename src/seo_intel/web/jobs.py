"""網頁介面的背景工作：研究 episode（含逐步模式、核准後續跑）與評測。

與 CLI 共用同一套流程：檢查點、trace.jsonl、intel.db、run.json、timeline.html，兩邊可以交替使用。
每個工作把步驟紀錄與狀態變化依序存成事件，前端以「從第 N 筆之後」輪詢取得。
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from seo_intel.config import PROJECT_ROOT, get_settings
from seo_intel.core import approvals
from seo_intel.core.episode import RUNS_DIR, build_context, load_checkpoint, new_state, write_checkpoint
from seo_intel.core.loop import ResearchLoop
from seo_intel.core.runner import (DEFAULT_MEMORY, apply_run_config, load_run_config, save_run_config, scenario_task,
                                   write_artifacts)
from seo_intel.observe.store import DbRecorder
from seo_intel.observe.trace import TraceWriter, truncate_trace
from seo_intel.store import Database

DEFAULT_DB = PROJECT_ROOT / "data" / "intel.db"
REPORTS_DIR = PROJECT_ROOT / "evals" / "reports"


def make_llm(mode: str, settings, run_dir: Path):
    from seo_intel.llm import AnthropicLLM, RecordingLLM
    from seo_intel.llm.fake_planner import fake_llm

    if mode == "fake":
        return fake_llm(settings.models)
    if mode == "live":
        return RecordingLLM(AnthropicLLM(settings.models), run_dir / "llm_cassette.jsonl")
    raise ValueError(f"未知的 LLM 模式：{mode}")


@dataclass
class Job:
    id: str
    kind: str                                    # research / eval
    status: str = "starting"                     # starting / running / stepping / paused / waiting_approval / done / failed
    episode_id: str | None = None
    llm: str = "fake"
    step_mode: bool = False
    events: list[dict] = field(default_factory=list)
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    go: threading.Event = field(default_factory=threading.Event)
    pause_requested: bool = False

    def emit(self, kind: str, **data) -> None:
        with self.lock:
            self.events.append({"i": len(self.events), "type": kind, "t": time.time(), **data})

    def since(self, i: int) -> list[dict]:
        with self.lock:
            return self.events[i:]

    def info(self) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "episode_id": self.episode_id,
                "llm": self.llm, "step_mode": self.step_mode, "error": self.error, "result": self.result,
                "n_events": len(self.events)}


class JobObserver:
    def __init__(self, job: Job) -> None:
        self.job = job

    def emit(self, record) -> None:
        self.job.emit("step", record=record.model_dump(mode="json"))

    def finish(self, state) -> None:
        self.job.emit("state", status=state.status, end_reason=state.end_reason)


class JobManager:
    """同一時間只允許一個工作在執行：研究共用同一份記憶與 SQLite，避免互相干擾。"""

    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        self.db_path = db_path
        self.jobs: dict[str, Job] = {}
        self.by_episode: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._n = 0

    def busy(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.status in ("starting", "running", "stepping")), None)

    def _new(self, kind: str, **kw) -> Job:
        with self._lock:
            if (b := self.busy()) is not None:
                raise RuntimeError(f"已有工作在執行（{b.kind} {b.episode_id or b.id}），請等它結束或暫停")
            self._n += 1
            job = Job(id=f"J{self._n}", kind=kind, **kw)
            self.jobs[job.id] = job
            return job

    # ---------- 研究 ----------

    def start_research(self, p: dict) -> Job:
        from simweb import build

        settings0 = get_settings()
        seed = int(p["seed"]) if p.get("seed") not in (None, "") else None
        sim = build(p["scenario"], seed=seed)
        state = new_state(scenario_task(sim), sim.clock)
        state.world_seed = seed
        run_dir = RUNS_DIR / state.id
        run_dir.mkdir(parents=True, exist_ok=True)
        mode = p.get("memory") or "default"
        if mode in ("default", "none"):
            memory = DEFAULT_MEMORY if mode == "default" else None
        else:   # 自訂記憶檔：相對路徑以專案根目錄為準
            memory = Path(mode) if Path(mode).is_absolute() else PROJECT_ROOT / mode
        budget = {k: (int(p[k]) if k == "max_fetches" else float(p[k])) for k in ("max_fetches", "max_llm_usd")
                  if p.get(k) not in (None, "")}
        cfg = save_run_config(run_dir, Path(memory) if memory else None, sorted(p.get("ablate") or []), budget)
        settings, mem = apply_run_config(settings0, cfg)
        job = self._new("research", llm=p.get("llm", "fake"), step_mode=bool(p.get("step")))
        job.episode_id = state.id
        self.by_episode[state.id] = job
        ctx = build_context(state, settings, make_llm(job.llm, settings, run_dir), sim.client(), sim.clock, run_dir,
                            memory=mem)
        job.emit("start", episode_id=state.id, task=state.task.model_dump(mode="json"),
                 budget={"max_fetches": ctx.ledger.max_fetches, "max_llm_usd": ctx.ledger.max_llm_usd},
                 memory=str(mem.store.path) if mem else None)
        threading.Thread(target=self._run, args=(job, ctx, sim, False, []), daemon=True).start()
        return job

    def resume(self, episode_id: str, llm: str | None = None, step: bool = False) -> Job:
        from simweb import VirtualClock, build

        run_dir = RUNS_DIR / episode_id
        state, frontier = load_checkpoint(run_dir)
        if state.status in ("done", "failed"):
            raise RuntimeError(f"Episode 已結束（{state.status}）")
        settings, mem = apply_run_config(get_settings(), load_run_config(run_dir))
        sim = build(state.task.scenario, clock=VirtualClock(start=state.clock_at), seed=state.world_seed)
        prev = self.by_episode.get(episode_id)
        mode = llm or (prev.llm if prev else "fake")
        ctx = build_context(state, settings, make_llm(mode, settings, run_dir), sim.client(), sim.clock, run_dir,
                            frontier, memory=mem)
        try:
            notes = approvals.resume(ctx)
        except approvals.ApprovalPending:
            if mem:
                mem.store.close()
            raise
        truncate_trace(run_dir / "trace.jsonl", state.seq)
        job = prev if prev is not None and prev.status not in ("starting", "running", "stepping") else None
        if job is None:
            job = self._new("research", llm=mode, step_mode=step)
            job.episode_id = episode_id
            self.by_episode[episode_id] = job
        else:
            with self._lock:
                if (b := self.busy()) is not None:
                    raise RuntimeError(f"已有工作在執行（{b.kind} {b.episode_id or b.id}）")
                job.status, job.step_mode, job.pause_requested = "starting", step, False
        job.emit("resume", episode_id=episode_id, notes=notes)
        threading.Thread(target=self._run, args=(job, ctx, sim, True, notes), daemon=True).start()
        return job

    def _hook(self, job: Job):
        def hook(record) -> bool:
            if job.pause_requested:
                return False
            quiet = record.step == "DECIDE_NEXT" and record.decision.get("decision") == "continue"
            if not job.step_mode or quiet:
                return True
            job.status = "stepping"
            job.emit("await", seq=record.seq)
            job.go.wait()
            job.go.clear()
            job.status = "running"
            return not job.pause_requested
        return hook

    def _run(self, job: Job, ctx, sim, resumed: bool, notes: list[str]) -> None:
        st = ctx.state
        db = Database(self.db_path)
        final = "failed"
        try:
            recorder = DbRecorder(db, st)
            if resumed:
                recorder.truncate(st.seq)
            loop = ResearchLoop(ctx, [TraceWriter(ctx.run_dir / "trace.jsonl"), recorder, JobObserver(job)],
                                step_hook=self._hook(job))
            job.status = "running"
            if notes:
                loop.note(f"人工決定 {notes[0]}", notes[1:])
            loop.run()
            final = ("waiting_approval" if st.status == "waiting_approval" else
                     "paused" if st.status == "running" else st.status)
        except Exception as e:   # noqa: BLE001 — 錯誤要回報給前端，而不是讓執行緒默默結束
            final, job.error = "failed", f"{type(e).__name__}: {e}"
            job.emit("error", message=job.error, trace=traceback.format_exc()[-2000:])
        finally:
            for cleanup in (db.close, lambda: write_artifacts(ctx, sim),
                            lambda: ctx.memory.store.close() if ctx.memory else None):
                try:
                    cleanup()
                except Exception as e:   # noqa: BLE001 — 收尾失敗也不能吞掉「結束」事件
                    job.emit("error", message=f"收尾失敗：{type(e).__name__}: {e}")
            # 狀態與「結束」事件一起更新：前端看到工作不再執行時，一定也拿得到結束事件
            with job.lock:
                job.status = final
                job.events.append({"i": len(job.events), "type": "finished", "t": time.time(), "status": final,
                                   "summary": episode_summary(ctx.run_dir)})

    def control(self, job_id: str, action: str) -> Job:
        job = self.jobs[job_id]
        if action == "next":
            job.go.set()
        elif action == "run":
            job.step_mode = False
            job.go.set()
        elif action == "pause":
            job.pause_requested = True
            job.go.set()
        else:
            raise ValueError(f"未知的控制指令：{action}")
        return job

    def decide(self, episode_id: str, approve: bool, note: str | None, resume: bool = True) -> dict:
        run_dir = RUNS_DIR / episode_id
        state, frontier = load_checkpoint(run_dir)
        a = approvals.decide(state, None, approve, note)
        write_checkpoint(run_dir, state, frontier)
        if self.db_path.is_file():
            from seo_intel.observe.store import record_approvals

            db = Database(self.db_path)
            if db.query("SELECT id FROM episodes WHERE id = ?", (state.id,)):
                record_approvals(db, state)
            db.close()
        out = {"approval": a.model_dump(mode="json")}
        if resume and a.status in ("approved", "rejected", "expired"):
            prev = self.by_episode.get(episode_id)
            out["job"] = self.resume(episode_id, step=prev.step_mode if prev else False).info()
        return out

    # ---------- 評測 ----------

    def start_eval(self, p: dict) -> Job:
        from datetime import datetime

        from seo_intel.evals.report import render
        from seo_intel.evals.runner import BudgetExceeded, EvalConfig, protocol, save, splits

        llm = p.get("llm", "fake")
        if llm == "live" and not p.get("max_usd"):
            raise ValueError("真實 LLM 評測必須設定花費上限（max_usd）")
        quick = bool(p.get("quick"))
        out = REPORTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{llm}"
        job = self._new("eval", llm=llm)
        settings = get_settings()
        cfg = EvalConfig(out_dir=out, seeds=int(p.get("seeds") or 3), test_offset=int(p.get("test_offset") or 0),
                         max_usd=float(p["max_usd"]) if p.get("max_usd") else None,
                         ablations=() if quick else ("M1", "M2"), tight_budgets=() if quick else (20, 30),
                         progress=lambda m: job.emit("log", message=m))

        def work() -> None:
            import json

            job.status = "running"
            try:
                reflector = make_llm(llm, settings, out)
                ev = protocol(settings, lambda run_dir: make_llm(llm, settings, run_dir), cfg, reflector)
                sp = splits()
                meta = {"name": out.name, "llm": llm, "seeds": cfg.seeds, "test_offset": cfg.test_offset,
                        "on_approval": cfg.on_approval, "test": [x for x, _ in sp["test"]],
                        "train": [x for x, _ in sp["train"]]}
                path = save(ev, cfg, meta)
                (out / "report.md").write_text(render(json.loads(path.read_text(encoding="utf-8")), out),
                                               encoding="utf-8")
                job.result = {"report": out.name}
                job.status = "done"
            except BudgetExceeded as e:
                job.status, job.error = "failed", str(e)
            except Exception as e:   # noqa: BLE001
                job.status, job.error = "failed", f"{type(e).__name__}: {e}"
                job.emit("error", message=job.error, trace=traceback.format_exc()[-2000:])
            job.emit("finished", status=job.status, report=job.result.get("report"), error=job.error)

        threading.Thread(target=work, daemon=True).start()
        return job


def episode_summary(run_dir: Path) -> dict:
    """前端用的 episode 摘要：結論、假設、報告、核准、限制、學到什麼。"""
    if not (run_dir / "checkpoint.json").is_file():
        return {}
    st, _ = load_checkpoint(run_dir)
    pa = st.pending_approval
    rep = st.report or {}
    return {
        "id": st.id, "scenario": st.task.scenario, "seed": st.world_seed, "question": st.task.question,
        "status": st.status, "end_reason": st.end_reason, "phase": st.phase, "seq": st.seq,
        "budget": st.budget.model_dump(), "goal": st.goal.model_dump(mode="json") if st.goal else None,
        "claims": [c.model_dump(mode="json") for c in st.claims],
        "hypotheses": [{"id": h.id, "category": h.category, "statement": h.statement, "template": h.template,
                        "status": h.status, "posterior": round(h.posterior, 3)} for h in st.hypotheses],
        "report": {k: rep.get(k) for k in ("headline", "summary", "findings", "caveats", "recommendations",
                                           "citations")},
        "approvals": [a.model_dump(mode="json") for a in st.approvals],
        "pending_approval": pa.model_dump(mode="json") if pa else None,
        "limitations": st.limitations, "security_events": st.security_events,
        "lessons_applied": st.lessons_applied, "reflection": st.reflection,
        "has_timeline": (run_dir / "timeline.html").is_file(), "has_report": (run_dir / "report.md").is_file(),
    }
