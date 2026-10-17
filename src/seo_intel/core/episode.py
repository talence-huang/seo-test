"""Episode 的建立、檢查點存取與執行期物件組裝。"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

import httpx

from seo_intel.config import PROJECT_ROOT, Settings
from seo_intel.core.budget import BudgetLedger, Clock
from seo_intel.core.context import EpisodeContext
from seo_intel.core.frontier import Frontier
from seo_intel.core.models import EpisodeState, TaskInput
from seo_intel.core.policy import PolicyGate
from seo_intel.llm.types import LLMClient
from seo_intel.tools.history import ArchiveClient
from seo_intel.tools.http import HttpToolkit
from seo_intel.tools.rank import RankClient

RUNS_DIR = PROJECT_ROOT / "runs"
CHECKPOINT = "checkpoint.json"


def new_episode_id(scenario: str | None) -> str:
    return f"E-{datetime.now(UTC):%Y%m%d-%H%M%S}-{scenario or 'task'}-{secrets.token_hex(2)}"


def new_state(task: TaskInput, clock: Clock) -> EpisodeState:
    return EpisodeState(id=new_episode_id(task.scenario), task=task, clock_at=clock.now())


def _block_rate(ctx: EpisodeContext, domain: str) -> tuple[float, int]:
    if ctx.frontier is None:
        return 0.0, 0
    stats = ctx.frontier.stats([domain])
    fetched = sum(s.n_fetched for s in stats)
    blocked = sum(s.n_blocked for s in stats)
    return (blocked / fetched if fetched else 0.0), fetched


def build_context(state: EpisodeState, settings: Settings, llm: LLMClient, client: httpx.Client, clock: Clock,
                  run_dir: Path, frontier: Frontier | None = None, memory=None) -> EpisodeContext:
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger = BudgetLedger(settings.budget, state.budget, clock)
    http = HttpToolkit(client, clock, ledger, rate_interval=settings.policy.rate_limit_seconds_per_domain)
    ctx = EpisodeContext(state=state, settings=settings, llm=llm, http=http, ledger=ledger, policy=None,
                         rank=RankClient(http), archive=ArchiveClient(http), clock=clock, run_dir=run_dir,
                         frontier=frontier, memory=memory)
    ctx.policy = PolicyGate(settings.policy, state.task, http, ledger, block_rate=lambda d: _block_rate(ctx, d),
                            state=state, known_url=lambda u: _known_url(ctx, u))
    if frontier is not None and state.goal is not None and memory is not None:   # 續跑：重新接上記憶
        from seo_intel.core.steps import memory_setup

        memory_setup(ctx)
    return ctx


def _known_url(ctx: EpisodeContext, url: str) -> bool:
    """網址必須是程式從 sitemap、連結或 SERP 取得的（在 frontier 中），頁面文字或 LLM 輸出不能直接變成抓取目標。"""
    from seo_intel.core.frontier import normalize_url

    return ctx.frontier is not None and normalize_url(url) in ctx.frontier.entries


def save_checkpoint(ctx: EpisodeContext) -> Path:
    return write_checkpoint(ctx.run_dir, ctx.state, ctx.frontier)


def write_checkpoint(run_dir: Path, state: EpisodeState, frontier: Frontier | None) -> Path:
    """原子寫入（先寫暫存檔再取代），程序在寫入途中被中斷也不會留下損壞的檢查點。"""
    path = run_dir / CHECKPOINT
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "state": state.model_dump(mode="json"),
        "frontier": frontier.to_dict() if frontier else None,
    }, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def load_checkpoint(run_dir: Path) -> tuple[EpisodeState, Frontier | None]:
    data = json.loads((run_dir / CHECKPOINT).read_text(encoding="utf-8"))
    frontier = Frontier.from_dict(data["frontier"]) if data["frontier"] else None
    return EpisodeState.model_validate(data["state"]), frontier
