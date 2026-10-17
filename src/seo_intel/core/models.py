"""研究迴圈的資料模型。所有步驟的輸入輸出都是型別化的，可序列化為檢查點與 trace。"""

from __future__ import annotations

import json
from datetime import date
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

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
    HUMAN_DECISION = "HUMAN_DECISION"   # 只出現在 trace：人工核准 / 拒絕的套用紀錄（不是狀態機的步驟）


class Phase(StrEnum):
    DISCOVERY = "discovery"
    SAMPLING = "sampling"
    TARGETED = "targeted"
    VERIFICATION = "verification"


PHASE_ORDER = (Phase.DISCOVERY, Phase.SAMPLING, Phase.TARGETED, Phase.VERIFICATION)

InfoKind = Literal["rank_change", "control_check", "serp_competitors", "site_inventory", "page_changes",
                   "ranking_pages", "backlink_profile", "own_site_profile"]
DISCOVERY_KINDS = frozenset({"rank_change", "control_check", "serp_competitors", "site_inventory", "backlink_profile"})
ToolName = Literal["serp_scan", "discover", "fetch", "history_diff", "backlinks"]


class TaskInput(BaseModel):
    """使用者交給 Agent 的研究任務：競品網站、關鍵字與研究期間（真實網站沒有排名歷史與標準答案）。

    研究要回答三個問題：競品的 On-page 策略、Off-page 策略，以及我們最值得優先採取的行動。
    各問題需要的資料（排名、反向連結、自家網站）都是選填；缺少時 Agent 照樣研究，但會在報告寫明哪個問題答不了。
    """

    question: str
    own_site: str = ""
    competitors: list[str]
    today: date
    period_hint: tuple[date, date] | None = None
    site_language: str = "en"
    keywords: list[str] = []                       # 使用者指定的關鍵字
    serp_source: Literal["none", "serpapi", "csv"] = "none"   # 目前 SERP 從哪裡來
    backlink_source: Literal["none", "csv"] = "none"         # 反向連結（Off-page）從哪裡來
    backlink_domains: list[str] = []               # 任務網域中，反向連結資料裡有連結指向的網域

    @property
    def allowed_domains(self) -> list[str]:
        return [d for d in (self.own_site, *self.competitors) if d]

    @property
    def label(self) -> str:
        return f"{'、'.join(self.competitors)}｜{'、'.join(self.keywords)}"


class ResearchGoal(BaseModel):
    restated_question: str
    goal_type: Literal["competitive_strategy", "ranking_drop_cause", "competitor_content_strategy", "other"]
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
    probe_id: str | None = None     # 為了某個證據探針而執行的行動
    lessons: list[str] = []         # 影響此行動選擇的經驗 ID
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


HypothesisCategory = Literal["onpage_strategy", "offpage_strategy", "competitor_content_change",
                             "competitor_new_pages", "sitewide_factor", "own_site_change", "other"]
STRATEGY_CATEGORIES = {"onpage_strategy": "onpage", "offpage_strategy": "offpage"}
Strength = Literal["weak", "moderate", "strong"]


class Hypothesis(BaseModel):
    id: str
    round: int
    category: HypothesisCategory
    statement: str
    subject_site: str | None = None
    template: str | None = None          # 範本鍵，例如 example.com/guide/{slug}
    dimension: str | None = None         # 策略假設的維度（core/strategy.py 的固定詞彙）
    mechanism: str = ""
    prediction: str
    falsification: str
    cited: list[str] = []                # 形成時引用的觀察 ID
    prior: float
    posterior: float
    status: Literal["proposed", "testing", "supported", "refuted", "inconclusive", "rejected"] = "proposed"
    verified: bool = False               # 是否已經過紅隊驗證
    note: str | None = None


class EvidenceItem(BaseModel):
    id: str
    hypothesis_id: str
    probe: str
    direction: Literal["for", "against"]
    strength: Strength
    observation_ids: list[str]
    note: str
    source: Literal["probe", "verifier"] = "probe"
    seq: int = 0


class Probe(BaseModel):
    id: str
    kind: str
    hypothesis_ids: list[str]
    status: Literal["queued", "executed", "evaluated", "skipped"] = "queued"
    pending: list[str] = []              # 尚未執行完的行動簽章
    observation_ids: list[str] = []
    sample: list[str] = []               # 比例估計用的隨機樣本（不受 frontier 排序的選樣偏誤影響）
    requested_by: Literal["system", "planner", "verifier"] = "system"
    rationale: str = ""
    note: str | None = None


class Claim(BaseModel):
    id: str
    claim_type: Literal["onpage_strategy", "offpage_strategy", "displacement_cause", "content_strategy"]
    subject_site: str
    template: str
    template_prefix: str
    cluster: str
    change_types: list[str]
    date_window: tuple[str, str] | None
    direction: Literal["own_loss", "competitor_gain"]
    confidence: float
    hypothesis_id: str
    evidence_ids: list[str]
    observation_ids: list[str]
    validated: bool = False
    dimension: str | None = None              # 策略結論的維度
    measure: dict[str, Any] = {}              # 程式量測的數字（競品排名頁的普及率、對比、連結指標…）


