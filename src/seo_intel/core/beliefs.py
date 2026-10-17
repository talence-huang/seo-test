"""信念更新：可證偽性檢查、log-odds 信心更新、狀態判定、證據矛盾偵測。數字一律由程式計算。"""

from __future__ import annotations

import math

from seo_intel.core import strategy
from seo_intel.core.models import STRATEGY_CATEGORIES, EpisodeState, EvidenceItem, Hypothesis

WEIGHT = {"weak": 0.3, "moderate": 0.8, "strong": 1.5}
PRIOR_RANGE = (0.1, 0.6)        # 任何假設的先驗都不超過 0.6：必須靠證據升上去
SUPPORTED_AT = 0.8
REFUTED_AT = 0.2
VERIFY_AT = 0.7                 # 跨過此門檻時觸發紅隊驗證
MIN_TEXT = 8
COMPETITOR_CATEGORIES = frozenset({"competitor_content_change", "competitor_new_pages"})


def logit(p: float) -> float:
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def posterior(prior: float, evidence: list[EvidenceItem]) -> float:
    x = logit(prior) + sum((1 if e.direction == "for" else -1) * WEIGHT[e.strength] for e in evidence)
    return round(min(0.999, max(0.001, sigmoid(x))), 4)


def clamp_prior(p: float) -> float:
    return round(min(PRIOR_RANGE[1], max(PRIOR_RANGE[0], p)), 3)


def validate(raw: dict, state: EpisodeState, known_templates: set[str]) -> tuple[dict, str | None]:
    """回傳（正規化後的欄位, 拒收理由）。拒收的假設仍保留在紀錄中，供事後檢視。"""
    reasons = []
    for field in ("statement", "prediction", "falsification"):
        if len((raw.get(field) or "").strip()) < MIN_TEXT:
            reasons.append(f"{field} 空白或過短")
    cited = [o for o in raw.get("cited", []) if state.observation(o)]
    dropped = len(raw.get("cited", [])) - len(cited)
    if not cited:
        reasons.append("沒有引用任何存在的觀察")
    category = raw["category"]
    site = raw.get("subject_site") or None
    template = raw.get("template") or None
    if category in COMPETITOR_CATEGORIES:
        if site not in state.task.competitors:
            reasons.append(f"subject_site {site} 不是任務中的競品")
        if template not in known_templates:
            reasons.append(f"範本 {template} 不在已觀察到的範本中")
        elif site and not template.startswith(f"{site}/"):
            reasons.append("範本不屬於 subject_site")
    if category == "own_site_change" and site != state.task.own_site:
        reasons.append("own_site_change 的 subject_site 必須是自家站")
    dimension = None
    notes = []
    if category in STRATEGY_CATEGORIES:   # 策略假設：維度必須是固定詞彙，且要有對應的輪廓資料可以檢驗
        side = STRATEGY_CATEGORIES[category]
        dimension = raw.get("dimension") or None
        dim = strategy.DIMENSIONS.get(dimension)
        if dim is None or dim.side != side:
            reasons.append(f"dimension {dimension} 不是 {side} 的策略維度")
        if site not in state.task.competitors:
            reasons.append(f"subject_site {site} 不是任務中的競品")
        kind = "onpage_profile" if side == "onpage" else "link_profile"
        if not any(o.kind == kind and o.data.get("site") == site for o in state.observations):
            reasons.append(f"沒有 {site} 的{'排名頁輪廓' if side == 'onpage' else '反向連結資料'}，無法檢驗")
        if template and template not in known_templates:
            notes.append(f"範本 {template} 不在已觀察到的範本中，已移除")
            template = None
    fields = {**raw, "cited": cited, "subject_site": site, "template": template, "dimension": dimension,
              "prior": clamp_prior(float(raw.get("prior", 0.3)))}
    if dropped:
        notes.insert(0, f"已移除 {dropped} 個不存在的觀察引用")
    note = "；".join(notes) or None
    return fields | {"note": note}, ("；".join(reasons) or None)


def has_conflict(evidence: list[EvidenceItem]) -> bool:
    strong = {e.direction for e in evidence if e.strength == "strong"}
    return strong == {"for", "against"}


def status_of(h: Hypothesis, evidence: list[EvidenceItem]) -> str:
    if h.status == "rejected":
        return "rejected"
    if has_conflict(evidence):
        return "testing"
    if h.posterior >= SUPPORTED_AT and h.verified:
        return "supported"
    if h.posterior <= REFUTED_AT:
        return "refuted"
    return "testing"


def refresh(state: EpisodeState) -> list[str]:
    """重新計算所有假設的信心與狀態，回傳可讀的變化說明。"""
    lines = []
    for h in state.hypotheses:
        if h.status == "rejected":
            continue
        ev = state.evidence_for(h.id)
        before, before_status = h.posterior, h.status
        h.posterior = posterior(h.prior, ev)
        h.status = status_of(h, ev)
        if (h.posterior, h.status) != (before, before_status):
            conflict = "（強證據互相矛盾，維持測試中）" if has_conflict(ev) else ""
            lines.append(f"{h.id} 信心 {before:.2f} → {h.posterior:.2f}，狀態 {before_status} → {h.status}{conflict}")
    return lines


def record_point(state: EpisodeState, h: Hypothesis, evidence_id: str | None) -> None:
    """記錄套用某筆證據後的信心，讓信心曲線能顯示「每筆證據造成多少變化」。"""
    state.belief_history.append({"seq": state.seq + 1, "hypothesis_id": h.id, "evidence_id": evidence_id,
                                 "posterior": posterior(h.prior, state.evidence_for(h.id))})
