"""研究迴圈各步驟的處理器。每個處理器更新 EpisodeState，並回傳可觀察的 StepOutcome。

LLM 只出現在 DEFINE_GOAL 與 PLAN_INFO_NEEDS（Phase 3 起加入假設、驗證與報告）；
選擇行動、解析結果、判斷下一步都是確定性程式，並把理由寫進紀錄。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date, timedelta
from urllib.parse import urlsplit

from seo_intel.core import approvals, beliefs, priority, probes, profiles, strategy
from seo_intel.core.actions import COMPONENT_LABELS, compute_phase, focus_satisfied, generate
from seo_intel.core.claims import (confounder_note, derive_claims, inconclusive_reasons, render_markdown,
                                   validate_citations)
from seo_intel.core.context import EpisodeContext, StepOutcome, llm_record
from seo_intel.core.digest import hypotheses_digest, observation_digest
from seo_intel.core.frontier import Frontier, normalize_url, template_key
from seo_intel.core.policy import EST_DISCOVER_FETCHES
from seo_intel.core.models import (Action, EvidenceItem, Hypothesis, InfoNeed, Phase, ResearchGoal, TaskInput,
                                   action_scope)
from seo_intel.llm.roles import (
    EVIDENCE_SYSTEM,
    HYPOTHESIZER_SYSTEM,
    PLANNER_SYSTEM,
    REPORTER_SYSTEM,
    VERIFIER_SYSTEM,
    GoalOut,
    HypothesesOut,
    PlanOut,
    ProbePlanOut,
    ReportOut,
    VerifierOut,
    evidence_prompt,
    goal_prompt,
    hypotheses_prompt,
    plan_prompt,
    report_prompt,
    verifier_prompt,
)
from seo_intel.llm.types import LLMError
from seo_intel.real.backlinks import link_profile
from seo_intel.real.env import data_gaps
from seo_intel.tools.extract import PageFeatures, looks_like_instructions, redact_instructions
from seo_intel.tools.history import history_diff
from seo_intel.tools.text import tokens
from seo_intel.tools.site import discover, fetch_page

_QUOTED = re.compile(r"「([^」]+)」")
DIMINISHING_WINDOW = 15
CHANGES_TO_RESOLVE = 3
ON_TOPIC_OVERLAP = 0.75
MAX_KEYWORD_WORDS = 8
MAX_ACTION_ATTEMPTS = 2
REAL_UNAVAILABLE = {"rank_change", "control_check"}
UNAVAILABLE_WHY = {"rank_change": "沒有排名歷史", "control_check": "沒有非主題查詢的排名資料",
                   "serp_competitors": "沒有設定排名資料來源", "backlink_profile": "沒有提供反向連結 CSV",
                   "own_site_profile": "沒有提供自家網址"}
GOAL_TYPE = "competitive_strategy"
FIXED_CRITERIA = [
    "On-page：指出競品有排名的頁面普遍採用、而同網站其他主題頁較少採用的做法，每項附量測與統計檢定",
    "Off-page：以反向連結資料說明競品的連結成長、錨點文字與連結指向（沒有資料時明確說明無法研究）",
    "優先行動：依影響、自家網站的差距與實作難度排序（沒有自家網站時說明無法比較差距）",
]
_TRANSIENT = re.compile(r"timeout|connect_error|http_5[0-9][0-9]|http_429")


# ---------- 共用 ----------

def _call_llm(ctx: EpisodeContext, role: str, user: str, schema, retries: int = 1, system: str = PLANNER_SYSTEM):
    records: list[dict] = []
    user, n_redacted = redact_instructions(user)   # F6 最後一道防線：LLM 只該看到程式整理過的資料
    if n_redacted:
        ctx.state.security_events.append(f"送給 {role} 的資料中有 {n_redacted} 段疑似指令文字，已在送出前移除")
    for attempt in range(retries + 1):
        try:
            res = ctx.llm.call(role, system=system, user=user, schema=schema)
        except LLMError as e:
            ctx.ledger.charge_llm(e.meta)
            records.append(llm_record(e.meta, error=f"{type(e).__name__}: {e}") | {"role": role})
            if e.retryable and attempt < retries:
                continue
            return None, records, str(e)
        ctx.ledger.charge_llm(res.meta)
        records.append(llm_record(res.meta, output=res.parsed.model_dump(mode="json")))
        return res.parsed, records, None
    return None, records, "retries exhausted"


def _short(url_or_tkey: str) -> str:
    if url_or_tkey.startswith("http"):
        p = urlsplit(url_or_tkey)
        return f"{p.hostname}{p.path}" + (f"?{p.query}" if p.query else "")
    return url_or_tkey


def _window(ctx: EpisodeContext) -> tuple[date, date]:
    g = ctx.state.goal
    return g.window_start, g.window_end


def _phrases(ctx: EpisodeContext) -> list[set[str]]:
    """策略量測用的關鍵字：使用者指定的關鍵字（競爭的就是這些詞）；沒有時用研究目標的主題關鍵字。"""
    st = ctx.state
    return strategy.phrases_of(st.task.keywords or st.goal.topic_keywords)


def _resolve(need: InfoNeed, text: str, outcome: StepOutcome, status: str = "resolved") -> None:
    if need.status != "open":
        return
    need.status = status
    need.resolution = text
    label = "已解答" if status == "resolved" else "停止追查"
    outcome.details.append(f"{need.id} {label}：{text}")


# ---------- DEFINE_GOAL ----------

def _fallback_keywords(task: TaskInput) -> list[str]:
    return [m.lower() for m in _QUOTED.findall(task.question)] or [task.question.lower()]


def normalize_goal(out: GoalOut | None, task: TaskInput) -> tuple[ResearchGoal, list[str]]:
    notes: list[str] = []
    hint = task.period_hint or (task.today - timedelta(days=90), task.today)
    if out is None:
        notes.append("LLM 未產生目標，改用任務文字的確定性預設")
        keywords = [k.lower() for k in task.keywords] or _fallback_keywords(task)
        return ResearchGoal(restated_question=task.question, goal_type=GOAL_TYPE, topic_keywords=keywords,
                            focus_domains=task.competitors, window_start=hint[0], window_end=hint[1],
                            success_criteria=list(FIXED_CRITERIA)), notes

    def parse(raw: str, fallback: date, label: str) -> date:
        try:
            return date.fromisoformat(raw.strip()[:10])
        except ValueError:
            notes.append(f"{label} 日期格式無效（{raw}），改用 {fallback}")
            return fallback

    ws, we = parse(out.window_start, hint[0], "起始"), parse(out.window_end, hint[1], "結束")
    if ws >= we:
        notes.append("研究期間無效，改用任務提示的期間")
        ws, we = hint
    if we > task.today:
        we = task.today
    raw_kw = [k.strip().lower() for k in out.topic_keywords if k.strip()]
    keywords = [k for k in raw_kw if not looks_like_instructions(k) and "://" not in k
                and len(k.split()) <= MAX_KEYWORD_WORDS][:5]
    if len(keywords) < min(5, len(raw_kw)):
        notes.append("已移除可疑或過長的關鍵字（疑似指令文字、網址或超過 8 個字）")
    if task.keywords:   # 使用者指定的關鍵字一定保留且排在前面；LLM 提出的變體排在後面
        user = [k.strip().lower() for k in task.keywords if k.strip()]
        keywords = list(dict.fromkeys(user + keywords))[:max(5, len(user))]
    if not keywords:
        notes.append("LLM 未提供關鍵字，改用問題中的引號詞")
        keywords = _fallback_keywords(task)
    focus = [d for d in out.focus_domains if d in task.competitors]
    if len(focus) != len(out.focus_domains):
        notes.append("已移除不在任務競品清單中的焦點網域")
    if out.goal_type != GOAL_TYPE:   # 研究目標固定為三個問題；排名下滑原因在真實網站上也無法診斷（沒有排名歷史）
        notes.append(f"研究目標固定為競爭策略的三個問題（LLM 提出的類型 {out.goal_type} 不採用）")
    criteria = FIXED_CRITERIA + [c for c in out.success_criteria if c not in FIXED_CRITERIA]
    return ResearchGoal(restated_question=out.restated_question, goal_type=GOAL_TYPE,
                        topic_keywords=keywords, focus_domains=focus or task.competitors,
                        window_start=ws, window_end=we, success_criteria=criteria), notes


def memory_setup(ctx: EpisodeContext) -> list[str]:
    """載入記憶並接上 frontier（新 episode 與續跑都會呼叫）。回傳可讀的說明。"""
    mem, st = ctx.memory, ctx.state
    if mem is None:
        return ["記憶：未啟用（每次都從零開始，相當於 Day 1）"]
    mem.state = st
    loaded = mem.load(st.goal.goal_type)
    ctx.scratch["lessons"] = ctx.scratch["hypothesis_lessons"] = mem.texts()
    ctx.frontier.attach_memory(mem.prior if mem.use_m2 else None, mem.avoid if mem.use_m1 else None)
    for lesson in loaded:
        if not lesson.biases or any(b.type != "avoid_path" for b in lesson.biases):
            mem.mark(st, lesson, "prompt")
    lines = [f"記憶：M1 經驗庫{'啟用' if mem.use_m1 else '停用'}（載入 {len(loaded)} 條生效經驗）、"
             f"M2 URL 價值先驗{'啟用' if mem.use_m2 else '停用'}"]
    lines += [f"  {x.id} [{x.source}] {x.lesson}（{'；'.join(b.describe() for b in x.biases) or '僅文字'}）"
              for x in loaded]
    return lines


def define_goal(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    out, records, error = _call_llm(ctx, "planner", goal_prompt(st.task), GoalOut)
    goal, notes = normalize_goal(out, st.task)
    if any("疑似指令" in n for n in notes):
        st.security_events.append("LLM 提出的研究目標含疑似指令文字或網址的關鍵字，已在使用前移除")
    st.goal = goal
    ctx.frontier = Frontier(goal.topic_keywords, (goal.window_start, goal.window_end), st.task.allowed_domains)
    ctx.frontier.set_focus(goal.focus_domains)
    details = [f"類型：{goal.goal_type}",
               f"主題關鍵字：{', '.join(goal.topic_keywords)}",
               f"研究期間：{goal.window_start} ～ {goal.window_end}",
               f"焦點網域：{', '.join(goal.focus_domains)}"]
    details += [f"成功條件：{c}" for c in goal.success_criteria]
    details += [f"⚠ {n}" for n in notes]
    details += memory_setup(ctx)
    return StepOutcome(summary=f"研究目標：{goal.restated_question}", details=details,
                       decision={"goal": goal.model_dump(mode="json"), "notes": notes}, llm=records,
                       reason=error, lessons_applied=sorted(st.lessons_applied))


# ---------- PLAN_INFO_NEEDS ----------

def unavailable_kinds(task: TaskInput) -> set[str]:
    """拿不到的資料需求：真實網站沒有排名歷史；沒有排名來源就沒有 SERP；沒有反向連結 CSV、自家網址也一樣。"""
    return (REAL_UNAVAILABLE | ({"serp_competitors"} if task.serp_source == "none" else set())
            | ({"backlink_profile"} if task.backlink_source == "none" or not task.backlink_domains else set())
            | ({"own_site_profile"} if not task.own_site else set()))


def normalize_plan(out: PlanOut | None, task: TaskInput, goal: ResearchGoal) -> tuple[list[InfoNeed], list[str]]:
    notes: list[str] = []
    own, comps = task.own_site, task.competitors
    needs: list[InfoNeed] = []
    seen: set[tuple] = set()
    unavailable = unavailable_kinds(task)
    for raw in (out.info_needs[:8] if out else []):
        if raw.kind in unavailable:
            notes.append(f"略過 {raw.kind}：真實網站模式拿不到這類資料（{UNAVAILABLE_WHY[raw.kind]}）")
            continue
        targets = [d for d in raw.target_domains if d in task.allowed_domains]
        if raw.kind == "control_check":
            targets = [own]
        if raw.kind in ("site_inventory", "page_changes", "ranking_pages"):
            targets = [d for d in targets if d in comps]
        if raw.kind == "backlink_profile":
            targets = list(task.backlink_domains)       # 只有 CSV 裡有資料的網站
        if raw.kind == "own_site_profile":
            targets = [own]
        if not targets:
            targets = goal.focus_domains
            notes.append(f"{raw.kind} 未指定有效網域，改用 {', '.join(targets)}")
        key = (raw.kind, tuple(targets))
        if key in seen:
            continue
        seen.add(key)
        needs.append(InfoNeed(id=f"N{len(needs) + 1}", kind=raw.kind, question=raw.question, target_domains=targets,
                              answer_criteria=raw.answer_criteria, priority=min(5, max(1, raw.priority)),
                              must_answer=raw.must_answer))
    # 三個問題的必要證據：網站盤點、排名頁輪廓、頁面變化；有資料時加上 SERP、反向連結與自家網站
    ranking_q = ("競品有排名的頁面採取了哪些做法？和同網站其他主題頁比有什麼不同？" if task.serp_source != "none"
                 else "競品的主題頁採取了哪些做法？（沒有排名資料，無法區分哪些頁面有排名）")
    essentials = {"site_inventory": (goal.focus_domains, 1, "競品有哪些頁面範本，哪些近期有更新？"),
                  "ranking_pages": (goal.focus_domains, 1, ranking_q),
                  "page_changes": (goal.focus_domains, 2, "競品在這些關鍵字相關的頁面，研究期間內做了什麼改變？")}
    if task.serp_source != "none":
        essentials["serp_competitors"] = (comps, 1, "這些關鍵字目前誰排在前面、排的是哪些頁面？")
    if "backlink_profile" not in unavailable:
        essentials["backlink_profile"] = (list(task.backlink_domains), 2,
                                          "競品（與自家）的反向連結：研究期間新增了多少引用網域、錨點文字與連結指向？")
    if "own_site_profile" not in unavailable:
        essentials["own_site_profile"] = ([own], 3, "自家主題頁在同一套做法上的狀態如何？（用來計算差距）")
    for kind, (targets, prio, question) in essentials.items():
        if not any(n.kind == kind for n in needs):
            needs.append(InfoNeed(id=f"N{len(needs) + 1}", kind=kind, question=question, target_domains=targets,
                                  answer_criteria="以量測資料回答", priority=prio, must_answer=True, added_by="system"))
            notes.append(f"規劃缺少 {kind}，系統補上（研究迴圈需要這類證據）")
    return needs, notes


def plan_info_needs(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    lessons: list[str] = ctx.scratch.get("lessons", [])
    out, records, error = _call_llm(ctx, "planner", plan_prompt(st.task, st.goal, lessons), PlanOut)
    needs, notes = normalize_plan(out, st.task, st.goal)
    applied = []
    if ctx.memory is not None:   # M1 偏置：調整資訊需求優先序，缺少時補上
        own, focus = st.task.own_site, st.goal.focus_domains
        default_targets = {"serp_competitors": st.task.competitors, "site_inventory": focus, "page_changes": focus,
                           "ranking_pages": focus, "backlink_profile": list(st.task.backlink_domains),
                           "own_site_profile": [own]}
        for lesson, b in ctx.memory.biases("info_need_priority"):
            if b.kind in unavailable_kinds(st.task):
                continue
            need = next((n for n in needs if n.kind == b.kind), None)
            if need is None:
                needs.append(InfoNeed(id=f"N{len(needs) + 1}", kind=b.kind, question=f"依經驗 {lesson.id} 補上",
                                      target_domains=default_targets[b.kind], answer_criteria="以量測資料回答",
                                      priority=b.priority, must_answer=True, added_by="system"))
                notes.append(f"依經驗 {lesson.id} 補上 {b.kind}（優先 {b.priority}）")
            elif need.priority != b.priority:
                notes.append(f"依經驗 {lesson.id} 把 {need.id} [{b.kind}] 優先序 {need.priority} → {b.priority}")
                need.priority = b.priority
            ctx.memory.mark(st, lesson, "plan")
            applied.append(lesson.id)
    st.info_needs = needs
    details = [f"{n.id} [{n.kind}] 優先 {n.priority}{' 必答' if n.must_answer else ''}"
               f"{'（系統補上）' if n.added_by == 'system' else ''} {', '.join(n.target_domains)}：{n.question}"
               for n in needs]
    if out:
        details.append(f"規劃理由：{out.rationale}")
    details += [f"⚠ {n}" for n in notes]
    return StepOutcome(summary=f"規劃 {len(needs)} 項資訊需求", details=details,
                       decision={"info_needs": [n.model_dump(mode="json") for n in needs], "notes": notes},
                       llm=records, reason=error, lessons_applied=applied)


# ---------- SELECT_ACTION ----------

def _score_line(breakdown: dict[str, float]) -> str:
    parts = [f"{COMPONENT_LABELS.get(k, k)} {v:+.2f}" for k, v in breakdown.items() if abs(v) >= 0.005]
    return " ".join(parts)


def _pause_for_action(ctx: EpisodeContext, a: Action, v) -> StepOutcome:
    est = {"domain": action_scope(a), "tool": a.tool}
    if v.rule == "continue_after_blocks":
        rate, n = ctx.policy.block_rate(action_scope(a))
        est |= {"block_rate": round(rate, 3), "fetched": n}
    ap = approvals.request(ctx, v.rule, v.reason, action=a, estimate=est)
    return StepOutcome(summary=f"暫停：{_short(a.label())} 需要人工核准（{ap.id}）",
                       details=[f"規則 {v.rule}：{v.reason}", f"核准請求 {ap.id}，期限 {ap.expires_at}",
                                f"核准：seo-intel approvals approve {ap.id}；拒絕：seo-intel approvals reject {ap.id}"],
                       decision={"phase": ctx.state.phase, "approval": ap.model_dump(mode="json")}, reason=v.reason)


def _fetches_needed(queue: list[Action]) -> int:
    return sum(1 if a.tool == "fetch" else EST_DISCOVER_FETCHES for a in queue if a.tool in ("fetch", "discover"))


def request_budget(ctx: EpisodeContext, resource: str, amount: float, reason: str, estimate: dict) -> StepOutcome | None:
    """預算用盡但還有待完成的工作：請求人工核准延長。之前被拒絕過（或政策禁止）則回傳 None，由呼叫端降級。"""
    st = ctx.state
    if ctx.policy.classify("exceed_budget") == "deny" or approvals.denied(st, "exceed_budget", resource):
        return None
    ap = approvals.request(ctx, "exceed_budget", reason, extend={resource: amount}, estimate=estimate)
    label = approvals.RESOURCE_ZH.get(resource, resource)
    return StepOutcome(summary=f"暫停：{reason}；請求延長{label} {amount:g}（{ap.id}）",
                       details=[f"預估：{json.dumps(estimate, ensure_ascii=False)}", f"核准請求 {ap.id}，期限 {ap.expires_at}",
                                f"核准：seo-intel approvals approve {ap.id}；拒絕：seo-intel approvals reject {ap.id}"
                                "（拒絕時以現有證據下結論，並在報告註明限制）"],
                       decision={"phase": st.phase, "approval": ap.model_dump(mode="json")}, reason=reason)


def _fetch_budget_request(ctx: EpisodeContext) -> StepOutcome | None:
    st = ctx.state
    need = _fetches_needed(st.evidence_queue)
    amount = need + max(2, math.ceil(need * RETRY_MARGIN))   # 重試（429、5xx）也算抓取：預估不留餘裕，一次核准會變兩次
    pids = sorted({a.probe_id for a in st.evidence_queue if a.probe_id})
    labels = [f"{p} {probes.CATALOG[st.probe(p).kind].label}" for p in pids if st.probe(p)]
    reason = f"驗證還需要約 {need} 次抓取（{'、'.join(labels) or '證據佇列'}），但抓取預算 {ctx.ledger.max_fetches} 次已用完"
    est = {"fetches": need, "with_retry_margin": amount,
           "share_of_original_budget": round(amount / ctx.settings.budget.max_fetches, 3),
           "probes": labels, "testing_hypotheses": [h.id for h in st.hypotheses if h.status == "testing"]}
    return request_budget(ctx, "max_fetches", amount, reason, est)


def _drop_queue(ctx: EpisodeContext, why: str) -> list[str]:
    st = ctx.state
    lines = []
    for a in st.evidence_queue:
        probes.drop_action(ctx, a, why)
        lines.append(f"✗ 略過 {_short(a.label())}：{why}")
    if st.evidence_queue:
        pids = sorted({a.probe_id for a in st.evidence_queue if a.probe_id})
        st.limitations.append(f"{why}：{'、'.join(pids) or '部分證據'} 未完成，結論只依據已取得的證據")
    st.evidence_queue = []
    return lines


def _select_evidence_action(ctx: EpisodeContext) -> StepOutcome:
    """證據佇列優先：使用保留給驗證的預算。"""
    st = ctx.state
    st.phase = Phase.VERIFICATION
    skipped = []
    while st.evidence_queue:
        a = st.evidence_queue.pop(0)
        if a.tool in ("fetch", "discover") and ctx.ledger.fetch_room(Phase.VERIFICATION) <= 0:
            st.evidence_queue.insert(0, a)
            if (paused := _fetch_budget_request(ctx)) is not None:
                st.pending = None
                return paused
            skipped += _drop_queue(ctx, "抓取預算已用完（人工拒絕延長）")
            break
        v = ctx.policy.check(a)
        if v.decision == "deny":
            probes.drop_action(ctx, a, f"政策拒絕：{v.reason}")
            skipped.append(f"✗ 拒絕 {a.label()}：{v.reason}")
            continue
        if v.decision == "require_approval":
            st.evidence_queue.insert(0, a)
            st.pending = None
            return _pause_for_action(ctx, a, v)
        st.pending = a
        ctx.scratch["no_candidates"] = False
        details = [f"理由：{a.reason}", f"證據佇列剩餘 {len(st.evidence_queue)} 個行動"] + skipped
        return StepOutcome(summary=_short(a.label()), details=details, reason=a.reason,
                           decision={"phase": st.phase, "action": a.model_dump(mode="json"), "evidence_queue": True})
    st.pending = None
    ctx.scratch["no_candidates"] = False
    return StepOutcome(summary="證據佇列已清空", details=skipped, decision={"phase": st.phase})


def select_action(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    if st.evidence_queue:
        return _select_evidence_action(ctx)
    previous = st.phase
    st.phase = compute_phase(ctx)
    cands = generate(ctx, st.phase)
    chosen, rejected = None, []
    for a in cands[:30]:
        v = ctx.policy.check(a)
        if v.decision == "allow":
            chosen = a
            break
        if v.decision == "deny":
            rejected.append({"action": a.label(), "rule": v.rule, "reason": v.reason})
            st.done_signatures.append(a.signature)
            if a.tool == "fetch":
                ctx.frontier.exclude(a.args["url"], v.rule or "policy")
            continue
        st.pending = None
        return _pause_for_action(ctx, a, v)
    st.pending = chosen
    ctx.scratch["no_candidates"] = chosen is None
    if chosen is not None and ctx.memory is not None:
        for lid in chosen.lessons:
            lesson = next((x for x in ctx.memory.lessons if x.id == lid), None)
            if lesson:
                ctx.memory.mark(st, lesson, "sampling")
    details = []
    focus_lesson = ctx.scratch.pop("focus_skip", None)
    if st.phase != previous:
        details.append(f"進入階段 {st.phase.value}（抓取額度上限 {ctx.ledger.fetch_cap(st.phase)}）")
        if focus_lesson and previous in (Phase.DISCOVERY, Phase.SAMPLING):
            details.append(f"依經驗 {focus_lesson}：有訊號的範本已有足夠證據候選，跳過其餘範本的普查")
            lesson = next((x for x in ctx.memory.lessons if x.id == focus_lesson), None)
            if lesson:
                ctx.memory.mark(st, lesson, "skip_broad_sampling")
                if chosen is not None and focus_lesson not in chosen.lessons:
                    chosen.lessons.append(focus_lesson)
    if chosen:
        details += [f"理由：{chosen.reason}", f"評分 {chosen.score:.2f}：{_score_line(chosen.breakdown)}"]
        alts = [f"{a.label()}（{a.score:.2f}）" for a in cands[1:4] if a is not chosen]
        if alts:
            details.append("其他候選：" + "；".join(_short(x) for x in alts))
    details += [f"✗ 拒絕 {r['action']}：{r['reason']}" for r in rejected]
    return StepOutcome(
        summary=_short(chosen.label()) if chosen else "沒有可執行的行動",
        details=details,
        decision={"phase": st.phase, "action": chosen.model_dump(mode="json") if chosen else None,
                  "n_candidates": len(cands), "rejected": rejected},
        reason=chosen.reason if chosen else None,
        lessons_applied=list(chosen.lessons) if chosen else [],
        candidates=[{"action": a.label(), "tool": a.tool, "score": a.score, "breakdown": a.breakdown,
                     "reason": a.reason} for a in cands[:5]],
    )


# ---------- EXECUTE_TOOL ----------

def _features_from_info(url: str, info: dict) -> PageFeatures:
    return PageFeatures(url=url, title=info.get("title", ""), h1=info.get("h1", ""), h2=info.get("h2", []),
                        word_count=info.get("words", 0), schema_types=info.get("schema", []),
                        faq_count=info.get("faq", 0), has_table=info.get("table", False),
                        modified=info.get("modified"), published=info.get("published"),
                        meta_description=info.get("meta_desc", ""), author=info.get("author", ""))


def execute_tool(ctx: EpisodeContext) -> StepOutcome:
    st, a = ctx.state, ctx.state.pending
    ctx.ledger.charge_action()
    st.done_signatures.append(a.signature)
    t0 = ctx.clock.now()
    summary = a.label()
    try:
        if a.tool == "serp_scan":   # 競品與自家：自家在 SERP 上的頁面也是差距比較的依據
            raw = ctx.rank.serp_snapshot(st.task.keywords or st.goal.topic_keywords, st.task.allowed_domains)
            summary = f"serp_scan {len(raw['queries'])} 個關鍵字（{raw['source']}，{raw['after']}）"
        elif a.tool == "discover":
            raw = discover(ctx.http, a.args["domain"])
            summary = f"discover {a.args['domain']} → {len(raw['entries']):,} 個 URL（{len(raw['sitemaps'])} 個 sitemap）"
        elif a.tool == "fetch":
            res, feats = fetch_page(ctx.http, a.args["url"])
            raw = {"result": res, "features": feats}
            status = res.status if res.status is not None else res.error
            summary = f"fetch {_short(a.args['url'])} → {status}" + ("（快取）" if res.from_cache else "")
        elif a.tool == "backlinks":   # 本機檔案（使用者上傳的 CSV），不連網
            if ctx.backlinks is None:
                raise RuntimeError("沒有反向連結資料")
            ranking = {u for d in st.task.allowed_domains for u in profiles.serp_urls(ctx, d)}
            raw = {"profiles": {d: link_profile(ctx.backlinks.for_site(d), d, _window(ctx),
                                                st.task.keywords or st.goal.topic_keywords, ranking)
                                for d in a.args["domains"] if ctx.backlinks.for_site(d)}}
            summary = f"backlinks {len(raw['profiles'])} 個網站（{'、'.join(ctx.backlinks.files)}）"
        elif a.tool == "history_diff":
            url = a.args["url"]
            info = st.fetched[url]
            current = _features_from_info(url, info)
            modified = current.modified_date()
            before = min(_window(ctx)[0], modified - timedelta(days=1)) if modified else _window(ctx)[0]
            raw = history_diff(ctx.archive, url, current, before)
            summary = f"history_diff {_short(url)} → {raw['status']}"
        else:
            raise ValueError(f"未知工具 {a.tool}")
    except Exception as e:   # F3：工具崩潰（API 錯誤、例外、格式錯誤）轉成觀察，迴圈不中斷
        raw = {"error": f"{type(e).__name__}: {e}"}
        summary = f"{a.label()} → 失敗：{type(e).__name__}: {e}"
    ctx.scratch["raw"] = raw
    attempts = ctx.http.drain_attempts()
    waited = sum(x.waited_s for x in attempts)
    details = [f"HTTP 請求 {len(attempts)} 次，禮貌等待 {waited:.1f}s，耗時 {ctx.clock.now() - t0:.1f}s（環境時間）"]
    details += [f"重試：{_short(x.url)} 第 {x.attempt} 次 → {x.status or x.error}" for x in attempts if x.error]
    if "error" in raw and not a.probe_id and _TRANSIENT.search(raw["error"]):   # F3：暫時性錯誤，行動層再給一次機會
        n = st.failed_attempts[a.signature] = st.failed_attempts.get(a.signature, 0) + 1
        if n < MAX_ACTION_ATTEMPTS:
            st.done_signatures.remove(a.signature)
            details.append(f"暫時性錯誤（第 {n} 次）：稍後會再嘗試這個行動")
    return StepOutcome(summary=summary, details=details, decision={"action": a.model_dump(mode="json")},
                       tool_calls=[{"url": x.url, "status": x.status, "error": x.error, "attempt": x.attempt,
                                    "waited_s": round(x.waited_s, 2)} for x in attempts])


# ---------- PARSE_RESULT ----------

def _check_page_changes(ctx: EpisodeContext, out: StepOutcome) -> None:
    st = ctx.state
    focus = set(st.serp_gainers)
    for need in (n for n in st.info_needs if n.kind == "page_changes" and n.status == "open"):
        candidates = []
        for tkey, urls in st.template_changes.items():
            domain = tkey.split("/", 1)[0]
            if domain in need.target_domains and len(set(urls)) >= CHANGES_TO_RESOLVE:
                candidates.append((domain in focus, len(set(urls)), tkey))
        if not candidates:
            continue
        if focus and not any(c[0] for c in candidates):
            continue   # SERP 上升的網域尚未找到變動，先不下結論
        _, n, tkey = max(candidates)
        types = Counter()
        for o in st.observations:
            if o.url in st.template_changes[tkey]:
                types.update(o.data.get("change_types", []))
                if o.kind == "page" and o.data.get("new_page"):
                    types["new_page"] += 1
        top = "、".join(f"{t}×{c}" for t, c in types.most_common(4)) or "變動"
        _resolve(need, f"{tkey}：{n} 頁在期間內出現顯著變動或新增（{top}）", out)


def _parse_serp(ctx: EpisodeContext, raw: dict, out: StepOutcome) -> None:
    st, fr = ctx.state, ctx.frontier
    st.serp_gainers = [d for d in raw["gainers"] if d in st.task.competitors]
    added = 0
    for urls in raw["target_urls_after"].values():
        for u in urls:
            added += fr.add(u, source="serp", allowed=ctx.http.allowed(u))
    fr.set_focus(list(dict.fromkeys([*st.serp_gainers, *st.goal.focus_domains])))
    top = raw["movement"][0] if raw["movement"] else None
    if raw.get("snapshot_only"):   # 真實網站第一次研究：只有目前的 SERP，沒有可比較的歷史
        pos = {m["domain"]: m["after"] for m in raw["movement"]}
        leaders = sorted(pos.items(), key=lambda kv: kv[1])[:3]
        summary = (f"目前 SERP（{raw['source']}，{raw['after']}）："
                   + "、".join(f"{d} 平均第 {p:g} 名" for d, p in leaders)
                   + "；競品：" + "、".join(f"{d} {'第 ' + format(pos[d], 'g') + ' 名' if pos.get(d, 101) <= 100 else '未進前 100'}"
                                         for d in st.task.competitors))
        out.details.append("這是第一次記錄這些關鍵字的 SERP，沒有可比較的歷史；下次研究時就能看到名次變化")
        for kw, rows in raw.get("top", {}).items():
            out.details.append(f"「{kw}」前 {len(rows)} 名：" + "、".join(f"{r['position']}. {r['domain']}" for r in rows))
    else:
        summary = (f"SERP 上升最多：{top['domain']}（平均名次 {top['before']} → {top['after']}）"
                   if top and top["gain"] > 0 else "主題查詢的 SERP 沒有明顯上升者")
    obs = st.add_observation("serp_shift", summary, raw, need_id=st.pending.need_id)
    out.observations.append(obs.id)
    out.summary = f"{obs.id} {summary}"
    out.details.append(f"競品中的上升者：{', '.join(st.serp_gainers) or '無'}；加入 {added} 個 SERP 網址到 frontier")
    for need in st.needs_of("serp_competitors"):
        _resolve(need, summary, out)
    profiles.check(ctx, out)


def _parse_backlinks(ctx: EpisodeContext, raw: dict, out: StepOutcome) -> None:
    need = ctx.state.need(ctx.state.pending.need_id)
    if need is not None:
        profiles.link_profiles(ctx, need, raw, out)
    out.summary = f"反向連結輪廓：{len(raw['profiles'])} 個網站"


def _parse_discover(ctx: EpisodeContext, raw: dict, out: StepOutcome) -> None:
    st, fr = ctx.state, ctx.frontier
    domain = raw["domain"]
    src = "link" if raw.get("source") == "homepage_links" else "sitemap"
    added = sum(fr.add(u, source=src, lastmod=lm, allowed=ctx.http.allowed(u)) for u, lm in raw["entries"])
    if src == "link":
        out.details.append(f"{domain} 沒有可用的 sitemap：改從首頁的站內連結開始（沒有 lastmod，無法判斷更新爆發度）")
    rows = [{"template": s.key.split("/", 1)[1], "n_urls": s.n_urls, "n_dated": s.n_dated, "n_recent": s.n_recent,
             "recent_share": round(s.n_recent / s.n_dated, 3) if s.n_dated else None}   # None：sitemap 沒有 lastmod
            for s in fr.stats([domain]) if s.n_sitemap]
    hot = sorted((r for r in rows if r["recent_share"] is not None),
                 key=lambda r: (-r["recent_share"], -r["n_recent"]))[:3]
    undated = [r["template"] for r in rows if r["recent_share"] is None]
    summary = (f"{domain}：{added:,} 個 URL、{len(rows)} 個範本；期間內更新比例最高："
               + "、".join(f"{r['template']}（{r['n_recent']}/{r['n_dated']}）" for r in hot)
               + (f"；沒有 lastmod：{'、'.join(undated[:3])}" if undated else ""))
    fr.reprime(domain, rows)   # 已知範本規模與更新爆發度：重新查詢經驗先驗
    primed = [s for s in fr.stats([domain]) if s.prior_source]
    if primed:
        out.details.append(f"經驗先驗：{len(primed)} 個範本已套用 M2 先驗（例如 "
                           + "、".join(f"{s.key.split('/', 1)[1]} {s.prior_mean:.2f}" for s in primed[:3]) + "）")
    avoided = [s for s in fr.stats([domain]) if s.avoid_source]
    if avoided:
        out.details.append("依經驗避開：" + "、".join(f"{s.key.split('/', 1)[1]}（{s.avoid_source}）" for s in avoided))
        out.lessons_applied += sorted({s.avoid_source for s in avoided})
    data = {"domain": domain, "n_urls": added, "templates": rows, "sitemaps": len(raw["sitemaps"]),
            "errors": raw["errors"], "excluded": dict(fr.excluded)}
    obs = st.add_observation("site_inventory", summary, data, need_id=st.pending.need_id)
    out.observations.append(obs.id)
    out.summary = f"{obs.id} {summary}"
    if raw["errors"]:
        out.details.append(f"sitemap 錯誤 {len(raw['errors'])} 個")
    for need in st.needs_of("site_inventory"):
        done = {o.data["domain"] for o in st.observations if o.kind == "site_inventory"}
        if set(need.target_domains) <= done:
            _resolve(need, f"已取得 {', '.join(need.target_domains)} 的範本清單", out)
    profiles.check(ctx, out)


def _on_topic(ctx: EpisodeContext, text: str) -> bool:
    """標題完整包含任一組關鍵字片語（容許 25% 缺漏）即為主題頁。"""
    return ctx.frontier.phrase_coverage(tokens(text)) >= ON_TOPIC_OVERLAP


def _parse_fetch(ctx: EpisodeContext, raw: dict, out: StepOutcome) -> None:
    st, fr = ctx.state, ctx.frontier
    res, f = raw["result"], raw["features"]
    url = normalize_url(res.url)
    tkey = template_key(url)
    if f is None:
        blocked = res.status == 403
        fr.mark_fetched(url, ok=False, evidence=False, blocked=blocked)
        st.fetched[url] = {"status": res.status, "error": res.error, "template": tkey}
        st.fetch_yield.append(0)
        out.summary = f"{_short(url)} 無法取得（{res.error}）"
        if blocked:
            obs = st.add_observation("blocked", f"{_short(tkey)} 被網站封鎖（403），此範本降權", {"url": url}, url=url)
            out.observations.append(obs.id)
        return
    ws, we = _window(ctx)
    mod, pub = f.modified_date(), f.published_date()
    in_win = lambda d: d is not None and ws <= d <= we  # noqa: E731
    on_topic = _on_topic(ctx, f"{f.title} {f.h1}")
    evidence = on_topic and (in_win(mod) or in_win(pub)) and not f.js_shell
    new_page = on_topic and in_win(pub)
    n_before = len(fr.entries)
    for link, anchor in ([] if f.suspicious_instructions else f.links):   # F6：含注入文字的頁面不提供連結
        fr.add(link, source="link", anchor=anchor, allowed=ctx.http.allowed(link))
    entry = fr.entries.get(url)
    fr.mark_fetched(url, ok=True, evidence=evidence, blocked=False)
    info = {"status": 200, "template": tkey, "title": f.title, "h1": f.h1, "words": f.word_count,
            "schema": f.schema_types, "faq": f.faq_count, "table": f.has_table, "h2": f.h2, "modified": f.modified,
            "published": f.published, "meta_desc": f.meta_description[:200], "author": f.author[:80],
            "on_topic": on_topic, "evidence": evidence, "relevance": entry.relevance if entry else 0.0,
            "js_shell": f.js_shell, "malformed": f.malformed}
    info["strategy"] = strategy.page_values(url, info, _phrases(ctx), (ws, we))   # 策略維度：每一頁都量，供對比與差距
    if new_page:
        st.template_changes.setdefault(tkey, []).append(url)
        info["change"] = "new"
    st.fetched[url] = info
    st.fetch_yield.append(int(evidence))
    verdict = "證據候選" if evidence else "主題相關但不在期間內" if on_topic else "非主題頁"
    out.summary = f"{_short(url)} → {verdict}（{f.word_count} 字，schema {', '.join(f.schema_types) or '無'}，" \
                  f"FAQ {f.faq_count}，修改 {mod or '—'}）"
    out.details.append(f"新發現連結 {len(fr.entries) - n_before} 個；範本 {_short(tkey)} "
                       f"證據率 {fr.templates[tkey].value:.2f}（{fr.templates[tkey].n_evidence}/{fr.templates[tkey].n_fetched}）")
    if not f.js_shell:
        out.details.append(f"策略量測：{strategy.describe(info['strategy'])}")
    if evidence:
        obs = st.add_observation("page", f"{_short(url)}：主題頁，{'新發布' if new_page else '修改'}於期間內",
                                 {**info, "new_page": new_page, "h2": f.sections}, need_id=st.pending.need_id, url=url)
        out.observations.append(obs.id)
    if f.suspicious_instructions:
        obs = st.add_observation("security_flag", f"{_short(url)} 含疑似對 AI 下指令的文字，已視為資料並忽略",
                                 {"url": url}, url=url)
        out.observations.append(obs.id)
        st.security_events.append(f"{_short(url)} 含疑似對 AI 下指令的文字（prompt injection）：已忽略，"
                                  f"也不採用此頁的 {len(f.links)} 個連結")
        out.details.append(f"⚠ 偵測到頁面內嵌指令（prompt injection）：視為資料、不採用此頁的 {len(f.links)} 個連結")
    if f.js_shell:
        out.details.append("JS 空殼頁：沒有可用內容")
    if f.malformed:
        out.details.append("HTML 不完整：已解析可用的部分")
    _check_page_changes(ctx, out)
    profiles.check(ctx, out)


def _parse_history(ctx: EpisodeContext, raw: dict, out: StepOutcome) -> None:
    st = ctx.state
    url = raw["url"]
    info = st.fetched[url]
    info["diffed"] = True
    info["diff_status"] = raw["status"]
    tkey = info["template"]
    if raw["status"] == "ok":
        summary = (f"{_short(url)}：{raw['snapshot_date']} → 目前，字數 {raw['words_before']}→{raw['words_after']}"
                   f"（{raw['words_change_pct']:+.0%}）；{', '.join(raw['change_types']) or '無顯著變化'}")
        if raw.get("before_page"):   # 改版前後的策略維度：哪些做法是研究期間內才採用的
            after = info.get("strategy") or strategy.page_values(url, info, _phrases(ctx), _window(ctx))
            before = strategy.page_values(url, raw["before_page"], _phrases(ctx), _window(ctx))
            raw["adopted"], raw["dropped"] = strategy.adoption(before, after)
            info["adopted"] = raw["adopted"]
            if raw["adopted"]:
                summary += "；期間內新採用：" + "、".join(strategy.label(k) for k in raw["adopted"])
            if raw["dropped"]:
                summary += "；拿掉：" + "、".join(strategy.label(k) for k in raw["dropped"])
        if raw["significant"] and url not in st.template_changes.get(tkey, []):
            st.template_changes.setdefault(tkey, []).append(url)
            info["change"] = "diff"
    else:
        reason = {"no_snapshot_before": "沒有期間前的快照", "snapshot_unavailable": "快照無法取得"}.get(raw["status"], raw["status"])
        summary = f"{_short(url)}：{reason}"
    obs = st.add_observation("page_diff", summary, raw, need_id=st.pending.need_id, url=url)
    out.observations.append(obs.id)
    out.summary = f"{obs.id} {summary}"
    _check_page_changes(ctx, out)


def parse_result(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    raw = ctx.scratch.pop("raw")
    out = StepOutcome(summary="")
    if "error" in raw:
        obs = st.add_observation("tool_error", f"{st.pending.label()} 失敗：{raw['error']}", raw,
                                 need_id=st.pending.need_id)
        out.summary, out.observations = obs.summary, [obs.id]
    else:
        try:
            {"serp_scan": _parse_serp, "discover": _parse_discover, "fetch": _parse_fetch,
             "history_diff": _parse_history, "backlinks": _parse_backlinks}[st.pending.tool](ctx, raw, out)
        except Exception as e:   # F3：解析失敗（例如資料格式異常）只影響這一筆結果
            obs = st.add_observation("tool_error", f"{st.pending.label()} 結果無法解析：{type(e).__name__}: {e}",
                                     {"error": f"{type(e).__name__}: {e}"}, need_id=st.pending.need_id)
            out.summary = obs.summary
            out.observations.append(obs.id)
    if st.pending.probe_id:
        probes.on_action_done(ctx, st.pending, out.observations)
        p = st.probe(st.pending.probe_id)
        out.details.append(f"{p.id} [{probes.CATALOG[p.kind].label}] 狀態：{p.status}"
                           + (f"（尚有 {len(p.pending)} 個行動）" if p.pending else ""))
    st.pending = None
    return out


# ---------- FORM_HYPOTHESES ----------

MAX_HYPOTHESIS_ROUNDS = 2
MAX_HYPOTHESES_PER_ROUND = 6   # 三個問題：On-page 幾個做法、Off-page、加上競爭的解釋
MAX_SEEK_ROUNDS = 2
MAX_VERIFY_PER_REVISE = 2
STATUS_ZH = {"proposed": "提出", "testing": "測試中", "supported": "成立", "refuted": "推翻",
             "inconclusive": "無結論", "rejected": "拒收"}
STRENGTH_ZH = {"weak": "弱", "moderate": "中", "strong": "強"}


def form_hypotheses(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    pre = StepOutcome(summary="")
    profiles.check(ctx, pre, force=True)   # 輪廓還沒寫出（例如抓取預算或候選頁用完）：以目前的資料寫出，假設才看得到
    st.hypothesis_round += 1
    st.seek_rounds = 0
    digest = observation_digest(ctx)
    refuted = [{"id": h.id, "statement": h.statement, "status": h.status, "posterior": h.posterior, "note": h.note}
               for h in st.hypotheses if h.status in ("refuted", "rejected")]
    user = hypotheses_prompt(st.task, st.goal, digest, refuted, ctx.scratch.get("hypothesis_lessons"))
    out, records, error = _call_llm(ctx, "hypothesizer", user, HypothesesOut, system=HYPOTHESIZER_SYSTEM)
    known = set(digest["known_templates"])
    details, new = list(pre.details), []
    for raw in (out.hypotheses[:MAX_HYPOTHESES_PER_ROUND] if out else []):
        fields, reason = beliefs.validate({
            "category": raw.category, "statement": raw.statement, "subject_site": raw.subject_site,
            "template": raw.template, "dimension": raw.dimension, "mechanism": raw.mechanism,
            "prediction": raw.prediction, "falsification": raw.falsification, "cited": raw.cited_observations,
            "prior": raw.prior}, st, known)
        if ctx.memory is not None and not reason:   # M1 偏置：調整某類假設的先驗
            for lesson, b in ctx.memory.biases("hypothesis_prior"):
                if b.category == fields["category"]:
                    fields["prior"] = beliefs.clamp_prior(fields["prior"] + b.delta)
                    fields["note"] = f"依經驗 {lesson.id} 先驗 {b.delta:+.2f}"
                    ctx.memory.mark(st, lesson, "hypothesis_prior")
        h = Hypothesis(id=f"H{len(st.hypotheses) + 1}", round=st.hypothesis_round, category=fields["category"],
                       statement=fields["statement"], subject_site=fields["subject_site"], template=fields["template"],
                       dimension=fields["dimension"],
                       mechanism=fields["mechanism"], prediction=fields["prediction"],
                       falsification=fields["falsification"], cited=fields["cited"], prior=fields["prior"],
                       posterior=fields["prior"], status="rejected" if reason else "testing",
                       note=reason or fields["note"])
        st.hypotheses.append(h)
        new.append(h)
        if h.status != "rejected":
            beliefs.record_point(st, h, None)
        if reason:
            details.append(f"✗ {h.id} 拒收（{reason}）：{h.statement}")
        else:
            target = strategy.label(h.dimension) if h.dimension else (h.template or "全站")
            details += [f"{h.id} [{h.category}] {target}，先驗 {h.prior:.2f}：{h.statement}",
                        f"    預測：{h.prediction}",
                        f"    反證：{h.falsification}（引用 {', '.join(h.cited)}）"]
    if out:
        details.append(f"推理摘要：{out.reasoning_summary}")
    n_ok = sum(1 for h in new if h.status == "testing")
    return StepOutcome(summary=f"第 {st.hypothesis_round} 輪：提出 {n_ok} 個可檢驗的假設（拒收 {len(new) - n_ok} 個）",
                       details=details, decision={"round": st.hypothesis_round,
                                                  "hypotheses": [h.model_dump(mode="json") for h in new]},
                       llm=records, reason=error, observations=pre.observations)


# ---------- SEEK_EVIDENCE ----------

def _catalog(kinds) -> dict:
    return {k: {"label": probes.CATALOG[k].label, "cost": probes.CATALOG[k].cost,
                "applies_to": sorted(probes.CATALOG[k].applies), "description": probes.CATALOG[k].description}
            for k in kinds}


def _evaluate_executed(ctx: EpisodeContext, details: list[str], final: bool = True) -> None:
    """判定已執行完的探針，把結果寫成證據（並記錄信心曲線的點）。

    final=False（SEEK 當下判定免費探針）：資料不足的探針保留到 REVISE 再判定，因為付費抽樣可能補齊資料（例如改版日期）。
    """
    st = ctx.state
    for p in [p for p in st.probes if p.status == "executed"]:
        drafts = probes.evaluate(ctx, p)
        label = probes.CATALOG[p.kind].label
        if not drafts and not final:
            details.append(f"{p.id} [{label}]：目前資料不足，待其他探針完成後再判定")
            continue
        p.status = "evaluated"
        if not drafts:
            details.append(f"{p.id} [{label}]：資料不足或結果不具鑑別力，不計入證據" + (f"（{p.note}）" if p.note else ""))
        for d in drafts:
            if d.direction == "none":
                details.append(f"{p.id} [{label}] → {d.hypothesis_id}：{d.note}")
                continue
            ev = EvidenceItem(id=f"EV-{len(st.evidence) + 1}", hypothesis_id=d.hypothesis_id, probe=p.kind,
                              direction=d.direction, strength=d.strength, observation_ids=d.observation_ids,
                              note=d.note, seq=st.seq + 1)
            st.evidence.append(ev)
            beliefs.record_point(st, st.hypothesis(ev.hypothesis_id), ev.id)
            details.append(f"{ev.id} → {ev.hypothesis_id} {'支持' if ev.direction == 'for' else '反對'}"
                           f"（{STRENGTH_ZH[ev.strength]}）[{label}]：{d.note}")


def seek_evidence(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    st.seek_rounds += 1
    testing = [h for h in st.hypotheses if h.status == "testing"]
    planned, details, records, error, applied = [], [], [], None, []
    for kind in probes.FREE:
        p = probes.plan(ctx, kind, testing, "system", "免費探針：只用既有觀察，一律執行")
        if p:
            planned.append(p)
    _evaluate_executed(ctx, details, final=False)   # 免費證據立即判定：付費探針的規劃者因此能看到它們
    skip = ctx.memory.biases("skip_costly_probes") if (ctx.memory and testing) else []
    if skip:   # M1 偏置：免費證據已明確分出高下時，不再花預算
        lesson, bias = skip[0]
        post = sorted((beliefs.posterior(h.prior, st.evidence_for(h.id)), h.id) for h in testing)
        top, others = post[-1], [p for p, _ in post[:-1]]
        if top[0] >= bias.threshold and all(p <= beliefs.REFUTED_AT for p in others):
            details.append(f"依經驗 {lesson.id}：免費證據已讓 {top[1]} 達 {top[0]:.2f}、其他 ≤ {beliefs.REFUTED_AT}，"
                           f"跳過付費探針")
            ctx.memory.mark(st, lesson, "seek")
            applied.append(lesson.id)
            testing = []
    if testing and ctx.ledger.fetch_room(Phase.VERIFICATION) > 0:
        budget = {"verification_fetches_left": ctx.ledger.fetch_room(Phase.VERIFICATION),
                  "api_calls_left": ctx.settings.budget.max_api_calls - st.budget.api_calls,
                  "llm_usd_left": round(ctx.ledger.llm_left(), 4)}
        out, records, error = _call_llm(ctx, "planner", evidence_prompt(hypotheses_digest(ctx), _catalog(probes.COSTLY),
                                                                         budget), ProbePlanOut, system=EVIDENCE_SYSTEM)
        if out:
            details.append(f"鑑別問題：{out.discriminating_question}")
            for req in out.probes:
                h = st.hypothesis(req.hypothesis_id)
                if h is None or h.status != "testing":
                    details.append(f"✗ 忽略 {req.probe}：{req.hypothesis_id} 不是測試中的假設")
                    continue
                p = probes.plan(ctx, req.probe, [h], "planner", req.rationale)
                if p:
                    planned.append(p)
    for p in planned:
        how = f"排入 {len(p.pending)} 個行動" if p.pending else "以既有觀察判定"
        why = f"（{p.rationale}）" if p.requested_by != "system" else ""
        details.append(f"{p.id} [{probes.CATALOG[p.kind].label}] → {', '.join(p.hypothesis_ids)}：{how}{why}")
    return StepOutcome(summary=f"規劃 {len(planned)} 個證據探針；證據佇列 {len(st.evidence_queue)} 個行動",
                       details=details, decision={"probes": [p.model_dump(mode="json") for p in planned]},
                       llm=records, reason=error, lessons_applied=applied)


# ---------- REVISE_BELIEFS ----------

MAX_SCORED_OBJECTIONS = 2   # 單次紅隊審查最多計入 2 項反駁（一中一弱），其餘只記錄：紅隊的價值在找漏洞，不在以數量壓倒證據


def _excerpt(o) -> dict:
    """給紅隊核對計算用的資料節錄（不只摘要）。"""
    d = o.data
    if o.kind in ("rank_change", "rank_control"):
        return {"aggregate": d.get("aggregate")}
    if o.kind == "serp_shift":
        return {"movement": d.get("movement", [])[:6], "target_urls_after": d.get("target_urls_after")}
    if o.kind == "site_inventory":
        return {"templates": d.get("templates")}
    if o.kind == "page_diff":
        return {k: d.get(k) for k in ("status", "snapshot_date", "current_modified", "words_change_pct", "change_types")}
    if o.kind == "page":
        return {k: d.get(k) for k in ("modified", "published", "words", "schema", "faq", "new_page")}
    if o.kind == "onpage_profile":
        return {k: d.get(k) for k in ("has_serp", "n_ranking", "n_others", "ranking_profile", "others_profile",
                                      "contrast", "adopted_in_window", "n_diffed")}
    if o.kind == "own_profile":
        return {k: d.get(k) for k in ("n", "profile")}
    if o.kind == "link_profile":
        return {k: d.get(k) for k in ("n_links", "n_ref_domains", "new_ref_domains_in_window", "baseline_in_same_length",
                                      "growth_ratio", "anchor_share", "top_targets", "share_to_ranking_pages")}
    return {}


def _red_team(ctx: EpisodeContext, h: Hypothesis) -> tuple[list[dict], list[str]]:
    st = ctx.state
    hd = next(d for d in hypotheses_digest(ctx, include_reasoning=False) if d["id"] == h.id)
    wanted = {o for e in st.evidence_for(h.id) for o in e.observation_ids} | set(h.cited)
    context_kinds = ("rank_change", "rank_control", "serp_shift", "site_inventory", "onpage_profile", "link_profile")
    observations = [{"id": o.id, "kind": o.kind, "summary": o.summary, "data": _excerpt(o)} for o in st.observations
                    if o.id in wanted or (o.kind in context_kinds and o.data.get("site", h.subject_site) == h.subject_site)]
    out, records, error = _call_llm(ctx, "verifier", verifier_prompt(hd, observations, _catalog(probes.CATALOG)),
                                    VerifierOut, system=VERIFIER_SYSTEM)
    if out is None:
        return records, [f"紅隊驗證 {h.id} 失敗：{error}（假設無法標記為成立）"]
    h.verified = True
    lines = [f"紅隊驗證 {h.id}：{ {'holds': '成立', 'weak': '薄弱', 'fails': '不成立'}[out.assessment]} — {out.summary}"]
    grounded = []
    for obj in out.objections:
        cited = [o for o in obj.cited_observations if st.observation(o)]
        if cited:
            grounded.append((obj, cited))
        else:
            lines.append(f"    ✗ 不計分（未引用有效觀察）：{obj.text}")
    grounded.sort(key=lambda x: x[0].severity != "major")   # 嚴重的優先計分
    for i, (obj, cited) in enumerate(grounded):
        if i >= MAX_SCORED_OBJECTIONS:
            lines.append(f"    · 記錄但不計分（單次審查最多計入 {MAX_SCORED_OBJECTIONS} 項）：{obj.text}")
            continue
        strength = "moderate" if i == 0 and obj.severity == "major" else "weak"
        ev = EvidenceItem(id=f"EV-{len(st.evidence) + 1}", hypothesis_id=h.id, probe="red_team", direction="against",
                          strength=strength, observation_ids=cited, note=obj.text, source="verifier", seq=st.seq + 1)
        st.evidence.append(ev)
        beliefs.record_point(st, h, ev.id)
        lines.append(f"    {ev.id} 反對（{STRENGTH_ZH[ev.strength]}）：{obj.text}（{', '.join(cited)}）")
    for name in out.requested_probes:
        if name in probes.CATALOG:
            p = probes.plan(ctx, name, [h], "verifier", "紅隊要求")
            if p:
                lines.append(f"    紅隊要求 {p.id} [{probes.CATALOG[p.kind].label}]："
                             + (f"排入 {len(p.pending)} 個行動" if p.pending else "以既有觀察判定"))
    return records, lines


def revise_beliefs(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    details, records = [], []
    _evaluate_executed(ctx, details)
    details += beliefs.refresh(st)
    for h in [h for h in st.hypotheses if h.status == "testing" and not h.verified
              and h.posterior >= beliefs.VERIFY_AT][:MAX_VERIFY_PER_REVISE]:
        recs, lines = _red_team(ctx, h)
        records += recs
        details += lines
        details += beliefs.refresh(st)
    live = [h for h in st.hypotheses if h.status != "rejected"]
    summary = "；".join(f"{h.id} {h.posterior:.2f}（{STATUS_ZH[h.status]}）" for h in live) or "沒有可檢驗的假設"
    return StepOutcome(summary=f"信心：{summary}", details=details, llm=records,
                       decision={"hypotheses": {h.id: {"posterior": h.posterior, "status": h.status} for h in live}})


# ---------- DECIDE_NEXT ----------

_HARD_STOPS = ("max_llm_usd", "max_wall_time_s", "max_actions", "max_iterations")
EXTENDABLE = ("max_llm_usd", "max_wall_time_s")   # 可請求延長；行動數與迭代數是防失控的安全網，不延長
RETRY_MARGIN = 0.25                                 # 抓取延長的重試餘裕
EXTEND_SHARE = 0.5                                 # 每次請求延長設定值的 50%


def decide_next(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    out = StepOutcome(summary="")
    must_open = lambda: [n for n in st.info_needs if n.must_answer and n.status == "open"]  # noqa: E731
    no_candidates = ctx.scratch.pop("no_candidates", False)

    if not st.hypotheses:   # 蒐集階段（Phase 2 的規則）
        if st.phase == Phase.TARGETED and len(st.fetch_yield) >= DIMINISHING_WINDOW \
                and sum(st.fetch_yield[-DIMINISHING_WINDOW:]) == 0:
            for n in st.needs_of("page_changes"):
                _resolve(n, f"最近 {DIMINISHING_WINDOW} 次抓取沒有新證據（報酬遞減）", out, "exhausted")
        if st.phase in (Phase.SAMPLING, Phase.TARGETED) and (lid := focus_satisfied(ctx)):
            # 經驗「答案範本在探勘時就有訊號」的本意是直接去檢驗，而不是換個方式繼續蒐集（Phase 6 評測發現）
            for n in [n for n in st.needs_of("page_changes") if n.status == "open"]:
                _resolve(n, f"依經驗 {lid}：訊號範本已有證據候選，改由假設檢驗蒐集有鑑別力的證據", out)
                lesson = next((x for x in ctx.memory.lessons if x.id == lid), None)
                if lesson:
                    ctx.memory.mark(st, lesson, "early_hypotheses")
                    out.lessons_applied.append(lid)
        if no_candidates:
            profiles.check(ctx, out, force=True)   # 沒有可抓的頁面了：用目前的資料寫出輪廓
            why = ("定向抓取額度已用完（剩餘額度保留給驗證）" if st.phase == Phase.VERIFICATION
                   else f"階段 {st.phase.value} 沒有可執行的行動")
            for n in [n for n in st.info_needs if n.status == "open"]:
                _resolve(n, why, out, "exhausted")

    stop = ctx.ledger.exhausted_reason()
    executed = [p for p in st.probes if p.status == "executed"]
    testing = [h for h in st.hypotheses if h.status == "testing"]
    supported = [h for h in st.hypotheses if h.status == "supported"]
    live = [h for h in st.hypotheses if h.status != "rejected"]
    paused = None
    if stop == "max_fetches" and _fetches_needed(st.evidence_queue):
        paused = _fetch_budget_request(ctx)
    elif stop in EXTENDABLE and (testing or st.evidence_queue or not st.hypotheses):
        amount = round(getattr(ctx.settings.budget, stop) * EXTEND_SHARE, 2)
        used = st.budget.llm_usd if stop == "max_llm_usd" else round(ctx.ledger.elapsed(), 1)
        reason = f"{approvals.RESOURCE_ZH[stop]}已達上限 {ctx.ledger.cap(stop):g}（已用 {used:g}），研究尚未完成"
        paused = request_budget(ctx, stop, amount, reason, {
            "used": used, "cap": ctx.ledger.cap(stop), "extend": amount,
            "testing_hypotheses": [h.id for h in testing], "evidence_queue": len(st.evidence_queue)})
    if paused is not None:
        st.decision = "continue"   # 續跑後回到選擇行動，由下一次 DECIDE_NEXT 重新評估
        return paused
    if stop in _HARD_STOPS:
        st.decision, st.end_reason = ("revise", None) if executed else ("finish", f"budget:{stop}")
        if st.decision == "finish" and (testing or not st.hypotheses):
            st.limitations.append(f"{approvals.RESOURCE_ZH.get(stop, stop)}達上限，研究在完成前結束")
    elif st.evidence_queue and stop is None:
        st.decision = "continue"
    elif executed:
        st.decision = "revise"
    elif not st.hypotheses and st.hypothesis_round == 0:
        if must_open() and not no_candidates and stop is None:
            st.decision = "continue"
        elif st.observations:
            st.decision = "form"
        else:
            st.decision, st.end_reason = "finish", "no_observations"
    elif not st.hypotheses:   # 已嘗試形成假設但沒有任何產出（例如 LLM 失敗）：有限次重試，不可無限迴圈
        if st.hypothesis_round < MAX_HYPOTHESIS_ROUNDS:
            st.decision = "form"
        else:
            st.decision, st.end_reason = "finish", "no_valid_hypotheses"
    elif testing and st.seek_rounds < MAX_SEEK_ROUNDS:
        st.decision = "seek"
    elif not supported and not testing and st.hypothesis_round < MAX_HYPOTHESIS_ROUNDS:
        st.decision = "form"   # 所有假設都被推翻或拒收：重新提出
    else:
        st.decision, st.end_reason = "finish", ("hypothesis_supported" if supported else "inconclusive")
    if st.evidence_queue and stop is not None and stop not in _HARD_STOPS:
        refused = approvals.denied(st, "exceed_budget", "max_fetches")
        out.details += _drop_queue(ctx, "抓取預算已用完" + ("（人工拒絕延長）" if refused else ""))

    labels = {"continue": "繼續", "form": "形成假設", "seek": "尋找證據", "revise": "修正推論"}
    if st.decision == "finish":
        out.summary = f"結束：{st.end_reason}"
    elif st.decision == "continue" and not st.hypotheses:
        out.summary = f"繼續（階段 {st.phase.value}；待解答 {', '.join(n.id for n in must_open())}）"
    elif st.decision == "continue":
        out.summary = f"繼續執行證據佇列（剩 {len(st.evidence_queue)} 個）"
    else:
        out.summary = f"下一步：{labels[st.decision]}"
        if st.decision == "form" and live:
            out.details.append("所有假設都已推翻或拒收，重新提出假設")
    out.decision = {"decision": st.decision, "end_reason": st.end_reason, "phase": st.phase,
                    "needs": {n.id: n.status for n in st.info_needs},
                    "hypotheses": {h.id: h.status for h in st.hypotheses}}
    return out


# ---------- REPORT ----------

def report(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    for h in st.hypotheses:
        if h.status == "testing":
            h.status = "inconclusive"
    claims = derive_claims(ctx)
    st.claims = claims
    own, own_obs = _own_values(st)
    st.actions = priority.rank(claims, own, own_obs)   # 優先行動由程式排序：影響 × 自家差距 ÷ 難度
    reasons = [] if any(c.validated for c in claims) else inconclusive_reasons(st)
    gaps = data_gaps(st.task)
    confounder = confounder_note(st)
    records, error, raw = [], None, None
    if st.goal is not None:
        out, records, error = _call_llm(ctx, "reporter", report_prompt(
            st.task, st.goal, [c.model_dump(mode="json") for c in claims], hypotheses_digest(ctx), confounder,
            actions=[a.model_dump(mode="json") for a in st.actions], reasons=reasons, gaps=gaps),
            ReportOut, system=REPORTER_SYSTEM)
        raw = out.model_dump() if out else None
    raw = raw or {"headline": "", "summary": f"（報告敘述產生失敗：{error}）", "findings": [], "excluded": [],
                  "caveats": [], "recommendations": []}
    cleaned, stats = validate_citations(raw, st, claims, st.actions)
    # 程式補上的限制（人工拒絕、預算用盡、安全事件）：不依賴 LLM 是否提到
    cleaned["caveats"] = list(cleaned.get("caveats", [])) + [f"（系統）{x}" for x in st.limitations] + \
        [f"（安全）{x}" for x in st.security_events]
    cleaned["inconclusive_reasons"], cleaned["data_gaps"] = reasons, gaps
    st.report = {**cleaned, "citations": stats, "confounder": confounder}
    if st.goal is not None:
        (ctx.run_dir / "report.md").write_text(render_markdown(st, claims, cleaned, stats, confounder), encoding="utf-8")
    b = st.budget
    (ctx.run_dir / "summary.json").write_text(json.dumps({
        "episode": st.id, "task": st.task.model_dump(mode="json"), "end_reason": st.end_reason,
        "goal": st.goal.model_dump(mode="json") if st.goal else None,
        "claims": [c.model_dump(mode="json") for c in claims],
        "actions": [a.model_dump(mode="json") for a in st.actions],
        "hypotheses": [h.model_dump(mode="json") for h in st.hypotheses],
        "evidence": [e.model_dump(mode="json") for e in st.evidence],
        "report": st.report, "info_needs": [n.model_dump(mode="json") for n in st.info_needs],
        "budget": b.model_dump(), "approvals": [a.model_dump(mode="json") for a in st.approvals],
        "limitations": st.limitations, "security_events": st.security_events,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    details = [f"{c.id} {c.subject_site}"
               + (f" {strategy.label(c.dimension)}" if c.dimension else f"{c.template_prefix}：{', '.join(c.change_types)}")
               + f"，信心 {c.confidence:.2f}，證據 {', '.join(c.evidence_ids)}{'' if c.validated else '（未通過驗證）'}"
               for c in claims]
    details += [f"{a.id} {a.action}（{a.label}；競品 {strategy.fmt(a.dimension, a.competitor)}、自家 "
                f"{strategy.fmt(a.dimension, a.own)}、難度 {a.effort}、分數 {a.score:.2f}）" for a in st.actions]
    details += [f"沒有結論的原因：{x}" for x in reasons]
    details += [f"{h.id} {STATUS_ZH[h.status]}（{h.posterior:.2f}）：{h.statement}" for h in st.hypotheses
                if h.status != "supported"]
    if confounder:
        details.append(f"混淆因子：{confounder}")
    details.append(f"引用驗證：有效 {stats['valid']}、移除無效引用 {stats['invalid_removed']}")
    details.append(f"花費：抓取 {b.fetches} 次、API {b.api_calls} 次、LLM US${b.llm_usd:.4f}、行動 {b.actions} 個")
    return StepOutcome(summary=cleaned["headline"] or f"研究結束（{st.end_reason}）", details=details, llm=records,
                       decision={"end_reason": st.end_reason, "claims": [c.model_dump(mode="json") for c in claims]},
                       reason=error)


def _own_values(st) -> tuple[dict | None, str | None]:
    """自家網站在各維度的值（On-page 來自自家輪廓、Off-page 來自自家的反向連結）。沒有自家網站時回傳 None。"""
    if not st.task.own_site:
        return None, None
    values: dict = {}
    obs = None
    prof = [o for o in st.observations if o.kind == "own_profile"]
    if prof and prof[-1].data["n"]:
        values.update(prof[-1].data["profile"])
        obs = prof[-1].id
    links = [o for o in st.observations if o.kind == "link_profile" and o.data.get("own")]
    if links:
        values.update({k: v for k, v in strategy.offpage_values(links[-1].data).items() if v is not None})
    return values, obs


# ---------- REFLECT ----------

def reflect_step(ctx: EpisodeContext) -> StepOutcome:
    from seo_intel.memory.reflect import reflect

    st = ctx.state
    if ctx.memory is None or ctx.memory.store is None:
        st.status = "done"
        return StepOutcome(summary="未啟用記憶：本次結果不寫入經驗（Day 1 模式）")
    lines, summary = reflect(ctx)
    st.reflection = {**summary, "lines": lines}
    st.status = "done"
    head = f"學習：{'成功' if summary['success'] else '未成功'}；生效經驗 {summary['active_lessons']} 條、候選 {summary['candidate_lessons']} 條"
    return StepOutcome(summary=head, details=lines[1:], decision=summary, lessons_applied=sorted(st.lessons_applied))


HANDLERS = {
    "REFLECT": reflect_step,
    "DEFINE_GOAL": define_goal,
    "PLAN_INFO_NEEDS": plan_info_needs,
    "SELECT_ACTION": select_action,
    "EXECUTE_TOOL": execute_tool,
    "PARSE_RESULT": parse_result,
    "FORM_HYPOTHESES": form_hypotheses,
    "SEEK_EVIDENCE": seek_evidence,
    "REVISE_BELIEFS": revise_beliefs,
    "DECIDE_NEXT": decide_next,
    "REPORT": report,
}
