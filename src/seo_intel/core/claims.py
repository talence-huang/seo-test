"""結構化主張的推導、報告引用驗證與 Markdown 報告。

主張的每個欄位（網站、做法或範本、量測、信心）都由程式從證據推導；優先行動由 core/priority.py 排序；
LLM 只撰寫說明文字，且其中的引用 [C1] [A1] [H1] [EV-3] [O-5] 必須真實存在，否則移除並計入無效引用。
報告依研究的三個問題分章：On-page 策略、Off-page 策略、優先行動；沒有結論時，由程式說明原因與缺少的資料。
"""

from __future__ import annotations

import math
import re
from collections import Counter

from seo_intel.core import strategy
from seo_intel.core.context import EpisodeContext
from seo_intel.core.models import STRATEGY_CATEGORIES, Claim, EpisodeState, Hypothesis, Observation, PriorityAction
from seo_intel.tools.extract import looks_like_instructions

_REF = re.compile(r"\[(C\d+|A\d+|H\d+|EV-\d+|O-\d+)\]")


def template_prefix(tkey: str) -> str:
    path = tkey.split("/", 1)[1] if "/" in tkey else ""
    first = path.split("/", 1)[0]
    return f"/{first}/" if first else "/"


def _inventory_share(st: EpisodeState, tkey: str) -> float:
    site, name = tkey.split("/", 1)
    for o in reversed(st.observations):
        if o.kind == "site_inventory" and o.data["domain"] == site:
            row = next((r for r in o.data["templates"] if r["template"] == name), None)
            return (row["recent_share"] or 0.0) if row else 0.0
    return 0.0


def _latest(st: EpisodeState, kind: str, site: str) -> Observation | None:
    found = [o for o in st.observations if o.kind == kind and o.data.get("site") == site]
    return found[-1] if found else None


def _strategy_claim(st: EpisodeState, h: Hypothesis, cid: str) -> Claim:
    """策略結論：做法（維度）與程式量測的數字（排名頁普及率、對比、採用時序；或反向連結指標）。"""
    onpage = h.category == "onpage_strategy"
    template, measure, obs = h.template, {}, []
    if onpage and (prof := _latest(st, "onpage_profile", h.subject_site)):
        d = prof.data
        row = next((r for r in d["contrast"] if r["dimension"] == h.dimension), None)
        measure = {"competitor": (d["ranking_profile"] if d["has_serp"] else d["others_profile"]).get(h.dimension),
                   "others": d["others_profile"].get(h.dimension) if d["has_serp"] else None,
                   "p": row["p"] if row else None, "has_serp": d["has_serp"], "n_ranking": d["n_ranking"],
                   "n_others": d["n_others"], "adopted_in_window": d["adopted_in_window"].get(h.dimension, 0),
                   "n_diffed": d["n_diffed"]}
        if not template and d["ranking_templates"]:
            template = max(d["ranking_templates"], key=d["ranking_templates"].get)
        obs = [prof.id]
    elif not onpage and (lp := _latest(st, "link_profile", h.subject_site)):
        measure = {"competitor": strategy.offpage_values(lp.data).get(h.dimension),
                   **{k: lp.data.get(k) for k in ("n_links", "n_ref_domains", "new_ref_domains_in_window",
                                                  "baseline_in_same_length", "growth_ratio")}}
        obs = [lp.id]
    template = template or f"{h.subject_site}/"
    for_ev = [e for e in st.evidence_for(h.id) if e.direction == "for"]
    return Claim(id=cid, claim_type="onpage_strategy" if onpage else "offpage_strategy", subject_site=h.subject_site,
                 template=template, template_prefix=template_prefix(template), cluster=st.goal.topic_keywords[0],
                 change_types=[h.dimension], date_window=None, direction="competitor_gain", confidence=h.posterior,
                 hypothesis_id=h.id, evidence_ids=[e.id for e in for_ev],
                 observation_ids=sorted({o for e in for_ev for o in e.observation_ids} | set(obs)),
                 dimension=h.dimension, measure=measure)


