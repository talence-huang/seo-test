"""預算帳本：硬上限與累進制的階段抓取額度。所有資源消耗都經過這裡記帳。"""

from __future__ import annotations

from typing import Protocol

from seo_intel.config import BudgetConfig
from seo_intel.core.models import PHASE_ORDER, BudgetState, Phase
from seo_intel.llm.types import CallMeta


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class BudgetLedger:
    def __init__(self, cfg: BudgetConfig, state: BudgetState, clock: Clock) -> None:
        self.cfg = cfg
        self.state = state
        self.clock = clock
        self._t0 = clock.now() - state.elapsed_s

    # ---------- 有效上限（設定值 + 人工核准的延長） ----------

    @property
    def max_fetches(self) -> int:
        return self.cfg.max_fetches + int(self.state.extra.get("max_fetches", 0))

    @property
    def max_fetches_per_domain(self) -> int:
        return self.cfg.max_fetches_per_domain + int(self.state.extra.get("max_fetches", 0))

    @property
    def max_llm_usd(self) -> float:
        return self.cfg.max_llm_usd + self.state.extra.get("max_llm_usd", 0.0)

    @property
    def max_wall_time_s(self) -> float:
        return self.cfg.max_wall_time_s + self.state.extra.get("max_wall_time_s", 0.0)

    def cap(self, resource: str) -> float:
        return getattr(self, resource)

    # ---------- 階段額度 ----------

    def fetch_cap(self, phase: Phase) -> int:
        """累進制：到這個階段為止可用的抓取總數。"""
        shares = self.cfg.phases.model_dump()
        upto = sum(shares[p.value] for p in PHASE_ORDER[: PHASE_ORDER.index(phase) + 1])
        return round(self.max_fetches * upto)

    def fetch_room(self, phase: Phase) -> int:
        return max(0, self.fetch_cap(phase) - self.state.fetches)

    # ---------- 檢查 ----------

    def can_fetch(self, domain: str, n: int = 1) -> bool:
        s = self.state
        return (s.fetches + n <= self.max_fetches
                and s.fetches_by_domain.get(domain, 0) + n <= self.max_fetches_per_domain)

    def can_call_api(self, n: int = 1) -> bool:
        return self.state.api_calls + n <= self.cfg.max_api_calls

    def llm_left(self) -> float:
        return self.max_llm_usd - self.state.llm_usd

    def elapsed(self) -> float:
        return self.clock.now() - self._t0

    def exhausted_reason(self) -> str | None:
        """回傳已用盡的硬上限名稱；未用盡則為 None。"""
        s = self.state
        if s.fetches >= self.max_fetches:
            return "max_fetches"
        if s.llm_usd >= self.max_llm_usd:
            return "max_llm_usd"
        if self.elapsed() >= self.max_wall_time_s:
            return "max_wall_time_s"
        if s.actions >= self.cfg.max_actions:
            return "max_actions"
        if s.iterations >= self.cfg.max_actions * 10:
            return "max_iterations"
        return None

    # ---------- 記帳 ----------

    def charge_fetch(self, domain: str) -> None:
        self.state.fetches += 1
        self.state.fetches_by_domain[domain] = self.state.fetches_by_domain.get(domain, 0) + 1

    def charge_api(self) -> None:
        self.state.api_calls += 1

    def charge_llm(self, meta: CallMeta | None) -> None:
        if meta is None:
            return
        self.state.llm_usd = round(self.state.llm_usd + meta.usd, 8)
        self.state.llm_calls += 1
        self.clock.sleep(meta.latency_ms / 1000)  # LLM 耗時也計入環境時間

    def charge_action(self) -> None:
        self.state.actions += 1

    def sync_elapsed(self) -> None:
        self.state.elapsed_s = self.elapsed()

    def snapshot(self, phase: Phase) -> dict:
        s = self.state
        return {
            "phase": phase.value,
            "fetches": s.fetches,
            "fetch_cap": self.max_fetches,
            "phase_cap": self.fetch_cap(phase),
            "api_calls": s.api_calls,
            "api_cap": self.cfg.max_api_calls,
            "llm_usd": round(s.llm_usd, 6),
            "llm_cap": self.max_llm_usd,
            "actions": s.actions,
            "elapsed_s": round(self.elapsed(), 1),
        }
