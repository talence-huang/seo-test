"""人工糾偏：分析師判定某個主張錯誤（或部分錯誤）時，撤回 Agent 從該 episode 學到的東西，並把理由轉成經驗。

1. M2：撤回該 episode 給主張範本的「命中」（wrong 全數、partial 一半改記為未命中）
2. M1：該 episode 套用過的經驗成功次數 −1（可能因此觸發淘汰）；從該 episode 萃取的成功經驗撤回支持
3. 把人工理由交給 reflector 轉成經驗：偏置只能用固定詞彙、由程式驗證；直接生效，來源標記 human_correction，
   之後 Agent 不會自行淘汰（只會在與證據衝突時標記「建議人工檢視」）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from seo_intel.core.models import EpisodeState
from seo_intel.llm.roles import REFLECTOR_SYSTEM, BiasOut, LessonOut, reflector_prompt
from seo_intel.llm.types import CallMeta, LLMClient, LLMError
from seo_intel.memory.models import Bias, Lesson
from seo_intel.memory.reflect import ACTIVATE_AT, maybe_retire
from seo_intel.memory.store import MemoryStore

WEIGHT = {"wrong": 1.0, "partial": 0.5}


@dataclass
class CorrectionResult:
    lines: list[str] = field(default_factory=list)
    lesson: Lesson | None = None
    calls: list[CallMeta] = field(default_factory=list)


def to_bias(b: BiasOut) -> Bias:
    """LLM 輸出的哨兵值（空字串、0）轉回選填欄位，再交給 Bias 驗證。"""
    return Bias(type=b.type, path=b.path or None, domain=b.domain or "*", kind=b.kind or None,
                priority=b.priority or None, category=b.category or None,
                delta=b.delta if b.type == "hypothesis_prior" else None,
                threshold=b.threshold if b.type == "skip_costly_probes" else None, reason=b.reason or "human")


def _context(state: EpisodeState, claim_template: str) -> dict:
    return {
        "claim_template": claim_template,
        "hypotheses": [{"id": h.id, "category": h.category, "statement": h.statement, "status": h.status,
                        "posterior": round(h.posterior, 3)} for h in state.hypotheses],
        "fetched_templates": sorted({i["template"] for i in state.fetched.values() if i.get("template")})[:40],
    }


def _translate(state: EpisodeState, claim, verdict: str, reason: str, llm: LLMClient,
               res: CorrectionResult) -> tuple[str, str, list[Bias]]:
    task = {"question": state.task.question, "own_site": state.task.own_site, "competitors": state.task.competitors}
    try:
        out = llm.call("reflector", system=REFLECTOR_SYSTEM, schema=LessonOut,
                       user=reflector_prompt(task, claim.model_dump(mode="json"), verdict, reason,
                                             _context(state, claim.template)))
    except LLMError as e:
        if e.meta:
            res.calls.append(e.meta)
        res.lines.append(f"reflector 失敗（{e}）：只保存理由原文，不產生偏置")
        return "研究同類型問題時", f"人工糾偏（{verdict}）：{reason}", []
    res.calls.append(out.meta)
    biases = []
    for b in out.parsed.biases:
        try:
            biases.append(to_bias(b))
        except ValidationError as e:
            res.lines.append(f"略過無效偏置 {b.type}（{e.errors()[0]['msg']}）")
    return out.parsed.trigger, out.parsed.lesson, biases


def correct(store: MemoryStore, state: EpisodeState, claim_id: str, verdict: str, reason: str,
            llm: LLMClient | None) -> CorrectionResult:
    res = CorrectionResult()
    claim = next((c for c in state.claims if c.id == claim_id), None)
    if claim is None:
        raise ValueError(f"episode {state.id} 沒有主張 {claim_id}（可用的主張：{', '.join(c.id for c in state.claims) or '無'}）")
    eid = state.id
    if any(r["claim_id"] == claim_id and r["verdict"] != "correct" for r in store.corrections(eid)) and verdict != "correct":
        raise ValueError(f"{eid}/{claim_id} 已經糾偏過；記憶不會被重複撤回")
    if verdict == "correct":
        store.add_correction(eid, claim_id, verdict, reason, None)
        res.lines.append(f"已記錄：人工確認 {claim_id} 正確（記憶不變）")
        return res

    weight = WEIGHT[verdict]
    # M2：撤回命中
    reversed_ = store.reverse_rewards(eid, claim.template, weight)
    if reversed_:
        amt = max(a for _, _, a in reversed_)
        res.lines.append(f"M2：撤回 {claim.template} 在此 episode 的命中 {amt:g} 次（改記為未命中，"
                         f"影響 {len(reversed_)} 個鍵：{', '.join(f'{s}:{k}' for s, k, _ in reversed_)}）")
    else:
        res.lines.append("M2：此 episode 沒有寫入 URL 價值（未啟用 M2 或當時判定未成功），無需撤回")

    # M1：套用過的經驗扣回成功次數；萃取自此 episode 的成功經驗撤回支持
    outcome = store.outcome(eid)
    if outcome is not None:
        was_success = bool(outcome["success"])
        store.mark_corrected(eid, failed=verdict == "wrong")
        if was_success and verdict == "wrong":
            for lid in json.loads(outcome["lessons_json"]):
                lesson = store.lesson(lid)
                if lesson is None:
                    continue
                lesson.applied_success = max(0, lesson.applied_success - 1)
                store.event(lid, eid, "corrected", f"人工判定 {claim_id} 錯誤：成功次數 −1")
                res.lines.append(f"經驗 {lid}：此 episode 改判失敗，成功 {lesson.applied_success}/{lesson.applied}")
                maybe_retire(store, lesson, eid, res.lines)
                store.save_lesson(lesson)
    if verdict == "wrong":
        for lesson in store.lessons():
            episodes = lesson.provenance.get("episodes", [])
            if lesson.source != "success" or eid not in episodes:
                continue
            episodes.remove(eid)
            lesson.support = len(episodes)
            store.event(lesson.id, eid, "support_withdrawn", f"支持來自被糾正的 episode，支持數 → {lesson.support}")
            note = f"經驗 {lesson.id}：撤回此 episode 的支持（支持數 {lesson.support}）"
            if lesson.support == 0 and lesson.status != "retired":
                lesson.status, lesson.retired_reason = "retired", "唯一的支持來自被人工糾正的 episode"
                store.event(lesson.id, eid, "retired", lesson.retired_reason)
                note += " → 淘汰"
            elif lesson.status == "active" and lesson.support < ACTIVATE_AT:
                lesson.status = "candidate"
                store.event(lesson.id, eid, "demoted", f"支持數 {lesson.support} < {ACTIVATE_AT}")
                note += " → 退回候選"
            res.lines.append(note)
            store.save_lesson(lesson)

    # 人工理由 → 經驗
    if llm is not None:
        trigger, text, biases = _translate(state, claim, verdict, reason, llm, res)
    else:
        trigger, text, biases = "研究同類型問題時", f"人工糾偏（{verdict}）：{reason}", []
    lesson = Lesson(id=store.next_lesson_id(), signature=f"human|{eid}|{claim_id}", source="human_correction",
                    scope={"goal_type": state.goal.goal_type if state.goal else "*"}, trigger=trigger, lesson=text,
                    biases=biases, status="active",
                    provenance={"episodes": [eid], "claim": claim_id, "verdict": verdict, "reason": reason})
    store.save_lesson(lesson)
    store.event(lesson.id, eid, "created", f"人工糾偏 {claim_id}（{verdict}）")
    store.add_correction(eid, claim_id, verdict, reason, lesson.id)
    res.lesson = lesson
    res.lines.append(f"新增人工經驗 {lesson.id}（直接生效）：{lesson.lesson}")
    res.lines += [f"  偏置：{b.describe()}" for b in biases] or ["  偏置：無（只在規劃與假設時提供文字提示）"]
    return res