def derive_claims(ctx: EpisodeContext) -> list[Claim]:
    st = ctx.state
    goal = st.goal
    claims: list[Claim] = []
    for h in st.hypotheses:
        if h.status != "supported" or not h.subject_site:
            continue
        if h.category in STRATEGY_CATEGORIES:
            if h.dimension:
                claims.append(_strategy_claim(st, h, f"C{len(claims) + 1}"))
            continue
        if not h.template:
            continue
        urls = sorted(set(st.template_changes.get(h.template, [])))
        diff_types = Counter()
        n_diffs = 0
        for o in st.observations:
            if o.kind == "page_diff" and o.url in urls and o.data.get("status") == "ok":
                n_diffs += 1
                diff_types.update(o.data.get("change_types", []))
        change_types = [t for t, c in diff_types.most_common() if c >= max(1, math.ceil(n_diffs / 2))]
        n_new = sum(1 for u in urls if st.fetched.get(u, {}).get("change") == "new")
        if n_new >= 2 and n_new >= len(urls) / 2:
            change_types.append("new_template_launch" if _inventory_share(st, h.template) >= 0.8 else "bulk_pages")
        dates = sorted(d[:10] for u in urls for d in [st.fetched.get(u, {}).get(
            "published" if st.fetched.get(u, {}).get("change") == "new" else "modified")] if d)
        for_ev = [e for e in st.evidence_for(h.id) if e.direction == "for"]
        claims.append(Claim(
            id=f"C{len(claims) + 1}",
            claim_type="displacement_cause" if goal.goal_type == "ranking_drop_cause" else "content_strategy",
            subject_site=h.subject_site, template=h.template, template_prefix=template_prefix(h.template),
            cluster=goal.topic_keywords[0], change_types=change_types or ["unspecified"],
            date_window=(dates[0], dates[-1]) if dates else None,
            direction="own_loss" if goal.goal_type == "ranking_drop_cause" else "competitor_gain",
            confidence=h.posterior, hypothesis_id=h.id, evidence_ids=[e.id for e in for_ev],
            observation_ids=sorted({o for e in for_ev for o in e.observation_ids}),
        ))
    for c in claims:
        c.validated = _validate_claim(st, c)
    return claims


def _validate_claim(st: EpisodeState, c: Claim) -> bool:
    ev_ids = {e.id for e in st.evidence}
    if not c.evidence_ids or any(e not in ev_ids for e in c.evidence_ids):
        return False
    if any(st.observation(o) is None for o in c.observation_ids):
        return False
    return any(e.strength in ("moderate", "strong") for e in st.evidence if e.id in c.evidence_ids)


def confounder_note(st: EpisodeState) -> str | None:
    own = st.task.own_site
    ctrl = [o for o in st.observations if o.kind == "rank_control" and o.data["site"] == own]
    if not ctrl:
        return None
    ag = ctrl[-1].data["aggregate"]
    if not ag.get("n_queries") or ag["mean_delta"] < 0.5:
        return None
    return f"自家站非主題查詢同期平均變差 {ag['mean_delta']:+.1f} 名（起始約 {ag['onset_median'] or '—'}），屬全站性波動 [{ctrl[-1].id}]"


_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?\s*|\n+")   # 不以半形句點斷句：網域與小數都含句點
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([。，、；：！？）])")
_CLAIM_ZH = {"displacement_cause": "排名下滑的主要原因", "content_strategy": "競品內容策略",
             "onpage_strategy": "On-page 策略", "offpage_strategy": "Off-page 策略"}
_STATUS_ZH = {"do": "建議執行", "maintain": "已做到，維持", "unknown": "無法比較（沒有自家資料）"}
_END_ZH = {"no_valid_hypotheses": "沒有提出可以檢驗的假設（LLM 沒有產出，或產出的假設沒有可以檢驗的資料）",
           "inconclusive": "提出的假設都沒有達到成立門檻（信心 ≥ 0.8 且通過紅隊驗證）",
           "no_observations": "沒有取得任何觀察"}


