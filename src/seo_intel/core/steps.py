"""研究迴圈各步驟的處理器。每個處理器更新 EpisodeState，並回傳可觀察的 StepOutcome。

LLM 只出現在 DEFINE_GOAL 與 PLAN_INFO_NEEDS（Phase 3 起加入假設、驗證與報告）；
選擇行動、解析結果、判斷下一步都是確定性程式，並把理由寫進紀錄。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, timedelta
from urllib.parse import urlsplit

from seo_intel.core.actions import COMPONENT_LABELS, compute_phase, generate
from seo_intel.core.context import EpisodeContext, StepOutcome, llm_record
from seo_intel.core.frontier import Frontier, normalize_url, template_key
from seo_intel.core.models import InfoNeed, Phase, ResearchGoal, TaskInput
from seo_intel.llm.roles import PLANNER_SYSTEM, GoalOut, PlanOut, goal_prompt, plan_prompt
from seo_intel.llm.types import LLMError
from seo_intel.tools.extract import PageFeatures
from seo_intel.tools.history import history_diff
from seo_intel.tools.rank import matches_topic, rank_scan, serp_scan, tokens
from seo_intel.tools.site import discover, fetch_page

_QUOTED = re.compile(r"「([^」]+)」")
DIMINISHING_WINDOW = 15
CHANGES_TO_RESOLVE = 3
ON_TOPIC_OVERLAP = 0.75


# ---------- 共用 ----------

def _call_llm(ctx: EpisodeContext, role: str, user: str, schema, retries: int = 1):
    records: list[dict] = []
    for attempt in range(retries + 1):
        try:
            res = ctx.llm.call(role, system=PLANNER_SYSTEM, user=user, schema=schema)
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
        return ResearchGoal(restated_question=task.question, goal_type="other",
                            topic_keywords=_fallback_keywords(task), focus_domains=task.competitors,
                            window_start=hint[0], window_end=hint[1], success_criteria=[]), notes

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
    keywords = [k.strip().lower() for k in out.topic_keywords if k.strip()][:5]
    if not keywords:
        notes.append("LLM 未提供關鍵字，改用問題中的引號詞")
        keywords = _fallback_keywords(task)
    focus = [d for d in out.focus_domains if d in task.competitors]
    if len(focus) != len(out.focus_domains):
        notes.append("已移除不在任務競品清單中的焦點網域")
    return ResearchGoal(restated_question=out.restated_question, goal_type=out.goal_type,
                        topic_keywords=keywords, focus_domains=focus or task.competitors,
                        window_start=ws, window_end=we, success_criteria=out.success_criteria), notes


def define_goal(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    out, records, error = _call_llm(ctx, "planner", goal_prompt(st.task), GoalOut)
    goal, notes = normalize_goal(out, st.task)
    st.goal = goal
    ctx.frontier = Frontier(goal.topic_keywords, (goal.window_start, goal.window_end), st.task.allowed_domains)
    ctx.frontier.set_focus(goal.focus_domains)
    details = [f"類型：{goal.goal_type}",
               f"主題關鍵字：{', '.join(goal.topic_keywords)}",
               f"研究期間：{goal.window_start} ～ {goal.window_end}",
               f"焦點網域：{', '.join(goal.focus_domains)}"]
    details += [f"成功條件：{c}" for c in goal.success_criteria]
    details += [f"⚠ {n}" for n in notes]
    return StepOutcome(summary=f"研究目標：{goal.restated_question}", details=details,
                       decision={"goal": goal.model_dump(mode="json"), "notes": notes}, llm=records,
                       reason=error)


# ---------- PLAN_INFO_NEEDS ----------

def normalize_plan(out: PlanOut | None, task: TaskInput, goal: ResearchGoal) -> tuple[list[InfoNeed], list[str]]:
    notes: list[str] = []
    own, comps = task.own_site, task.competitors
    needs: list[InfoNeed] = []
    seen: set[tuple] = set()
    for raw in (out.info_needs[:8] if out else []):
        targets = [d for d in raw.target_domains if d in task.allowed_domains]
        if raw.kind == "control_check":
            targets = [own]
        if raw.kind in ("site_inventory", "page_changes"):
            targets = [d for d in targets if d in comps]
        if not targets:
            targets = [own] if raw.kind == "rank_change" and goal.goal_type == "ranking_drop_cause" else goal.focus_domains
            notes.append(f"{raw.kind} 未指定有效網域，改用 {', '.join(targets)}")
        key = (raw.kind, tuple(targets))
        if key in seen:
            continue
        seen.add(key)
        needs.append(InfoNeed(id=f"N{len(needs) + 1}", kind=raw.kind, question=raw.question, target_domains=targets,
                              answer_criteria=raw.answer_criteria, priority=min(5, max(1, raw.priority)),
                              must_answer=raw.must_answer))
    essentials = {
        "rank_change": ([own] if goal.goal_type == "ranking_drop_cause" else goal.focus_domains, 1,
                        "目標網域在主題查詢上的排名如何變化？"),
        "page_changes": (goal.focus_domains, 3, "競品主題頁面在期間內改了什麼？"),
    }
    for kind, (targets, prio, question) in essentials.items():
        if not any(n.kind == kind for n in needs):
            needs.append(InfoNeed(id=f"N{len(needs) + 1}", kind=kind, question=question, target_domains=targets,
                                  answer_criteria="以量測資料回答", priority=prio, must_answer=True, added_by="system"))
            notes.append(f"規劃缺少 {kind}，系統補上（研究迴圈至少需要排名與頁面兩類證據）")
    return needs, notes


def plan_info_needs(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    lessons: list[str] = ctx.scratch.get("lessons", [])
    out, records, error = _call_llm(ctx, "planner", plan_prompt(st.task, st.goal, lessons), PlanOut)
    needs, notes = normalize_plan(out, st.task, st.goal)
    st.info_needs = needs
    details = [f"{n.id} [{n.kind}] 優先 {n.priority}{' 必答' if n.must_answer else ''}"
               f"{'（系統補上）' if n.added_by == 'system' else ''} {', '.join(n.target_domains)}：{n.question}"
               for n in needs]
    if out:
        details.append(f"規劃理由：{out.rationale}")
    details += [f"⚠ {n}" for n in notes]
    return StepOutcome(summary=f"規劃 {len(needs)} 項資訊需求", details=details,
                       decision={"info_needs": [n.model_dump(mode="json") for n in needs], "notes": notes},
                       llm=records, reason=error)


# ---------- SELECT_ACTION ----------

def _score_line(breakdown: dict[str, float]) -> str:
    parts = [f"{COMPONENT_LABELS.get(k, k)} {v:+.2f}" for k, v in breakdown.items() if abs(v) >= 0.005]
    return " ".join(parts)


def select_action(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
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
        st.status = "waiting_approval"
        st.approval_request = {"action": a.model_dump(mode="json"), "rule": v.rule, "reason": v.reason}
        st.pending = None
        return StepOutcome(summary=f"暫停：{a.label()} 需要人工核准", details=[f"規則 {v.rule}：{v.reason}"],
                           decision={"phase": st.phase, "approval": st.approval_request}, reason=v.reason)
    st.pending = chosen
    ctx.scratch["no_candidates"] = chosen is None
    details = []
    if st.phase != previous:
        details.append(f"進入階段 {st.phase.value}（抓取額度上限 {ctx.ledger.fetch_cap(st.phase)}）")
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
        candidates=[{"action": a.label(), "tool": a.tool, "score": a.score, "breakdown": a.breakdown,
                     "reason": a.reason} for a in cands[:5]],
    )


# ---------- EXECUTE_TOOL ----------

def _rank_obs(ctx: EpisodeContext, site: str | None = None):
    obs = [o for o in ctx.state.observations if o.kind == "rank_change" and (site is None or o.data["site"] == site)]
    return obs[-1] if obs else None


def _serp_inputs(ctx: EpisodeContext) -> tuple[list[str], date, date]:
    st = ctx.state
    ws, _ = _window(ctx)
    obs = _rank_obs(ctx, st.task.own_site) or _rank_obs(ctx)
    onset = None
    if obs and obs.data["aggregate"].get("top_movers"):
        queries = obs.data["aggregate"]["top_movers"]
        raw_onset = obs.data["aggregate"].get("onset_median")
        onset = date.fromisoformat(raw_onset) if raw_onset else None
    else:
        queries = [q for q in ctx.rank.queries(st.task.own_site) if matches_topic(q, st.goal.topic_keywords)][:3]
    before = onset - timedelta(days=10) if onset else ws + timedelta(days=7)
    after = st.task.today - timedelta(days=2)
    return queries, before, after


def _features_from_info(url: str, info: dict) -> PageFeatures:
    return PageFeatures(url=url, h2=info.get("h2", []), word_count=info.get("words", 0),
                        schema_types=info.get("schema", []), faq_count=info.get("faq", 0),
                        has_table=info.get("table", False), modified=info.get("modified"),
                        published=info.get("published"))


def execute_tool(ctx: EpisodeContext) -> StepOutcome:
    st, a = ctx.state, ctx.state.pending
    ctx.ledger.charge_action()
    st.done_signatures.append(a.signature)
    t0 = ctx.clock.now()
    summary = a.label()
    try:
        if a.tool == "rank_scan":
            raw = rank_scan(ctx.rank, a.args["site"], st.goal.topic_keywords, _window(ctx), a.args["scope"])
            summary = f"rank_scan {a.args['site']}（{a.args['scope']}）→ {raw['aggregate'].get('n_queries', 0)} 個查詢"
        elif a.tool == "serp_scan":
            queries, before, after = _serp_inputs(ctx)
            raw = serp_scan(ctx.rank, queries, before, after, st.task.competitors)
            summary = f"serp_scan {len(queries)} 個查詢 × {before} / {after}"
        elif a.tool == "discover":
            raw = discover(ctx.http, a.args["domain"])
            summary = f"discover {a.args['domain']} → {len(raw['entries']):,} 個 URL（{len(raw['sitemaps'])} 個 sitemap）"
        elif a.tool == "fetch":
            res, feats = fetch_page(ctx.http, a.args["url"])
            raw = {"result": res, "features": feats}
            status = res.status if res.status is not None else res.error
            summary = f"fetch {_short(a.args['url'])} → {status}" + ("（快取）" if res.from_cache else "")
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
    except RuntimeError as e:   # API 錯誤（例如預算用盡、參數無效）
        raw = {"error": str(e)}
        summary = f"{a.label()} → 失敗：{e}"
    ctx.scratch["raw"] = raw
    attempts = ctx.http.drain_attempts()
    waited = sum(x.waited_s for x in attempts)
    details = [f"HTTP 請求 {len(attempts)} 次，禮貌等待 {waited:.1f}s，耗時 {ctx.clock.now() - t0:.1f}s（環境時間）"]
    details += [f"重試：{_short(x.url)} 第 {x.attempt} 次 → {x.status or x.error}" for x in attempts if x.error]
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


def _parse_rank(ctx: EpisodeContext, raw: dict, out: StepOutcome) -> None:
    st, a = ctx.state, ctx.state.pending
    agg, site, scope = raw["aggregate"], raw["site"], raw["scope"]
    kind = "rank_change" if scope == "topic" else "rank_control"
    label = "主題查詢" if scope == "topic" else "非主題查詢（對照組）"
    if not agg.get("n_queries"):
        summary = f"{site} {label}：沒有可分析的查詢（與關鍵字相符的查詢 {raw['n_topic']} 個）"
    else:
        summary = (f"{site} {label} {agg['n_queries']} 個：平均排名變化 {agg['mean_delta']:+.1f}（正值＝變差），"
                   f"{agg['n_significant']} 個顯著，起始約 {agg['onset_median'] or '未偵測到'}")
    obs = st.add_observation(kind, summary, raw, need_id=a.need_id)
    out.observations.append(obs.id)
    out.summary = f"{obs.id} {summary}"
    for need in st.info_needs:
        if need.status != "open":
            continue
        if need.kind == "rank_change" and scope == "topic":
            latest = {o.data["site"]: o for o in st.observations if o.kind == "rank_change"}
            if set(need.target_domains) <= set(latest):
                parts = []
                for d in need.target_domains:
                    ag = latest[d].data["aggregate"]
                    parts.append(f"{d} {ag['mean_delta']:+.1f}（起始 {ag['onset_median'] or '—'}）"
                                 if ag.get("n_queries") else f"{d} 無資料")
                ok = any(latest[d].data["aggregate"].get("n_queries") for d in need.target_domains)
                _resolve(need, "主題查詢平均排名變化（正值＝變差）：" + "；".join(parts), out,
                         "resolved" if ok else "exhausted")
        if need.kind == "control_check" and scope == "control":
            _resolve(need, summary, out)


def _parse_serp(ctx: EpisodeContext, raw: dict, out: StepOutcome) -> None:
    st, fr = ctx.state, ctx.frontier
    st.serp_gainers = [d for d in raw["gainers"] if d in st.task.competitors]
    added = 0
    for urls in raw["target_urls_after"].values():
        for u in urls:
            added += fr.add(u, source="serp", allowed=ctx.http.allowed(u))
    fr.set_focus(list(dict.fromkeys([*st.serp_gainers, *st.goal.focus_domains])))
    top = raw["movement"][0] if raw["movement"] else None
    summary = (f"SERP 上升最多：{top['domain']}（平均名次 {top['before']} → {top['after']}）"
               if top and top["gain"] > 0 else "主題查詢的 SERP 沒有明顯上升者")
    obs = st.add_observation("serp_shift", summary, raw, need_id=st.pending.need_id)
    out.observations.append(obs.id)
    out.summary = f"{obs.id} {summary}"
    out.details.append(f"競品中的上升者：{', '.join(st.serp_gainers) or '無'}；加入 {added} 個 SERP 網址到 frontier")
    for need in st.needs_of("serp_competitors"):
        _resolve(need, summary, out)


def _parse_discover(ctx: EpisodeContext, raw: dict, out: StepOutcome) -> None:
    st, fr = ctx.state, ctx.frontier
    domain = raw["domain"]
    added = sum(fr.add(u, source="sitemap", lastmod=lm, allowed=ctx.http.allowed(u)) for u, lm in raw["entries"])
    rows = [{"template": s.key.split("/", 1)[1], "n_urls": s.n_urls, "n_recent": s.n_recent,
             "recent_share": round(s.n_recent / s.n_urls, 3) if s.n_urls else 0}
            for s in fr.stats([domain]) if s.n_sitemap]
    hot = sorted(rows, key=lambda r: (-r["recent_share"], -r["n_recent"]))[:3]
    summary = (f"{domain}：{added:,} 個 URL、{len(rows)} 個範本；期間內更新比例最高："
               + "、".join(f"{r['template']}（{r['n_recent']}/{r['n_urls']}）" for r in hot))
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
    for link, anchor in f.links:
        fr.add(link, source="link", anchor=anchor, allowed=ctx.http.allowed(link))
    entry = fr.entries.get(url)
    fr.mark_fetched(url, ok=True, evidence=evidence, blocked=False)
    info = {"status": 200, "template": tkey, "title": f.title, "words": f.word_count, "schema": f.schema_types,
            "faq": f.faq_count, "table": f.has_table, "h2": f.h2, "modified": f.modified, "published": f.published,
            "on_topic": on_topic, "evidence": evidence, "relevance": entry.relevance if entry else 0.0,
            "js_shell": f.js_shell, "malformed": f.malformed}
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
    if evidence:
        obs = st.add_observation("page", f"{_short(url)}：主題頁，{'新發布' if new_page else '修改'}於期間內",
                                 {**info, "new_page": new_page, "h2": f.sections}, need_id=st.pending.need_id, url=url)
        out.observations.append(obs.id)
    if f.suspicious_instructions:
        obs = st.add_observation("security_flag", f"{_short(url)} 含疑似對 AI 下指令的文字，已視為資料並忽略",
                                 {"url": url}, url=url)
        out.observations.append(obs.id)
        out.details.append("⚠ 偵測到頁面內嵌指令（prompt injection），僅記錄，不影響任何行動")
    if f.js_shell:
        out.details.append("JS 空殼頁：沒有可用內容")
    if f.malformed:
        out.details.append("HTML 不完整：已解析可用的部分")
    _check_page_changes(ctx, out)


def _parse_history(ctx: EpisodeContext, raw: dict, out: StepOutcome) -> None:
    st = ctx.state
    url = raw["url"]
    info = st.fetched[url]
    info["diffed"] = True
    tkey = info["template"]
    if raw["status"] == "ok":
        summary = (f"{_short(url)}：{raw['snapshot_date']} → 目前，字數 {raw['words_before']}→{raw['words_after']}"
                   f"（{raw['words_change_pct']:+.0%}）；{', '.join(raw['change_types']) or '無顯著變化'}")
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
        {"rank_scan": _parse_rank, "serp_scan": _parse_serp, "discover": _parse_discover, "fetch": _parse_fetch,
         "history_diff": _parse_history}[st.pending.tool](ctx, raw, out)
    st.pending = None
    return out


# ---------- DECIDE_NEXT ----------

def decide_next(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    out = StepOutcome(summary="")
    must_open = lambda: [n for n in st.info_needs if n.must_answer and n.status == "open"]  # noqa: E731

    if st.phase == Phase.TARGETED and len(st.fetch_yield) >= DIMINISHING_WINDOW \
            and sum(st.fetch_yield[-DIMINISHING_WINDOW:]) == 0:
        for n in st.needs_of("page_changes"):
            _resolve(n, f"最近 {DIMINISHING_WINDOW} 次抓取沒有新證據（報酬遞減）", out, "exhausted")
    if ctx.scratch.pop("no_candidates", False):
        why = ("定向抓取額度已用完（驗證額度保留給 Phase 3）" if st.phase == Phase.VERIFICATION
               else f"階段 {st.phase.value} 沒有可執行的行動")
        for n in [n for n in st.info_needs if n.status == "open"]:
            _resolve(n, why, out, "exhausted")

    if (r := ctx.ledger.exhausted_reason()) is not None:
        st.decision, st.end_reason = "finish", f"budget:{r}"
    elif not must_open():
        gaps = any(n.status == "exhausted" for n in st.info_needs if n.must_answer)
        st.decision, st.end_reason = "finish", "needs_closed_with_gaps" if gaps else "all_needs_answered"
    else:
        st.decision = "continue"
    if st.decision == "finish":
        out.summary = f"結束：{st.end_reason}"
    else:
        open_ids = ", ".join(n.id for n in must_open())
        out.summary = f"繼續（階段 {st.phase.value}；待解答 {open_ids}）"
    out.decision = {"decision": st.decision, "end_reason": st.end_reason, "phase": st.phase,
                    "needs": {n.id: n.status for n in st.info_needs}}
    return out


# ---------- REPORT（Phase 2：確定性摘要；Phase 3 改為假設與結論）----------

def report(ctx: EpisodeContext) -> StepOutcome:
    st = ctx.state
    details = [f"{n.id} [{n.kind}] {n.status}：{n.resolution or '—'}" for n in st.info_needs]
    ranked = sorted(st.template_changes.items(), key=lambda kv: -len(set(kv[1])))[:3]
    details += [f"變動範本 {tkey}：{len(set(urls))} 頁" for tkey, urls in ranked]
    b = st.budget
    details.append(f"花費：抓取 {b.fetches} 次、API {b.api_calls} 次、LLM US${b.llm_usd:.4f}、行動 {b.actions} 個")
    summary_path = ctx.run_dir / "summary.json"
    summary_path.write_text(json.dumps({
        "episode": st.id, "end_reason": st.end_reason, "goal": st.goal.model_dump(mode="json") if st.goal else None,
        "info_needs": [n.model_dump(mode="json") for n in st.info_needs],
        "template_changes": {k: sorted(set(v)) for k, v in st.template_changes.items()},
        "serp_gainers": st.serp_gainers, "budget": b.model_dump(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    st.status = "done"
    return StepOutcome(summary=f"研究摘要（結束原因：{st.end_reason}；尚未形成假設，Phase 3 加入）", details=details,
                       decision={"end_reason": st.end_reason})


HANDLERS = {
    "DEFINE_GOAL": define_goal,
    "PLAN_INFO_NEEDS": plan_info_needs,
    "SELECT_ACTION": select_action,
    "EXECUTE_TOOL": execute_tool,
    "PARSE_RESULT": parse_result,
    "DECIDE_NEXT": decide_next,
    "REPORT": report,
}
