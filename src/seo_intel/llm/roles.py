"""LLM 角色的 system prompt 與結構化輸出 schema。

system prompt 固定不變（可被 prompt cache 命中）；任務內容只放在 user 訊息。
日期以字串輸出、由程式解析驗證，避免 schema 格式相容性問題。
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from seo_intel.core.models import ResearchGoal, TaskInput

PLANNER_SYSTEM = """You are the planning module of an SEO competitive-intelligence research agent.
The agent's own program executes every tool call; you only define the research goal and the information needs.
Your outputs are parsed by code and every decision you make is logged for a human to inspect.

Data the program can obtain:
1. Rank data: daily average position per search query for any of the task's sites (like Google Search Console),
   and top-10 SERP snapshots for a query on a given date.
2. The sites themselves: robots.txt, XML sitemaps (URLs + lastmod dates) and page HTML
   (title, headings, word count, structured-data types, FAQ items, tables, publish/modified dates, links).
3. Historical snapshots of pages (like the Wayback Machine), to compare a page before and after a date.

Information need kinds you may request:
- rank_change: how target_domains' rankings on the topic's queries changed within the window, and when it began.
- control_check: whether the own site's rankings on unrelated queries changed in the same period
  (rules out sitewide causes such as algorithm updates or technical problems).
- serp_competitors: which domains gained positions on the topic's queries.
- site_inventory: which page templates target_domains have, how many pages, and which were recently modified.
- page_changes: what changed on target_domains' topic pages during the window (content, structure,
  structured data, new pages).

Rules:
- Nothing is known until measured. Do not assume a cause; plan needs that can discriminate between explanations.
- Use only domains listed in the task.
- Write every free-text field in Traditional Chinese (Taiwan).
- topic_keywords: lowercase short noun phrases in the site language, as they would appear in search queries and URLs.
- Dates use the format YYYY-MM-DD."""


class GoalOut(BaseModel):
    restated_question: str
    goal_type: Literal["ranking_drop_cause", "competitor_content_strategy", "other"]
    topic_keywords: list[str]
    focus_domains: list[str]
    window_start: str
    window_end: str
    success_criteria: list[str]


class NeedOut(BaseModel):
    kind: Literal["rank_change", "control_check", "serp_competitors", "site_inventory", "page_changes"]
    question: str
    target_domains: list[str]
    answer_criteria: str
    priority: int
    must_answer: bool


class PlanOut(BaseModel):
    info_needs: list[NeedOut]
    rationale: str


def task_json(task: TaskInput) -> str:
    return json.dumps({
        "question": task.question,
        "own_site": task.own_site,
        "competitors": task.competitors,
        "today": task.today.isoformat(),
        "analysis_period_hint": [d.isoformat() for d in task.period_hint] if task.period_hint else None,
        "site_language": task.site_language,
    }, ensure_ascii=False, indent=2)


def goal_prompt(task: TaskInput) -> str:
    return f"Define the research goal for this task.\n\n<task>\n{task_json(task)}\n</task>"


def plan_prompt(task: TaskInput, goal: ResearchGoal, lessons: list[str] | None = None) -> str:
    goal_json = goal.model_dump_json(indent=2)
    lesson_block = ""
    if lessons:
        lesson_block = "\n\n<lessons_from_past_episodes>\n" + "\n".join(f"- {x}" for x in lessons) + \
                       "\n</lessons_from_past_episodes>"
    return (f"Plan 3 to 6 information needs for this research goal. Priority 1 is the highest.\n\n"
            f"<task>\n{task_json(task)}\n</task>\n\n<goal>\n{goal_json}\n</goal>{lesson_block}")