def program_headline(st: EpisodeState, claims: list[Claim], actions: list[PriorityAction] = ()) -> str:
    """標題由程式依主張產生：報告最顯眼的一句話不交給 LLM 自由發揮。"""
    if not claims:
        return f"沒有足夠證據下結論（{st.end_reason}）"
    strat = [c for c in claims if c.claim_type in ("onpage_strategy", "offpage_strategy")]
    if strat:
        labels = "、".join(strategy.label(c.dimension) for c in strat[:3]) + (f"等 {len(strat)} 項" if len(strat) > 3 else "")
        do = next((a for a in actions if a.status == "do"), None)
        return (f"{strat[0].subject_site} 的關鍵策略：{labels}" + (f"；最優先的行動：{do.action} [{do.id}]" if do else ""))
    c = claims[0]
    more = f"，另有 {len(claims) - 1} 項主張" if len(claims) > 1 else ""
    return (f"{_CLAIM_ZH.get(c.claim_type, '結論')}：{c.subject_site} 的 {c.template_prefix} 範本"
            f"（{'、'.join(c.change_types)}），信心 {c.confidence:.2f} [{c.id}]{more}")


def validate_citations(report: dict, st: EpisodeState, claims: list[Claim],
                       actions: list[PriorityAction] = ()) -> tuple[dict, dict]:
    """清理 LLM 寫的報告敘述，回傳（清理後的報告, 統計）。

    - 不存在的引用移除；引用全部無效的句子 / 項目整段刪除（有引用卻沒有一個成立 = 沒有根據的說法）
    - 疑似指令文字（注入）的項目刪除；建議必須引用一個成立的主張 [C#] 或優先行動 [A#]
    - 標題由程式依主張產生
    """
    valid = ({c.id for c in claims} | {a.id for a in actions} | {h.id for h in st.hypotheses}
             | {e.id for e in st.evidence} | {o.id for o in st.observations})
    claim_ids = {c.id for c in claims}
    grounds = claim_ids | {a.id for a in actions}
    hyp_ids = {h.id for h in st.hypotheses}
    stats = {"valid": 0, "invalid_removed": 0, "dropped_findings": 0, "dropped_excluded": 0, "ungrounded_removed": 0,
             "instructions_removed": 0, "uncited_recommendations": 0}

    def clean(text: str) -> str | None:
        """回傳清理後文字；整段應刪除時回傳 None。"""
        if looks_like_instructions(text):
            stats["instructions_removed"] += 1
            return None
        refs = _REF.findall(text)
        good = [r for r in refs if r in valid]
        stats["valid"] += len(good)
        stats["invalid_removed"] += len(refs) - len(good)
        if refs and not good:
            stats["ungrounded_removed"] += 1
            return None
        text = _REF.sub(lambda m: m.group(0) if m.group(1) in valid else "", text)
        return _SPACE_BEFORE_PUNCT.sub(r"\1", re.sub(r"[ \t]{2,}", " ", text))

    def clean_prose(text: str) -> str:
        return "".join(p if not p.strip() else (clean(p) or "") for p in _SENTENCE.findall(text)).strip()

    def clean_list(items: list[str]) -> list[str]:
        return [c for c in (clean(x) for x in items) if c]

    recs = []
    for x in clean_list(report.get("recommendations", [])):
        if any(r in grounds for r in _REF.findall(x)):
            recs.append(x)
        else:
            stats["uncited_recommendations"] += 1
    out = {"headline": program_headline(st, claims, actions), "summary": clean_prose(report.get("summary", "")),
           "findings": [], "excluded": [], "caveats": clean_list(report.get("caveats", [])), "recommendations": recs}
    for f in report.get("findings", []):
        text = clean(f["explanation"]) if f["claim_id"] in claim_ids else None
        if text:
            out["findings"].append({"claim_id": f["claim_id"], "explanation": text})
        else:
            stats["dropped_findings"] += 1
    for x in report.get("excluded", []):
        text = clean(x["reason"]) if x["hypothesis_id"] in hyp_ids else None
        if text:
            out["excluded"].append({"hypothesis_id": x["hypothesis_id"], "reason": text})
        else:
            stats["dropped_excluded"] += 1
    return out, stats


