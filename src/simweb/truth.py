"""由情境中植入的事件自動推導標準答案（單一真相來源，避免手寫答案與事件不一致）。

- 關鍵主張：與研究目標相符、且有排名效應的事件
- 干擾項：沒有任何排名效應的事件（Agent 不應把它們當成原因）
- 混淆因子：全站性波動（Agent 應以對照組識別為非競品因素）
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from simweb.spec import ScenarioSpec


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TruthClaim(_M):
    claim_type: Literal["displacement_cause", "content_strategy"]
    subject_site: str
    template_prefix: str
    cluster: str
    change_types: tuple[str, ...]
    date_window: tuple[date, date]
    direction: Literal["own_loss", "competitor_gain"]
    event_id: str


class Distractor(_M):
    subject_site: str
    template_prefix: str
    cluster: str | None
    change_types: tuple[str, ...]
    event_id: str


class ConfounderTruth(_M):
    id: str
    date: date
    scope: Literal["own_sitewide"] = "own_sitewide"


class Truth(_M):
    scenario_id: str
    goal_type: str
    key_claims: tuple[TruthClaim, ...]
    distractors: tuple[Distractor, ...]
    confounders: tuple[ConfounderTruth, ...]

    def key_evidence_prefixes(self) -> set[tuple[str, str]]:
        """(網域, 範本前綴)：抓到這些頁面即視為取得關鍵證據。"""
        return {(c.subject_site, c.template_prefix) for c in self.key_claims}


def derive_truth(spec: ScenarioSpec) -> Truth:
    goal = spec.goal
    caused = {fx.cause for fx in spec.effects}
    key: list[TruthClaim] = []
    for fx in spec.effects:
        ev = spec.event(fx.cause)
        if fx.cluster != goal.cluster:
            continue
        if goal.type == "ranking_drop_cause" and fx.own_loss <= 0:
            continue
        if goal.type == "competitor_content_strategy" and (fx.competitor_gain <= 0 or ev.site != goal.focus_competitor):
            continue
        t = spec.site(ev.site).templates()[ev.template]
        key.append(TruthClaim(
            claim_type="displacement_cause" if goal.type == "ranking_drop_cause" else "content_strategy",
            subject_site=ev.site,
            template_prefix=t.prefix,
            cluster=fx.cluster,
            change_types=ev.change_types(t),
            date_window=(ev.date, ev.date + timedelta(days=ev.spread_days)),
            direction="own_loss" if goal.type == "ranking_drop_cause" else "competitor_gain",
            event_id=ev.id,
        ))
    if not key:
        raise ValueError(f"情境 {spec.id} 沒有任何符合研究目標的關鍵主張")
    distractors = []
    for ev in spec.events:
        if ev.id in caused:
            continue
        t = spec.site(ev.site).templates()[ev.template]
        distractors.append(Distractor(
            subject_site=ev.site, template_prefix=t.prefix, cluster=ev.cluster,
            change_types=ev.change_types(t), event_id=ev.id,
        ))
    return Truth(
        scenario_id=spec.id,
        goal_type=goal.type,
        key_claims=tuple(key),
        distractors=tuple(distractors),
        confounders=tuple(ConfounderTruth(id=c.id, date=c.date) for c in spec.confounders),
    )
