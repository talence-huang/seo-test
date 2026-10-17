"""網頁介面的背景工作：研究 episode（含逐步模式、核准後續跑）與評測。

與 CLI 共用同一套流程：檢查點、trace.jsonl、intel.db、run.json、timeline.html，兩邊可以交替使用。
每個工作把步驟紀錄與狀態變化依序存成事件，前端以「從第 N 筆之後」輪詢取得。
"""

from __future__ import annotations

import base64
import binascii
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from seo_intel.config import PROJECT_ROOT, get_settings
from seo_intel.core import approvals
from seo_intel.core.episode import RUNS_DIR, load_checkpoint, write_checkpoint
from seo_intel.core.loop import ResearchLoop
from seo_intel.core.runner import DEFAULT_MEMORY, apply_run_config, load_run_config, write_artifacts
from seo_intel.observe.store import DbRecorder
from seo_intel.observe.trace import TraceWriter, truncate_trace
from seo_intel.real.env import data_gaps
from seo_intel.store import Database

DEFAULT_DB = PROJECT_ROOT / "data" / "intel.db"
UPLOADS = PROJECT_ROOT / "data" / "uploads"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def save_uploads(files: list[dict], prefix: str, root: Path | None = None) -> list[Path]:
    """網頁上傳的檔案（{name, data: base64}）存進 data/uploads，續跑時才讀得到。
    以位元組保存：Ahrefs 的「Excel 用」匯出是 UTF-16，交給讀取端判斷編碼。"""
    root = root or UPLOADS
    out = []
    stamp = int(time.time())
    for i, f in enumerate(files):
        try:
            data = base64.b64decode(f.get("data") or "", validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"上傳的檔案 {f.get('name') or i + 1} 無法讀取") from None
        if not data:
            continue
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(f"上傳的檔案 {f.get('name')} 超過 50 MB")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(f.get("name") or "upload.csv").name)[-60:]
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{prefix}-{stamp}-{i + 1}-{safe}"
        path.write_bytes(data)
        out.append(path)
    return out


def make_llm(settings, run_dir: Path):
    """真實 Opus；每次呼叫錄製到 run 目錄（稽核用）。"""
    from seo_intel.llm import AnthropicLLM, RecordingLLM

    return RecordingLLM(AnthropicLLM(settings.models), run_dir / "llm_cassette.jsonl")