def inconclusive_reasons(st: EpisodeState) -> list[str]:
    """沒有成立的結論時，由程式說明原因：結束原因、抓到多少主題頁、輪廓量到什麼、最接近成立的假設卡在哪裡。"""
    reasons = []
    end = st.end_reason or ""
    reasons.append(_END_ZH.get(end) or (f"預算用盡（{end.split(':', 1)[1]}）" if end.startswith("budget:") else f"結束原因：{end}"))
    pages = [i for i in st.fetched.values() if i.get("status") == 200]
    on_topic = sum(1 for i in pages if i.get("on_topic"))
    reasons.append(f"抓取 {len(pages)} 頁，其中 {on_topic} 頁的標題含有關鍵字"
                   + ("：主題頁太少，能檢驗的證據不足" if on_topic < 3 else ""))
    for prof in (o for o in st.observations if o.kind == "onpage_profile"):
        d = prof.data
        if not d["has_serp"]:
            reasons.append(f"{d['site']}：沒有排名資料，無法比較有排名與沒排名的頁面，On-page 結論只能描述主題頁 [{prof.id}]")
        elif d["n_ranking"] == 0:
            reasons.append(f"{d['site']}：SERP 上沒有它的排名頁，或排名頁抓不到 [{prof.id}]")
        elif not any(r["p"] < 0.05 for r in d["contrast"]):
            reasons.append(f"{d['site']}：排名頁（{d['n_ranking']} 頁）和其他主題頁（{d['n_others']} 頁）在各項做法上"
                           f"沒有統計上明顯的差異 [{prof.id}]")
    if not any(o.kind == "onpage_profile" for o in st.observations):
        reasons.append("沒有建立排名頁輪廓（找不到可以量測的競品主題頁）")
    live = [h for h in st.hypotheses if h.status not in ("rejected", "supported")]
    if live:
        best = max(live, key=lambda h: h.posterior)
        against = sorted((e for e in st.evidence_for(best.id) if e.direction == "against"),
                         key=lambda e: {"strong": 0, "moderate": 1, "weak": 2}[e.strength])
        reasons.append(f"最接近成立的是 {best.id}（信心 {best.posterior:.2f}，門檻 0.8）：{best.statement[:60]}"
                       + (f"；主要反對：{against[0].note[:80]} [{against[0].id}]" if against else ""))
    rejected = [h for h in st.hypotheses if h.status == "rejected"]
    if rejected and not live:
        reasons.append(f"{len(rejected)} 個假設被拒收（例如：{rejected[0].note or '沒有引用有效的觀察'}）")
    return reasons


def _strategy_rows(claims: list[Claim]) -> list[str]:
    rows = ["| 結論 | 競品 | 做法 | 排名頁 | 同網站其他主題頁 | 期間內才採用 | 信心 | 證據 |", "|---|---|---|---|---|---|---|---|"]
    for c in claims:
        m, dim = c.measure, c.dimension
        topic = "" if m.get("has_serp", True) else "（主題頁）"
        adopted = f"{m.get('adopted_in_window', 0)}/{m['n_diffed']} 頁" if m.get("n_diffed") else "—"
        rows.append(f"| {c.id} | {c.subject_site} | {strategy.label(dim)} | {strategy.fmt(dim, m.get('competitor'))}{topic} "
                    f"| {strategy.fmt(dim, m.get('others'))} | {adopted} | {c.confidence:.2f} | {', '.join(c.evidence_ids)} |")
    return rows


