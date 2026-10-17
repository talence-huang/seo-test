"""結構化主張的推導、報告引用驗證與 Markdown 報告。

主張的每個欄位（網站、範本、變動類型、時間窗、信心）都由程式從證據推導；
LLM 只撰寫說明文字，且其中的引用 [C1] [H1] [EV-3] [O-5] 必須真實存在，否則移除並計入無效引用。
"""

from __future__ import annotations

import math
import re
from collections import Counter

from seo_intel.core.context import EpisodeContext
from seo_intel.core.models import Claim, EpisodeState
from seo_intel.tools.extract import looks_like_instructions

_REF = re.compile(r"\[(C\d+|H\d+|EV-\d+|O-\d+)\]")


def template_prefix(tkey: str) -> str:
    path = tkey.split("/", 1)[1] if "/" in tkey else ""
    first = path.split("/", 1)[0]
    return f"/{first}/" if first else "/"


def _inventory_share(st: EpisodeState, tkey: str) -> float:
    site, name = tkey.split("/", 1)
    for o in reversed(st.observations):
        if o.kind == "site_inventory" and o.data["domain"] == site:
            row = next((r for r in o.data["templates"] if r["template"] == name), None)
            return row["recent_share"] if row else 0.0
    return 0.0


def derive_claims(ctx: EpisodeContext) -> list[Claim]:
    st = ctx.state
    goal = st.goal
    claims: list[Claim] = []
    for h in st.hypotheses:
        if h.status != "supported" or not h.subject_site or not h.template:
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
_CLAIM_ZH = {"displacement_cause": "排名下滑的主要原因", "content_strategy": "競品內容策略"}


def program_headline(st: EpisodeState, claims: list[Claim]) -> str:
    """標題由程式依主張產生：報告最顯眼的一句話不交給 LLM 自由發揮。"""
    if not claims:
        return f"沒有足夠證據下結論（{st.end_reason}）"
    c = claims[0]
    more = f"，另有 {len(claims) - 1} 項主張" if len(claims) > 1 else ""
    return (f"{_CLAIM_ZH.get(c.claim_type, '結論')}：{c.subject_site} 的 {c.template_prefix} 範本"
            f"（{'、'.join(c.change_types)}），信心 {c.confidence:.2f} [{c.id}]{more}")


def validate_citations(report: dict, st: EpisodeState, claims: list[Claim]) -> tuple[dict, dict]:
    """清理 LLM 寫的報告敘述，回傳（清理後的報告, 統計）。

    - 不存在的引用移除；引用全部無效的句子 / 項目整段刪除（有引用卻沒有一個成立 = 沒有根據的說法）
    - 疑似指令文字（注入）的項目刪除；建議必須引用一個成立的主張 [C#]
    - 標題由程式依主張產生
    """
    valid = ({c.id for c in claims} | {h.id for h in st.hypotheses} | {e.id for e in st.evidence}
             | {o.id for o in st.observations})
    claim_ids = {c.id for c in claims}
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
        if any(r in claim_ids for r in _REF.findall(x)):
            recs.append(x)
        else:
            stats["uncited_recommendations"] += 1
    out = {"headline": program_headline(st, claims), "summary": clean_prose(report.get("summary", "")),
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


def render_markdown(st: EpisodeState, claims: list[Claim], report: dict, stats: dict, confounder: str | None) -> str:
    lines = [f"# {report['headline'] or st.goal.restated_question}", "",
             f"> Episode `{st.id}` · {st.task.label} · 結束原因 `{st.end_reason}`", "",
             report["summary"], "", "## 結論（程式由證據推導的結構化主張）", ""]
    if claims:
        lines += ["| 主張 | 網站 | 範本 | 變動類型 | 時間窗 | 信心 | 證據 | 驗證 |", "|---|---|---|---|---|---|---|---|"]
        for c in claims:
            window = f"{c.date_window[0]}～{c.date_window[1]}" if c.date_window else "—"
            lines.append(f"| {c.id} | {c.subject_site} | `{c.template_prefix}` | {', '.join(c.change_types)} | {window} "
                         f"| {c.confidence:.2f} | {', '.join(c.evidence_ids)} | {'✓' if c.validated else '✗'} |")
    else:
        lines.append("沒有任何假設達到「成立」門檻（信心 ≥ 0.8 且通過紅隊驗證）。")
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
