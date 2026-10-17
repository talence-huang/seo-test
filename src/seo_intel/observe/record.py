"""每一步的標準紀錄：即時主控台、trace.jsonl、資料庫、timeline.html 都由它產生。"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from seo_intel.core.context import EpisodeContext, StepOutcome
from seo_intel.core.models import EpisodeState, Step


class StepRecord(BaseModel):
    episode_id: str
    seq: int
    step: str
    phase: str
    t: float
    summary: str
    details: list[str] = []
    decision: dict[str, Any] = {}
    reason: str | None = None
    candidates: list[dict[str, Any]] = []
    llm: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    lessons_applied: list[str] = []
    budget: dict[str, Any] = {}


class Observer(Protocol):
    def emit(self, record: StepRecord) -> None: ...
    def finish(self, state: EpisodeState) -> None: ...


def make_record(ctx: EpisodeContext, step: Step, outcome: StepOutcome) -> StepRecord:
    st = ctx.state
    wanted = set(outcome.observations)
    obs = [{"id": o.id, "kind": o.kind, "summary": o.summary, "url": o.url}
           for o in st.observations if o.id in wanted]
    return StepRecord(
        episode_id=st.id, seq=st.seq, step=step.value, phase=st.phase.value,
        t=round(ctx.ledger.elapsed(), 2), summary=outcome.summary, details=outcome.details,
        decision=outcome.decision, reason=outcome.reason, candidates=outcome.candidates, llm=outcome.llm,
        tool_calls=outcome.tool_calls, observations=obs, lessons_applied=outcome.lessons_applied,
        budget=ctx.ledger.snapshot(st.phase),
    )