def render_markdown(st: EpisodeState, claims: list[Claim], report: dict, stats: dict, confounder: str | None) -> str:
    on = [c for c in claims if c.claim_type == "onpage_strategy"]
    off = [c for c in claims if c.claim_type == "offpage_strategy"]
    content = [c for c in claims if c.claim_type in ("content_strategy", "displacement_cause")]
    lines = [f"# {report['headline'] or st.goal.restated_question}", "",
             f"> Episode `{st.id}` · {st.task.label} · 結束原因 `{st.end_reason}`", "",
             f"**研究問題**：{st.task.question}", "", report["summary"], "", "## 1. On-page 策略", ""]
    if on:
        lines += _strategy_rows(on)
    if content:
        lines += ["", "內容改版（研究期間內的變化）：", "",
                  "| 結論 | 網站 | 範本 | 變動類型 | 時間窗 | 信心 | 證據 |", "|---|---|---|---|---|---|---|"]
        for c in content:
            window = f"{c.date_window[0]}～{c.date_window[1]}" if c.date_window else "—"
            lines.append(f"| {c.id} | {c.subject_site} | `{c.template_prefix}` | {', '.join(c.change_types)} | {window} "
                         f"| {c.confidence:.2f} | {', '.join(c.evidence_ids)} |")
    if not on and not content:
        lines.append("沒有成立的 On-page 策略結論（信心 ≥ 0.8 且通過紅隊驗證）。")
    lines += ["", "## 2. Off-page 策略", ""]
    if off:
        lines += ["| 結論 | 競品 | 做法 | 量測 | 新增引用網域 | 信心 | 證據 |", "|---|---|---|---|---|---|---|"]
        for c in off:
            m = c.measure
            lines.append(f"| {c.id} | {c.subject_site} | {strategy.label(c.dimension)} | {strategy.fmt(c.dimension, m.get('competitor'))} "
                         f"| {m.get('new_ref_domains_in_window', '—')}（平常約 {m.get('baseline_in_same_length', '—')}） "
                         f"| {c.confidence:.2f} | {', '.join(c.evidence_ids)} |")
    elif st.task.backlink_source == "none":
        lines.append("沒有提供反向連結資料（Ahrefs、Semrush 匯出的 CSV），無法研究 Off-page 策略。")
    else:
        lines.append("沒有成立的 Off-page 策略結論。")
    lines += ["", "## 3. 優先行動", ""]
    if st.actions:
        lines += ["分數 = 影響（結論信心 × 競品排名頁的普及率）× 自家差距 ÷ 實作難度；由程式計算。", "",
                  "| 順序 | 行動 | 做法 | 競品 | 自家 | 差距 | 難度 | 分數 | 狀態 | 依據 |", "|---|---|---|---|---|---|---|---|---|---|"]
        for a in st.actions:
            gap = "—" if a.gap is None else f"{a.gap:.0%}"
            lines.append(f"| {a.id} | {a.action} | {a.label} | {strategy.fmt(a.dimension, a.competitor)} "
                         f"| {strategy.fmt(a.dimension, a.own)} | {gap} | {'低中高'[a.effort - 1]} | {a.score:.2f} "
                         f"| {_STATUS_ZH[a.status]} | {a.claim_id} |")
        if not st.task.own_site:
            lines += ["", "沒有提供自家網址：差距以 50% 估計，排序只反映競品的做法。"]
    else:
        lines.append("沒有成立的策略結論，無法排出優先行動。")
    if report.get("inconclusive_reasons"):
        lines += ["", "## 為什麼沒有結論", ""] + [f"- {x}" for x in report["inconclusive_reasons"]]
    if report.get("data_gaps"):
        lines += ["", "## 資料缺口", ""] + [f"- {x}" for x in report["data_gaps"]]
    if confounder:
        lines += ["", f"**混淆因子**：{confounder}"]
    if report["findings"]:
        lines += ["", "## 說明", ""] + [f"- **{f['claim_id']}**：{f['explanation']}" for f in report["findings"]]
    if report["excluded"]:
        lines += ["", "## 已排除或未成立的假設", ""] + [f"- **{x['hypothesis_id']}**：{x['reason']}" for x in report["excluded"]]
    lines += ["", "## 假設與信心", "", "| 假設 | 類別 | 範本 | 先驗 → 信心 | 狀態 |", "|---|---|---|---|---|"]
    for h in st.hypotheses:
        lines.append(f"| {h.id} | {h.category} | {h.template or '—'} | {h.prior:.2f} → {h.posterior:.2f} | {h.status} |")
    lines += ["", "## 證據", "", "| 證據 | 假設 | 探針 | 方向 | 強度 | 觀察 | 說明 |", "|---|---|---|---|---|---|---|"]
    for e in st.evidence:
        lines.append(f"| {e.id} | {e.hypothesis_id} | {e.probe} | {e.direction} | {e.strength} "
                     f"| {', '.join(e.observation_ids)} | {e.note} |")
    if report["caveats"]:
        lines += ["", "## 限制", ""] + [f"- {x}" for x in report["caveats"]]
    if report["recommendations"]:
        lines += ["", "## 建議", ""] + [f"- {x}" for x in report["recommendations"]]
    lines += ["", f"_引用驗證：有效 {stats['valid']}、移除無效引用 {stats['invalid_removed']}、"
                  f"移除不存在的主張 {stats['dropped_findings']}、移除不存在的假設 {stats['dropped_excluded']}_", ""]
    return "\n".join(lines)
