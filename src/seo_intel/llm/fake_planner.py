"""不連網的確定性規劃器，供測試與 `--llm fake` 使用（不花費、結果可重現）。

只依據任務文字推導（與 LLM 拿到的資訊相同），不讀取任何標準答案。
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel

from seo_intel.config import ModelsConfig
from seo_intel.llm.fake import FakeLLM
from seo_intel.llm.roles import GoalOut, PlanOut

_QUOTED = re.compile(r"「([^」]+)」")
_TASK = re.compile(r"<task>\s*(\{.*?\})\s*</task>", re.DOTALL)
_GOAL = re.compile(r"<goal>\s*(\{.*?\})\s*</goal>", re.DOTALL)


def _goal(task: dict) -> dict:
    q = task["question"]
    goal_type = "competitor_content_strategy" if "策略" in q or "做了什麼" in q else "ranking_drop_cause"
    mentioned = [d for d in task["competitors"] if d in q]
    period = task.get("analysis_period_hint") or [task["today"], task["today"]]
    return GoalOut(
        restated_question=q,
        goal_type=goal_type,
        topic_keywords=[m.lower() for m in _QUOTED.findall(q)] or [q.lower()],
        focus_domains=mentioned or task["competitors"],
        window_start=period[0],
        window_end=period[1],
        success_criteria=["找出排名變化的起始時間與幅度", "找出變動的競品頁面範本與變動類型"],
    ).model_dump()


def _plan(task: dict, goal: dict) -> dict:
    own, comps, focus = task["own_site"], task["competitors"], goal["focus_domains"]
    if goal["goal_type"] == "ranking_drop_cause":
        needs = [
            ("rank_change", [own], 1, "自家站在主題查詢上的排名何時、下滑多少？"),
            ("control_check", [own], 1, "自家站其他主題是否同時下滑？"),
            ("serp_competitors", comps, 2, "哪些競品在主題查詢上取代了自家站？"),
            ("site_inventory", comps, 2, "競品有哪些頁面範本，哪些近期有更新？"),
            ("page_changes", comps, 3, "競品的主題頁面在期間內改了什麼？"),
        ]
    else:
        needs = [
            ("rank_change", focus, 1, "焦點競品在主題查詢上的排名何時開始上升？"),
            ("serp_competitors", comps, 2, "誰在主題查詢上獲得最多名次？"),
            ("site_inventory", focus, 1, "焦點競品有哪些頁面範本，哪些近期新增或更新？"),
            ("page_changes", focus, 2, "焦點競品的主題頁面在期間內新增或改了什麼？"),
        ]
    return PlanOut(
        info_needs=[{"kind": k, "question": q, "target_domains": d, "answer_criteria": "以量測資料回答",
                     "priority": p, "must_answer": True} for k, d, p, q in needs],
        rationale="依目標類型套用預設的資訊需求模板（FakeLLM）。",
    ).model_dump()


def _responder(system: str, user: str, schema: type[BaseModel]) -> dict:
    task = json.loads(_TASK.search(user).group(1))
    if schema is GoalOut:
        return _goal(task)
    if schema is PlanOut:
        return _plan(task, json.loads(_GOAL.search(user).group(1)))
    raise ValueError(f"FakePlanner 不支援 {schema.__name__}")


def fake_llm(models: ModelsConfig) -> FakeLLM:
    return FakeLLM(models, {role: _responder for role in models.roles})
