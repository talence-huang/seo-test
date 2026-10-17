"""優先行動：把成立的策略結論和自家網站的差距，換算成有順序的行動清單（確定性，LLM 只負責說明）。

分數 = 影響 × 差距 ÷ 難度
- 影響：結論信心 × 這個做法在競品有排名頁面上的普及率（網站層級的做法以 1 計）
- 差距：自家在同一維度落後多少（core/strategy.gap；0 = 已經做到、1 = 完全沒做）；沒有自家資料時以 0.5 計並標示
- 難度：維度固定的實作難度（1 低、2 中、3 高）
自家已經做到（差距 ≤ 0.1）的維度列為「維持」，不建議投入。
"""

from __future__ import annotations

from seo_intel.core.models import Claim, PriorityAction
from seo_intel.core.strategy import DIMENSIONS, gap

MAINTAIN_BELOW = 0.1
UNKNOWN_GAP = 0.5
# 內容改版結論（content_strategy）的變動類型 → 策略維度
CHANGE_TO_DIMENSION = {"content_expansion": "content_depth", "new_sections": "sections", "faq_content": "faq",
                       "schema:FAQPage": "schema_faq", "schema:HowTo": "schema_howto", "schema:Product": "schema_product",
                       "schema:BreadcrumbList": "schema_breadcrumb", "comparison_table": "comparison_table",
                       "new_template_launch": "new_content", "bulk_pages": "new_content"}


def claim_dimensions(c: Claim) -> list[str]:
    if c.dimension:
        return [c.dimension]
    return list(dict.fromkeys(CHANGE_TO_DIMENSION[t] for t in c.change_types if t in CHANGE_TO_DIMENSION))


def rank(claims: list[Claim], own: dict[str, float] | None, own_observation: str | None = None) -> list[PriorityAction]:
    """claims：通過驗證的結論；own：自家網站在各維度的值（strategy.profile 或反向連結指標；None＝沒有自家資料）。"""
    best: dict[str, PriorityAction] = {}
    for c in claims:
        if not c.validated:
            continue
        for key in claim_dimensions(c):
            dim = DIMENSIONS[key]
            comp = c.measure.get("competitor") if c.dimension == key else None
            prevalence = comp if (dim.kind == "bool" and comp is not None) else 1.0
            impact = round(c.confidence * prevalence, 3)
            own_value = own.get(key) if own is not None else None
            g = gap(key, comp, own_value) if comp is not None else None
            if own is not None and g is None and own_value is not None and dim.kind == "bool":
                g = round(max(0.0, 1.0 - own_value), 3)    # 沒有競品普及率（例如來自內容改版結論）：以自家是否做到計
            status = "unknown" if g is None else ("maintain" if g <= MAINTAIN_BELOW else "do")
            score = round(impact * (UNKNOWN_GAP if g is None else g) / dim.effort, 3)
            a = PriorityAction(id="", dimension=key, label=dim.label, side=dim.side, action=dim.action, status=status,
                               score=score, impact=impact, gap=g, effort=dim.effort, competitor=comp, own=own_value,
                               claim_id=c.id, observation_ids=c.observation_ids[:6] + ([own_observation] if own_observation else []))
            if key not in best or a.score > best[key].score:
                best[key] = a
    order = sorted(best.values(), key=lambda a: ({"do": 0, "unknown": 1, "maintain": 2}[a.status], -a.score))
    for i, a in enumerate(order, 1):
        a.id = f"A{i}"
    return order
