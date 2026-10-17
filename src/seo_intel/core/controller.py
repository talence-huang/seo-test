"""狀態轉移：依目前狀態決定下一步（確定性，見 docs/04-research-loop.md §2.2）。"""

from __future__ import annotations

from seo_intel.core.models import EpisodeState, Step

_AFTER_DECIDE = {"continue": Step.SELECT_ACTION, "form": Step.FORM_HYPOTHESES, "seek": Step.SEEK_EVIDENCE,
                 "revise": Step.REVISE_BELIEFS, "finish": Step.REPORT}


def next_step(state: EpisodeState) -> Step:
    if state.status != "running":
        return Step.DONE
    if state.goal is None:
        return Step.DEFINE_GOAL
    if not state.info_needs:
        return Step.PLAN_INFO_NEEDS
    last = state.last_step
    if last in (Step.DEFINE_GOAL, Step.PLAN_INFO_NEEDS, None):
        return Step.SELECT_ACTION
    if last == Step.SELECT_ACTION:
        return Step.EXECUTE_TOOL if state.pending is not None else Step.DECIDE_NEXT
    if last == Step.EXECUTE_TOOL:
        return Step.PARSE_RESULT
    if last in (Step.PARSE_RESULT, Step.REVISE_BELIEFS):
        return Step.DECIDE_NEXT
    if last == Step.FORM_HYPOTHESES:
        return Step.SEEK_EVIDENCE
    if last == Step.SEEK_EVIDENCE:
        return Step.SELECT_ACTION if state.evidence_queue else Step.REVISE_BELIEFS
    if last == Step.DECIDE_NEXT:
        return _AFTER_DECIDE[state.decision or "continue"]
    return Step.DONE   # REPORT 之後（Phase 4 加入 REFLECT）