@dataclass
class Job:
    id: str
    kind: str                                    # research
    status: str = "starting"                     # starting / running / stepping / paused / waiting_approval / done / failed
    episode_id: str | None = None
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
                "step_mode": self.step_mode, "error": self.error, "result": self.result,
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
        return self._start_real(p)

    def _start_real(self, p: dict) -> Job:
        from seo_intel.core.runner import prepare_real

        mode = p.get("memory") or "default"
        memory = None if mode == "none" else DEFAULT_MEMORY if mode == "default" else             (Path(mode) if Path(mode).is_absolute() else PROJECT_ROOT / mode)
        budget = {k: (int(p[k]) if k == "max_fetches" else float(p[k])) for k in ("max_fetches", "max_llm_usd")
                  if p.get(k) not in (None, "")}
        run_cfg = {"memory": {"path": str(memory) if memory else None, "ablate": sorted(p.get("ablate") or [])},
                   "budget": budget}
        settings, mem = apply_run_config(get_settings(), run_cfg)
        csv_path = None
        if p.get("serp_source") == "csv":   # 網頁上傳的 CSV 先存成檔案，續跑時才讀得到
            if not (p.get("serp_csv") or "").strip():
                raise ValueError("選擇排名 CSV 時請上傳檔案")
            UPLOADS.mkdir(parents=True, exist_ok=True)
            csv_path = UPLOADS / f"serp-{int(time.time())}.csv"
            csv_path.write_text(p["serp_csv"], encoding="utf-8")
        params = {k: p.get(k) for k in ("competitors", "keywords", "own_site", "start", "end", "serp_source")}
        params["csv_path"] = str(csv_path) if csv_path else None
        params["backlink_paths"] = [str(x) for x in save_uploads(p.get("backlinks_csv") or [], "backlinks")]
        with self._lock:
            if (b := self.busy()) is not None:
                raise RuntimeError(f"已有工作在執行（{b.kind} {b.episode_id or b.id}），請等它結束或暫停")
        try:
            ctx = prepare_real(params, settings, lambda run_dir: make_llm(settings, run_dir), memory=mem,
                               run_cfg=run_cfg)
        except Exception:
            if mem:
                mem.store.close()
            raise
        job = self._new("research", step_mode=bool(p.get("step")))
        job.episode_id = ctx.state.id
        self.by_episode[ctx.state.id] = job
        job.emit("start", episode_id=ctx.state.id, task=ctx.state.task.model_dump(mode="json"),
                 budget={"max_fetches": ctx.ledger.max_fetches, "max_llm_usd": ctx.ledger.max_llm_usd},
                 memory=str(mem.store.path) if mem else None, gaps=data_gaps(ctx.state.task))
        threading.Thread(target=self._run, args=(job, ctx, False, []), daemon=True).start()
        return job

    def resume(self, episode_id: str, step: bool = False) -> Job:
        from seo_intel.core.runner import resume_context

        run_dir = RUNS_DIR / episode_id
        state, _ = load_checkpoint(run_dir)
        if state.status in ("done", "failed"):
            raise RuntimeError(f"Episode 已結束（{state.status}）")
        settings, mem = apply_run_config(get_settings(), load_run_config(run_dir))
        prev = self.by_episode.get(episode_id)
        ctx = resume_context(run_dir, settings, lambda d: make_llm(settings, d), mem)
        state = ctx.state
        try:
            notes = approvals.resume(ctx)
        except approvals.ApprovalPending:
            if mem:
                mem.store.close()
            raise
        truncate_trace(run_dir / "trace.jsonl", state.seq)
        job = prev if prev is not None and prev.status not in ("starting", "running", "stepping") else None
        if job is None:
            job = self._new("research", step_mode=step)
            job.episode_id = episode_id
            self.by_episode[episode_id] = job
        else:
            with self._lock:
                if (b := self.busy()) is not None:
                    raise RuntimeError(f"已有工作在執行（{b.kind} {b.episode_id or b.id}）")
                job.status, job.step_mode, job.pause_requested = "starting", step, False
        job.emit("resume", episode_id=episode_id, notes=notes)
        threading.Thread(target=self._run, args=(job, ctx, True, notes), daemon=True).start()
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

    def _run(self, job: Job, ctx, resumed: bool, notes: list[str]) -> None:
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
            for cleanup in (db.close, lambda: write_artifacts(ctx),
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


def episode_summary(run_dir: Path) -> dict:
    """前端用的 episode 摘要：結論、假設、報告、核准、限制、學到什麼。"""
    if not (run_dir / "checkpoint.json").is_file():
        return {}
    from seo_intel.core.claims import action_display, claim_display
    from seo_intel.core.strategy import label

    st, _ = load_checkpoint(run_dir)
    pa = st.pending_approval
    rep = st.report or {}
    return {
        "id": st.id, "label": st.task.label, "question": st.task.question, "keywords": st.task.keywords,
        "task": {"own_site": st.task.own_site, "serp_source": st.task.serp_source,
                 "backlink_source": st.task.backlink_source},
        "status": st.status, "end_reason": st.end_reason, "phase": st.phase, "seq": st.seq,
        "budget": st.budget.model_dump(), "goal": st.goal.model_dump(mode="json") if st.goal else None,
        "claims": [{**c.model_dump(mode="json"), "display": claim_display(c)} for c in st.claims],
        "actions": [{**a.model_dump(mode="json"), "display": action_display(a)} for a in st.actions],
        "hypotheses": [{"id": h.id, "category": h.category, "statement": h.statement, "template": h.template,
                        "practice": label(h.dimension) if h.dimension else None,
                        "status": h.status, "posterior": round(h.posterior, 3)} for h in st.hypotheses],
        "report": {k: rep.get(k) for k in ("headline", "summary", "findings", "caveats", "recommendations",
                                           "citations", "inconclusive_reasons", "data_gaps")},
        "approvals": [a.model_dump(mode="json") for a in st.approvals],
        "pending_approval": pa.model_dump(mode="json") if pa else None,
        "limitations": st.limitations, "security_events": st.security_events,
        "lessons_applied": st.lessons_applied, "reflection": st.reflection,
        "has_timeline": (run_dir / "timeline.html").is_file(), "has_report": (run_dir / "report.md").is_file(),
    }