class PriorityAction(BaseModel):
    """優先行動：由程式依「影響 × 差距 ÷ 難度」排序（core/priority.py），LLM 只寫說明。"""

    id: str
    dimension: str
    label: str
    side: Literal["onpage", "offpage"]
    action: str
    status: Literal["do", "maintain", "unknown"]   # 要做 / 自家已做到 / 沒有自家資料、無法比較
    score: float
    impact: float
    gap: float | None
    effort: int
    competitor: float | None
    own: float | None
    claim_id: str
    observation_ids: list[str] = []


class Approval(BaseModel):
    """一次人工核准請求（見 docs/06 §2.4）。決定寫回檢查點，續跑時套用。"""

    id: str
    rule: str
    reason: str
    kind: Literal["action", "budget"]
    action: Action | None = None              # kind=action：被擋下的行動
    extend: dict[str, float] = {}             # kind=budget：核准後延長的預算，例如 {"max_fetches": 12}
    estimate: dict[str, Any] = {}             # 預估成本與用途，給人判斷
    status: Literal["pending", "approved", "rejected", "expired", "used"] = "pending"
    note: str | None = None
    created_at: str
    expires_at: str
    decided_at: str | None = None

    @property
    def scope_key(self) -> str:
        """人工拒絕後，同一範圍不再詢問：預算以資源為範圍，行動以網域為範圍。"""
        if self.kind == "budget":
            return next(iter(self.extend), "")
        return action_scope(self.action) if self.action else ""


def action_scope(action: Action) -> str:
    if "url" in action.args:
        return urlsplit(action.args["url"]).hostname or ""
    return action.args.get("domain") or action.args.get("site") or ""


class BudgetState(BaseModel):
    fetches: int = 0
    fetches_by_domain: dict[str, int] = {}
    api_calls: int = 0
    llm_usd: float = 0.0
    llm_calls: int = 0
    actions: int = 0
    iterations: int = 0
    elapsed_s: float = 0.0          # 已累積的環境時間（續跑時延續）
    extra: dict[str, float] = {}    # 經人工核准延長的上限（max_fetches、max_llm_usd、max_wall_time_s）


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
    decision: Literal["continue", "form", "seek", "revise", "finish"] | None = None
    hypotheses: list[Hypothesis] = []
    evidence: list[EvidenceItem] = []
    probes: list[Probe] = []
    evidence_queue: list[Action] = []
    hypothesis_round: int = 0
    seek_rounds: int = 0                 # 本輪假設已尋找證據的次數
    belief_history: list[dict[str, Any]] = []
    claims: list[Claim] = []
    actions: list[PriorityAction] = []
    report: dict[str, Any] | None = None
    lessons_applied: dict[str, list[str]] = {}    # 經驗 ID → 被套用的位置
    reflection: dict[str, Any] | None = None
    end_reason: str | None = None
    done_signatures: list[str] = []
    failed_attempts: dict[str, int] = {}          # 行動簽章 → 暫時性失敗次數（行動層重試）
    fetched: dict[str, dict[str, Any]] = {}       # url → 頁面摘要
    fetch_yield: list[int] = []                   # 每次抓取是否產生證據候選（報酬遞減判斷）
    template_changes: dict[str, list[str]] = {}   # 範本 → 已確認有變動的網址
    serp_gainers: list[str] = []
    budget: BudgetState = BudgetState()
    clock_at: float = 0.0
    approvals: list[Approval] = []                 # 所有核准請求（含已決定的，供稽核）
    grants: list[dict[str, str]] = []              # 已核准、尚未使用的一次性授權 {approval, rule, signature}
    denials: list[dict[str, str]] = []             # 人工拒絕的範圍 {approval, rule, scope}：之後直接拒絕、不再詢問
    limitations: list[str] = []                    # 因拒絕或預算而未完成的工作，寫入報告「限制」
    security_events: list[str] = []               # 疑似注入、來源不明網址等安全事件

    @property
    def pending_approval(self) -> Approval | None:
        return next((a for a in reversed(self.approvals) if a.status == "pending"), None)

    def need(self, need_id: str) -> InfoNeed | None:
        return next((n for n in self.info_needs if n.id == need_id), None)

    def needs_of(self, kind: str) -> list[InfoNeed]:
        return [n for n in self.info_needs if n.kind == kind]

    def hypothesis(self, hid: str) -> Hypothesis | None:
        return next((h for h in self.hypotheses if h.id == hid), None)

    def probe(self, pid: str) -> Probe | None:
        return next((p for p in self.probes if p.id == pid), None)

    def observation(self, oid: str) -> Observation | None:
        return next((o for o in self.observations if o.id == oid), None)

    def evidence_for(self, hid: str) -> list[EvidenceItem]:
        return [e for e in self.evidence if e.hypothesis_id == hid]

    def add_observation(self, kind: str, summary: str, data: dict[str, Any], *,
                        need_id: str | None = None, url: str | None = None) -> Observation:
        obs = Observation(id=f"O-{len(self.observations) + 1}", seq=self.seq + 1, kind=kind, summary=summary,
                          data=data, need_id=need_id, url=url)
        self.observations.append(obs)
        return obs
