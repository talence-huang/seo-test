"""M1 經驗的資料模型。偏置（bias）使用固定詞彙，程式可直接套用並驗證。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator

BiasType = Literal["avoid_path", "sampling_focus", "info_need_priority", "hypothesis_prior", "skip_costly_probes"]
NEED_KINDS = {"rank_change", "control_check", "serp_competitors", "site_inventory", "page_changes"}
CATEGORIES = {"competitor_content_change", "competitor_new_pages", "sitewide_factor", "own_site_change", "other"}


class Bias(BaseModel):
    """程式可直接套用的偏置。

    - avoid_path：避開路徑（path 例如 /reviews/；domain 為 * 代表任何網域）
    - sampling_focus：抽樣時先處理有訊號的範本（SERP 排名網址所屬、期間內更新爆發）
    - info_need_priority：調整某類資訊需求的優先序（1 最高），缺少時補上
    - hypothesis_prior：調整某類假設的先驗（±0.2 以內）
    - skip_costly_probes：免費證據已讓主要假設 ≥ threshold、其餘 ≤ 0.2 時，不做付費探針
    """

    type: BiasType
    path: str | None = None
    domain: str = "*"
    kind: str | None = None
    priority: int | None = None
    category: str | None = None
    delta: float | None = None
    threshold: float | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _check(self) -> Bias:
        t = self.type
        if t == "avoid_path" and not (self.path and self.path.startswith("/") and self.path.endswith("/") and len(self.path) > 2):
            raise ValueError("avoid_path 需要形如 /xxx/ 的 path")
        if t == "info_need_priority" and (self.kind not in NEED_KINDS or not self.priority or not 1 <= self.priority <= 5):
            raise ValueError("info_need_priority 需要有效的 kind 與 1–5 的 priority")
        if t == "hypothesis_prior" and (self.category not in CATEGORIES or self.delta is None or abs(self.delta) > 0.2):
            raise ValueError("hypothesis_prior 需要有效的 category 與 ±0.2 以內的 delta")
        if t == "skip_costly_probes" and not (self.threshold and 0.85 <= self.threshold <= 0.99):
            raise ValueError("skip_costly_probes 的 threshold 需介於 0.85–0.99")
        return self

    def describe(self) -> str:
        return {
            "avoid_path": f"避開 {self.domain if self.domain != '*' else '任何網站'} 的 {self.path}（{self.reason}）",
            "sampling_focus": "抽樣時先處理有訊號的範本",
            "info_need_priority": f"資訊需求 {self.kind} 優先序設為 {self.priority}",
            "hypothesis_prior": f"{self.category} 類假設先驗 {self.delta:+.2f}" if self.delta is not None else "",
            "skip_costly_probes": f"免費證據已達 {self.threshold} 時跳過付費探針",
        }[self.type]


class Lesson(BaseModel):
    id: str
    signature: str
    source: Literal["success", "failure", "human_correction"]
    scope: dict
    trigger: str
    lesson: str
    biases: list[Bias]
    status: Literal["candidate", "active", "retired"]
    support: int = 1
    applied: int = 0
    applied_success: int = 0
    conflicts: int = 0
    provenance: dict = {}
    retired_reason: str | None = None

    def matches(self, goal_type: str) -> bool:
        g = self.scope.get("goal_type", "*")
        return g in ("*", goal_type)

    @property
    def success_rate(self) -> float | None:
        return self.applied_success / self.applied if self.applied else None
