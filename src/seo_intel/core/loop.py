"""研究迴圈本體：程式碼中的狀態機（見 docs/04-research-loop.md §2.1）。"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from seo_intel.core.context import EpisodeContext, StepOutcome
from seo_intel.core.controller import next_step
from seo_intel.core.episode import save_checkpoint
from seo_intel.core.models import EpisodeState, Step
from seo_intel.core.steps import HANDLERS
from seo_intel.observe.record import Observer, StepRecord, make_record

StepHook = Callable[[StepRecord], bool]   # 回傳 False 表示要求暫停


class ResearchLoop:
    def __init__(self, ctx: EpisodeContext, observers: Sequence[Observer], step_hook: StepHook | None = None,
                 checkpoint_every: int = 50) -> None:
        self.ctx = ctx
        self.observers = list(observers)
        self.step_hook = step_hook
        self.checkpoint_every = checkpoint_every
        self.paused = False

    def run(self) -> EpisodeState:
        st = self.ctx.state
        quit_requested = False
        while True:
            step = next_step(st)
            if step == Step.DONE:
                break
            try:
                outcome = HANDLERS[step.value](self.ctx)
            except Exception as e:
                st.status, st.end_reason = "failed", f"error: {type(e).__name__}: {e}"
                self._record(step, StepOutcome(summary=f"步驟例外：{type(e).__name__}: {e}"))
                self._finish()
                raise
            record = self._record(step, outcome)
            if st.status == "waiting_approval":
                break
            safe_point = step != Step.EXECUTE_TOOL   # 工具結果尚未解析前不能暫停或存檔
            if safe_point and st.seq % self.checkpoint_every == 0:
                save_checkpoint(self.ctx)
            if self.step_hook is not None and not quit_requested and not self.step_hook(record):
                quit_requested = True
            if quit_requested and safe_point:
                self.paused = True
                break
        self._finish()
        return st

    def _record(self, step: Step, outcome: StepOutcome) -> StepRecord:
        st = self.ctx.state
        st.seq += 1
        st.last_step = step
        st.budget.iterations += 1
        self.ctx.ledger.sync_elapsed()
        st.clock_at = self.ctx.clock.now()
        record = make_record(self.ctx, step, outcome)
        for o in self.observers:
            o.emit(record)
        return record

    def _finish(self) -> None:
        save_checkpoint(self.ctx)
        for o in self.observers:
            o.finish(self.ctx.state)
