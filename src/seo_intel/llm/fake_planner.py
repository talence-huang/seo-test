"""不連網的確定性規劃器，供測試與 `--llm fake` 使用（不花費、結果可重現）。

只依據任務文字推導（與 LLM 拿到的資訊相同），不讀取任何標準答案。
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel

from seo_intel.config import ModelsConfig
from seo_intel.llm.fake import FakeLLM
from seo_intel.llm.roles import (BiasOut, GoalOut, HypothesesOut, LessonOut, PlanOut, ProbePlanOut, ReportOut,
                                 VerifierOut)

_QUOTED = re.compile(r"「([^」]+)」")
_TASK = re.compile(r"<task>\s*(\{.*?\})\s*</task>", re.DOTALL)
_GOAL = re.compile(r"<goal>\s*(\{.*?\})\s*</goal>", re.DOTALL)


def _block(user: str, tag: str):
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", user, re.DOTALL)
    return json.loads(m.group(1)) if m else None


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


def _hypotheses(task: dict, goal: dict, digest: dict) -> dict:
    """天真的假設產生器：SERP 排名網址所屬範本 + 有 2 頁以上變動的範本，各提一個假設。"""
    known = set(digest["known_templates"])
    serp = digest.get("serp") or {}
    findings = {f["template"]: f for f in digest["template_findings"]}
    cands: list[str] = []
    for site, counts in (serp.get("ranking_url_templates") or {}).items():
        cands += [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])]
    cands += [f["template"] for f in sorted(digest["template_findings"], key=lambda f: -f["changed_pages"])
              if f["changed_pages"] >= 2]
    hyps = []
    for tkey in [t for t in dict.fromkeys(cands) if t in known][:3]:
        site = tkey.split("/", 1)[0]
        if site not in task["competitors"]:
            continue
        f = findings.get(tkey, {})
        new = f.get("new_pages", 0) >= max(1, f.get("changed_pages", 0)) / 2 and f.get("changed_pages", 0) > 0
        cited = ([serp["obs"]] if serp else []) + f.get("observations", [])[:3]
        hyps.append({
            "category": "competitor_new_pages" if new else "competitor_content_change",
            "statement": f"{site} 在 {tkey} 的{'新頁面' if new else '內容改版'}造成主題排名變化",
            "subject_site": site, "template": tkey, "mechanism": "競品頁面更完整地回應搜尋意圖",
            "prediction": f"{tkey} 中多數主題頁在期間內變動，且其頁面在主題 SERP 中名次上升",
            "falsification": f"{tkey} 的變動比例與全站平常水準相當，或其頁面未出現在 SERP 上升名單",
            "cited_observations": cited or [digest["observations"][0]["id"]], "prior": 0.35,
        })
    rank_ids = [r["obs"] for r in digest["rank"]]
    if goal["goal_type"] == "ranking_drop_cause" and rank_ids:
        hyps.append({
            "category": "sitewide_factor", "statement": "自家站的下滑來自全站性因素（例如演算法更新）",
            "subject_site": "", "template": "", "mechanism": "全站品質訊號或演算法調整",
            "prediction": "非主題查詢與主題查詢在同期出現相近幅度的下滑",
            "falsification": "非主題查詢的下滑幅度明顯小於主題查詢",
            "cited_observations": rank_ids, "prior": 0.3,
        })
    return HypothesesOut(hypotheses=hyps, reasoning_summary="依 SERP 排名網址與變動範本提出假設（FakeLLM）。").model_dump()


def _probe_plan(hypotheses: list[dict]) -> dict:
    probes = []
    for h in hypotheses:
        if h["category"].startswith("competitor") and 0.15 < h["posterior"] < 0.8:
            probes.append({"hypothesis_id": h["id"], "probe": "template_change_sample", "rationale": "確認範本中變動頁面的比例"})
            probes.append({"hypothesis_id": h["id"], "probe": "competitor_rank_gain", "rationale": "確認競品同期是否進步"})
    return ProbePlanOut(discriminating_question="哪個範本的變動能解釋排名變化？", probes=probes).model_dump()


def _report(claims: list[dict], hypotheses: list[dict]) -> dict:
    findings = [{"claim_id": c["id"], "explanation": f"{c['subject_site']} 的 {c['template_prefix']} 範本在期間內出現 "
                 f"{', '.join(c['change_types'])}，證據 " + " ".join(f"[{e}]" for e in c["evidence_ids"][:3])}
                for c in claims]
    excluded = [{"hypothesis_id": h["id"], "reason": f"信心 {h['posterior']:.2f}，未達成立門檻"}
                for h in hypotheses if h["status"] != "supported"]
    head = f"主要原因：{claims[0]['subject_site']} 的 {claims[0]['template_prefix']} [C1]" if claims else "沒有足夠證據下結論"
    return ReportOut(headline=head, summary="（FakeLLM 依結構化主張產生的摘要）", findings=findings, excluded=excluded,
                     caveats=["此報告由確定性假規劃器產生"], recommendations=[]).model_dump()


_PATH = re.compile(r"(/[a-z0-9][a-z0-9-]*/)")
_DOMAIN = re.compile(r"([a-z0-9-]+\.sim)")
_NOISE_WORDS = ("不要", "避開", "忽略", "雜訊", "噪音", "不是原因", "無關", "自動產生")
_SITEWIDE_WORDS = ("全站", "演算法", "核心更新")


def _bias(type_: str, **kw) -> dict:
    base = {"type": type_, "path": "", "domain": "*", "kind": "", "priority": 0, "category": "", "delta": 0.0,
            "threshold": 0.0, "reason": "human"}
    return BiasOut(**{**base, **kw}).model_dump()


def _reflection(user: str) -> dict:
    """把人工糾偏的文字以關鍵字規則轉成經驗（真正的 LLM 會做語意理解；這裡只求確定、可測）。"""
    m = re.search(r"<correction>\s*(.*?)\s*</correction>", user, re.DOTALL)
    reason = m.group(1) if m else ""
    verdict = re.search(r"<verdict>(\w+)</verdict>", user)
    biases = []
    if any(w in reason for w in _NOISE_WORDS):
        domain = (_DOMAIN.search(reason) or [None, "*"])[1]
        biases += [_bias("avoid_path", path=p, domain=domain) for p in dict.fromkeys(_PATH.findall(reason))]
    if any(w in reason for w in _SITEWIDE_WORDS) or "對照" in reason:
        biases.append(_bias("info_need_priority", kind="control_check", priority=1))
    if any(w in reason for w in _SITEWIDE_WORDS):
        biases.append(_bias("hypothesis_prior", category="sitewide_factor", delta=0.15))
    if "新頁" in reason or "新增頁面" in reason:
        biases.append(_bias("hypothesis_prior", category="competitor_new_pages", delta=0.1))
    return LessonOut(trigger="研究同類型的排名或競品問題時",
                     lesson=f"人工糾偏（{verdict.group(1) if verdict else '—'}）：{reason}", biases=biases).model_dump()


def _responder(system: str, user: str, schema: type[BaseModel]) -> dict:
    if schema is LessonOut:
        return _reflection(user)
    if schema is VerifierOut:
        return VerifierOut(assessment="holds", objections=[], requested_probes=[], summary="未發現可引用觀察支持的反駁（FakeLLM）。").model_dump()
    if schema is ProbePlanOut:
        return _probe_plan(_block(user, "hypotheses"))
    task = json.loads(_TASK.search(user).group(1))
    goal = _block(user, "goal")
    if schema is GoalOut:
        return _goal(task)
    if schema is PlanOut:
        return _plan(task, goal)
    if schema is HypothesesOut:
        return _hypotheses(task, goal, _block(user, "digest"))
    if schema is ReportOut:
        return _report(_block(user, "claims"), _block(user, "hypotheses"))
    raise ValueError(f"FakePlanner 不支援 {schema.__name__}")


def fake_llm(models: ModelsConfig) -> FakeLLM:
    return FakeLLM(models, {role: _responder for role in models.roles})
