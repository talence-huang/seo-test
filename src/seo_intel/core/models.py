"""研究迴圈的資料模型。所有步驟的輸入輸出都是型別化的，可序列化為檢查點與 trace。"""

from __future__ import annotations

import json
from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Step(StrEnum):
    DEFINE_GOAL = "DEFINE_GOAL"
    PLAN_INFO_NEEDS = "PLAN_INFO_NEEDS"
    SELECT_ACTION = "SELECT_ACTION"
    EXECUTE_TOOL = "EXECUTE_TOOL"
    PARSE_RESULT = "PARSE_RESULT"
    FORM_HYPOTHESES = "FORM_HYPOTHESES"
    SEEK_EVIDENCE = "SEEK_EVIDENCE"
    REVISE_BELIEFS = "REVISE_BELIEFS"
    DECIDE_NEXT = "DECIDE_NEXT"
    REPORT = "REPORT"
    REFLECT = "REFLECT"
    DONE = "DONE"


class Phase(StrEnum):
    DISCOVERY = "discovery"
    SAMPLING = "sampling"
    TARGETED = "targeted"
    VERIFICATION = "verification"


PHASE_ORDER = (Phase.DISCOVERY, Phase.SAMPLING, Phase.TARGETED, Phase.VERIFICATION)

InfoKind = Literal["rank_change", "control_check", "serp_competitors", "site_inventory", "page_changes"]
DISCOVERY_KINDS = frozenset({"rank_change", "control_check", "serp_competitors", "site_inventory"})
ToolName = Literal["rank_scan", "serp_scan", "discover", "fetch", "history_diff"]


class TaskInput(BaseModel):
    """使用者交給 Agent 的任務（Agent 唯一知道的事；不含任何標準答案）。"""

    scenario: str | None = None
    question: str
    own_site: str
    competitors: list[str]
    today: date
    period_hint: tuple[date, date] | None = None
    site_language: str = "en"

    @property
    def allowed_domains(self) -> list[str]:
        return [self.own_site, *self.competitors]


class ResearchGoal(BaseModel):
    restated_question: str
    goal_type: Literal["ranking_drop_cause", "competitor_content_strategy", "other"]
    topic_keywords: list[str]
    focus_domains: list[str]
    window_start: date
    window_end: date
    success_criteria: list[str]


class InfoNeed(BaseModel):
    id: str
    kind: InfoKind
    question: str
    target_domains: list[str]
    answer_criteria: str
    priority: int = Field(ge=1, le=5)
    must_answer: bool = True
    status: Literal["open", "resolved", "exhausted"] = "open"
    resolution: str | None = None
    added_by: Literal["planner", "system"] = "planner"


class Action(BaseModel):
    tool: ToolName
    args: dict[str, Any]
    need_id: str | None = None
    phase: Phase
    score: float
    breakdown: dict[str, float] = {}
    reason: str

    @property
    def signature(self) -> str:
        return f"{self.tool}:{json.dumps(self.args, sort_keys=True, ensure_ascii=False)}"

    def label(self) -> str:
        target = self.args.get("url") or self.args.get("domain") or self.args.get("site") or ""
        scope = {"topic": "（主題）", "control": "（對照組）"}.get(self.args.get("scope", ""), "")
        return f"{self.tool} {target}{scope}".strip()


class Observation(BaseModel):
    id: str
    seq: int
    kind: str
    summary: str
    data: dict[str, Any]
    need_id: str | None = None
    url: str | None = None


class BudgetState(BaseModel):
    fetches: int = 0
    fetches_by_domain: dict[str, int] = {}
    api_calls: int = 0
    llm_usd: float = 0.0
    llm_calls: int = 0
    actions: int = 0
    iterations: int = 0
    elapsed_s: float = 0.0          # 已累積的環境時間（續跑時延續）


class EpisodeState(BaseModel):
    id: str
    task: TaskInput
    status: Literal["running", "waiting_approval", "done", "failed"] = "running"
    seq: int = 0
    last_step: Step | None = None
    phase: Phase = Phase.DISCOVERY
    goal: ResearchGoal | None = None
    info_needs: list[InfoNeed] = []
    observations: list[Observation] = []
    pending: Action | None = None
    decision: Literal["continue", "pivot", "finish"] | None = None
    end_reason: str | None = None
    done_signatures: list[str] = []
    fetched: dict[str, dict[str, Any]] = {}       # url → 頁面摘要
    fetch_yield: list[int] = []                   # 每次抓取是否產生證據候選（報酬遞減判斷）
    template_changes: dict[str, list[str]] = {}   # 範本 → 已確認有變動的網址
    serp_gainers: list[str] = []
    budget: BudgetState = BudgetState()
    clock_at: float = 0.0
    approval_request: dict[str, Any] | None = None

    def need(self, need_id: str) -> InfoNeed | None:
        return next((n for n in self.info_needs if n.id == need_id), None)

    def needs_of(self, kind: str) -> list[InfoNeed]:
        return [n for n in self.info_needs if n.kind == kind]

    def add_observation(self, kind: str, summary: str, data: dict[str, Any], *,
                        need_id: str | None = None, url: str | None = None) -> Observation:
        obs = Observation(id=f"O-{len(self.observations) + 1}", seq=self.seq + 1, kind=kind, summary=summary,
                          data=data, need_id=need_id, url=url)
        self.observations.append(obs)
        return obs
