"""Episode 執行期的共用物件，以及每一步回傳給觀察者的紀錄格式。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from seo_intel.config import Settings
from seo_intel.core.budget import BudgetLedger, Clock
from seo_intel.core.frontier import Frontier
from seo_intel.core.models import EpisodeState
from seo_intel.core.policy import PolicyGate
from seo_intel.llm.types import CallMeta, LLMClient
from seo_intel.tools.history import ArchiveClient
from seo_intel.tools.http import HttpToolkit
from seo_intel.tools.rank import RankClient


@dataclass
class EpisodeContext:
    state: EpisodeState
    settings: Settings
    llm: LLMClient
    http: HttpToolkit
    ledger: BudgetLedger
    policy: PolicyGate
    rank: RankClient
    archive: ArchiveClient
    clock: Clock
    run_dir: Path
    frontier: Frontier | None = None
    scratch: dict[str, Any] = field(default_factory=dict)   # 不寫入檢查點的暫存（例如原始工具結果）


@dataclass
class StepOutcome:
    """一個步驟對外可觀察的紀錄：做了什麼、為什麼、看到了什麼、花了多少。"""

    summary: str
    details: list[str] = field(default_factory=list)
    decision: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    llm: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    lessons_applied: list[str] = field(default_factory=list)


def llm_record(meta: CallMeta | None, output: Any = None, error: str | None = None) -> dict[str, Any]:
    if meta is None:
        return {"error": error}
    d = asdict(meta)
    d["usage"] = meta.usage.model_dump()
    d["output"] = output
    d["error"] = error
    return d
