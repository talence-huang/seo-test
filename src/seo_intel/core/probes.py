"""證據探針：取得客觀證據並以固定規則判定方向與強度（LLM 不決定證據的方向與數字）。

免費探針只使用既有觀察，一律執行；需要花費（抓取 / API）的探針由 LLM 依鑑別力挑選。
探針缺少資料時，會把所需的行動排入證據佇列，使用保留給驗證的預算。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from seo_intel.core import strategy
from seo_intel.core.context import EpisodeContext
from seo_intel.core.frontier import site_recent_share, template_key
from seo_intel.core.models import Action, Hypothesis, Observation, Phase, Probe, Strength

COMPETITOR = frozenset({"competitor_content_change", "competitor_new_pages"})
ONPAGE = frozenset({"onpage_strategy"})
OFFPAGE = frozenset({"offpage_strategy"})


@dataclass(frozen=True)
class ProbeSpec:
    label: str
    cost: str
    applies: frozenset[str]
    description: str


CATALOG: dict[str, ProbeSpec] = {
    "serp_template_share": ProbeSpec(
        "SERP 排名網址範本", "免費", COMPETITOR,
        "競品在主題查詢 SERP 中的排名網址，有多少比例屬於假設中的範本；競品若不是 SERP 上升者則反對。"),
    "template_burst": ProbeSpec(
        "範本更新爆發度", "免費", COMPETITOR,
        "範本在期間內的更新比例，相對於全站平常的更新比例；接近平常水準代表背景變動而非策略改變。"),
    "template_change_sample": ProbeSpec(
        "範本變動抽樣", "抓取最多 4 次 + 歷史快照", COMPETITOR | {"own_site_change"},
        "在假設範本中擴大抽樣並比對歷史版本，計算確實變動的頁面比例。"),
    "ranking_contrast": ProbeSpec(
        "排名頁對比", "免費", ONPAGE,
        "這個做法在競品有排名的頁面，是否比同網站其他主題頁明顯更普遍（Fisher 精確檢定；數字型為中位數檢定）；"
        "兩邊一樣普遍代表不是排名頁特有的做法。"),
    "ranking_prevalence": ProbeSpec(
        "排名頁普及率", "免費", ONPAGE,
        "競品有排名的頁面中，採用這個做法的比例；很少採用就不是它的策略。"),
    "adoption_timing": ProbeSpec(
        "採用時序", "免費", ONPAGE,
        "比對排名頁改版前後（Wayback），這個做法是不是研究期間內才加上的。"),
    "link_signal": ProbeSpec(
        "反向連結訊號", "免費", OFFPAGE,
        "從使用者提供的反向連結資料：期間內新增引用網域（相對平常速度）、錨點含關鍵字的比例、連結指向排名頁的比例。"),
}
# 排名歷史類的探針（對照組、時序、競品排名變化）需要逐日排名，真實網站拿不到
FREE = ("serp_template_share", "template_burst", "ranking_contrast", "ranking_prevalence", "adoption_timing", "link_signal")
COSTLY = ("template_change_sample",)
SAMPLE_FETCHES = 6
STRATEGIC_RATE = 0.3      # 「策略性改版」至少應涉及範本中約 30% 的主題頁


@dataclass
class Draft:
    hypothesis_id: str
    direction: str            # for / against / none（none：只說明為何不計入證據）
    strength: Strength
    observation_ids: list[str]
    note: str


def _binom_tail_ge(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def _binom_tail_le(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(0, k + 1))


# ---------- 觀察查詢 ----------

def _latest(ctx: EpisodeContext, kind: str, **match) -> Observation | None:
    found = [o for o in ctx.state.observations if o.kind == kind
             and all(o.data.get(k) == v for k, v in match.items())]
    return found[-1] if found else None


def _template_pages(ctx: EpisodeContext, tkey: str) -> list[tuple[str, dict]]:
    return [(u, i) for u, i in ctx.state.fetched.items() if i.get("template") == tkey and i.get("on_topic")]


def _page_obs_ids(ctx: EpisodeContext, urls: list[str]) -> list[str]:
    return [o.id for o in ctx.state.observations if o.url in urls and o.kind in ("page", "page_diff")]


# ---------- 規劃與排入行動 ----------

def _enqueue(ctx: EpisodeContext, probe: Probe, tool: str, args: dict, reason: str, front: bool = False) -> None:
    st = ctx.state
    a = Action(tool=tool, args=args, probe_id=probe.id, phase=Phase.VERIFICATION, score=1.0,
               breakdown={"priority": 1.0}, reason=f"{probe.id} [{CATALOG[probe.kind].label}]：{reason}")
    if a.signature in st.done_signatures or any(q.signature == a.signature for q in st.evidence_queue):
        return
    if front:
        st.evidence_queue.insert(0, a)
    else:
        st.evidence_queue.append(a)
    probe.pending.append(a.signature)


def _random_sample(ctx: EpisodeContext, tkey: str, k: int) -> list[str]:
    """範本中網址與主題相關的頁面，依網址雜湊排序取前 k 個（確定性的隨機樣本）。"""
    pool = [e.url for e in ctx.frontier.entries.values() if e.template == tkey and e.relevance >= 0.75]
    return sorted(pool, key=lambda u: hashlib.blake2b(u.encode(), digest_size=8).digest())[:k]


def applicable(kind: str, h: Hypothesis, serp_source: str) -> bool:
    if h.category not in CATALOG[kind].applies:
        return False
    if kind == "serp_template_share" and serp_source == "none":   # 沒有設定排名資料來源
        return False
    return True


def plan(ctx: EpisodeContext, kind: str, hyps: list[Hypothesis], requested_by: str, rationale: str = "") -> Probe | None:
    st = ctx.state
    hyps = [h for h in hyps if applicable(kind, h, st.task.serp_source)]
    covered = {hid for p in st.probes if p.kind == kind for hid in p.hypothesis_ids}
    hyps = [h for h in hyps if h.id not in covered]
    if not hyps:
        return None
    probe = Probe(id=f"P{len(st.probes) + 1}", kind=kind, hypothesis_ids=[h.id for h in hyps],
                  requested_by=requested_by, rationale=rationale)
    st.probes.append(probe)
    if kind == "serp_template_share":
        if not _latest(ctx, "serp_shift"):
            _enqueue(ctx, probe, "serp_scan", {"need": "probe"}, "需要這些關鍵字目前的 SERP")
    elif kind == "template_burst":
        for site in {h.subject_site for h in hyps}:
            if not _latest(ctx, "site_inventory", domain=site):
                _enqueue(ctx, probe, "discover", {"domain": site}, f"需要 {site} 的範本清單")
    elif kind == "template_change_sample":
        for h in hyps:
            # 比例估計必須用隨機樣本：frontier 偏好「最近更新」的頁面，用它來選樣會系統性高估變動率
            for url in _random_sample(ctx, h.template, SAMPLE_FETCHES):
                probe.sample.append(url)
                if url not in st.fetched:
                    _enqueue(ctx, probe, "fetch", {"url": url}, f"隨機抽樣 {h.template}")
                elif st.fetched[url].get("evidence") and not st.fetched[url].get("diffed"):
                    _enqueue(ctx, probe, "history_diff", {"url": url}, "比對樣本頁的改版前後")
    if not probe.pending:
        probe.status = "executed"
    return probe


def on_action_done(ctx: EpisodeContext, action: Action, observation_ids: list[str]) -> None:
    probe = ctx.state.probe(action.probe_id)
    if probe is None:
        return
    if action.signature in probe.pending:
        probe.pending.remove(action.signature)
    probe.observation_ids += observation_ids
    if probe.kind == "template_change_sample" and action.tool == "fetch":
        info = ctx.state.fetched.get(action.args["url"], {})
        if info.get("evidence") and not info.get("diffed"):
            _enqueue(ctx, probe, "history_diff", {"url": action.args["url"]}, "比對新抽樣頁面的改版前後", front=True)
    if not probe.pending and probe.status == "queued":
        probe.status = "executed"


def drop_action(ctx: EpisodeContext, action: Action, why: str) -> None:
    """行動無法執行（政策拒絕或預算不足）時，從探針的待辦中移除並註記。"""
    probe = ctx.state.probe(action.probe_id)
    if probe is None:
        return
    if action.signature in probe.pending:
        probe.pending.remove(action.signature)
    probe.note = (probe.note + "；" if probe.note else "") + why
    if not probe.pending and probe.status == "queued":
        probe.status = "executed"


# ---------- 判定規則 ----------

def _serp_share(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    serp = _latest(ctx, "serp_shift")
    if not serp:
        return []
    move = {m["domain"]: m for m in serp.data["movement"]}
    gain = move.get(h.subject_site, {}).get("gain", 0.0)
    urls = serp.data["target_urls_after"].get(h.subject_site, [])
    snapshot = bool(serp.data.get("snapshot_only"))   # 真實網站第一次研究：只有目前的 SERP，無從判斷「上升」
    if not snapshot and gain < 1.0:
        return [Draft(h.id, "against", "moderate", [serp.id], f"{h.subject_site} 在主題 SERP 平均名次只變化 {gain:+.1f}，不是上升者")]
    if not urls:
        return []
    inside = [u for u in urls if template_key(u) == h.template]
    share = len(inside) / len(urls)
    note = f"{h.subject_site} 的 SERP 排名網址 {len(inside)}/{len(urls)} 屬於 {h.template}" \
        + ("（目前的排名，沒有歷史可比較）" if snapshot else "")
    if share >= 0.5:
        strong = len(urls) >= 2 and not snapshot
        return [Draft(h.id, "for", "strong" if strong else "moderate", [serp.id], note)]
    if share == 0:
        return [Draft(h.id, "against", "strong" if len(urls) >= 2 else "moderate", [serp.id], note)]
    return [Draft(h.id, "for", "weak", [serp.id], note)]


def _burst(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    inv = _latest(ctx, "site_inventory", domain=h.subject_site)
    if not inv:
        return []
    rows = inv.data["templates"]
    name = h.template.split("/", 1)[1]
    row = next((r for r in rows if r["template"] == name), None)
    if row is None or not row["n_urls"]:
        return []
    site_share = site_recent_share(rows)
    if row["recent_share"] is None or site_share is None:   # 沒有日期不等於沒有更新：不計入證據
        return [Draft(h.id, "none", "weak", [inv.id], f"sitemap 沒有列出 {name} 的 lastmod：無法判斷更新爆發度，不計入證據")]
    ratio = row["recent_share"] / max(site_share, 0.01)
    note = (f"{name} 期間內更新 {row['n_recent']}/{row.get('n_dated', row['n_urls'])}（{row['recent_share']:.0%}），"
            f"全站平常 {site_share:.0%}，為 {ratio:.1f} 倍")
    if row["recent_share"] >= 0.8:
        return [Draft(h.id, "for", "strong", [inv.id], note + "：幾乎整個範本都在期間內新增或改版")]
    if ratio >= 2.0 and row["recent_share"] >= 0.15:
        return [Draft(h.id, "for", "moderate", [inv.id], note + "：明顯的集中改版")]
    if ratio < 1.3:
        return [Draft(h.id, "against", "moderate", [inv.id], note + "：與全站平常水準相當，屬背景變動")]
    return []


def _site_base_rate(ctx: EpisodeContext, site: str) -> float | None:
    inv = _latest(ctx, "site_inventory", domain=site)
    if not inv:
        return None
    return site_recent_share(inv.data["templates"])


def _page_state(info: dict | None) -> str:
    """changed / unchanged / unknown。沒有快照、無法判定的頁面不能算進分母。"""
    if not info or info.get("status") != 200:
        return "unknown"
    if info.get("change"):
        return "changed"
    if not info.get("evidence"):
        return "unchanged"           # 修改與發布日期都不在期間內
    return "unchanged" if info.get("diff_status") == "ok" else "unknown"


def _sample(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    probe = next((p for p in ctx.state.probes if p.kind == "template_change_sample" and h.id in p.hypothesis_ids), None)
    urls = [u for u in (probe.sample if probe else []) if u.startswith(f"https://{h.subject_site}/")]
    states = {u: _page_state(ctx.state.fetched.get(u)) for u in urls}
    known = [u for u, s in states.items() if s != "unknown"]
    if len(known) < 3:
        return []
    n, k = len(known), sum(1 for u in known if states[u] == "changed")
    base = _site_base_rate(ctx, h.subject_site)
    base = max(0.01, base if base is not None else 0.1)
    ids = _page_obs_ids(ctx, known)[:8] or probe.observation_ids[:8]
    # 二項檢定：比例本身不夠，要看樣本數能不能把它和背景變動率區分開
    p_above_base = _binom_tail_ge(k, n, base)        # 若只是背景變動，觀察到 ≥k 頁變動的機率
    p_below_strategy = _binom_tail_le(k, n, STRATEGIC_RATE)   # 若是策略性改版，觀察到 ≤k 頁的機率
    note = (f"{h.template} 隨機抽樣 {n} 頁（另 {len(urls) - n} 頁無法判定）中 {k} 頁在期間內顯著變動或新增"
            f"（{k / n:.0%}；全站背景約 {base:.0%}）")
    if p_above_base < 0.05 and k / n >= 0.2:
        strength = "strong" if p_above_base < 0.01 and k >= 4 else "moderate"
        return [Draft(h.id, "for", strength, ids, note + f"：顯著高於背景（p={p_above_base:.3f}）")]
    if p_below_strategy < 0.1:
        return [Draft(h.id, "against", "moderate", ids,
                      note + f"：顯著低於策略性改版應有的 {STRATEGIC_RATE:.0%}（p={p_below_strategy:.3f}）")]
    return [Draft(h.id, "none", "weak", ids, note + f"：樣本不足以區分（高於背景 p={p_above_base:.2f}、"
                                                    f"低於 {STRATEGIC_RATE:.0%} p={p_below_strategy:.2f}），不計入證據")]


def _onpage(ctx: EpisodeContext, h: Hypothesis):
    prof = _latest(ctx, "onpage_profile", site=h.subject_site)
    return prof if prof is not None and h.dimension in strategy.DIMENSIONS else None


def _contrast(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    prof = _onpage(ctx, h)
    if prof is None:
        return []
    if not prof.data["has_serp"]:
        return [Draft(h.id, "none", "weak", [prof.id], "沒有排名資料：無法比較有排名與沒排名的頁面，不計入證據")]
    row = next((r for r in prof.data["contrast"] if r["dimension"] == h.dimension), None)
    if row is None or row["n_group"] < 3 or row["n_others"] < 3:
        n = (row or {}).get("n_group", 0), (row or {}).get("n_others", 0)
        return [Draft(h.id, "none", "weak", [prof.id], f"樣本不足（排名頁 {n[0]} 頁、其他主題頁 {n[1]} 頁），不計入證據")]
    label, p_val, enough = strategy.label(h.dimension), row["p"], min(row["n_group"], row["n_others"]) >= 4
    if row["kind"] == "bool":
        diff = row["diff"]
        note = (f"{label}：排名頁 {row['group']:.0%}、同網站其他主題頁 {row['others']:.0%}（差 {diff:+.0%}，"
                f"Fisher p={p_val:.3f}，{row['n_group']} vs {row['n_others']} 頁）")
        if diff >= 0.5 and p_val < 0.01:
            return [Draft(h.id, "for", "strong", [prof.id], note + "：排名頁特有的做法")]
        if diff >= 0.3 and p_val < 0.05:
            return [Draft(h.id, "for", "moderate", [prof.id], note + "：排名頁明顯較普遍")]
        if diff >= 0.2 and p_val < 0.2:
            return [Draft(h.id, "for", "weak", [prof.id], note + "：排名頁較普遍，但樣本還不足以確定")]
        if diff <= -0.3 and p_val < 0.05:
            return [Draft(h.id, "against", "strong", [prof.id], note + "：排名頁反而較少採用")]
        if abs(diff) < 0.15 and enough:
            return [Draft(h.id, "against", "moderate", [prof.id], note + "：兩邊一樣普遍，不是排名頁特有的做法")]
        return [Draft(h.id, "none", "weak", [prof.id], note + "：差異不明確，不計入證據")]
    ratio = row["ratio"] if row["ratio"] is not None else float("inf")
    note = (f"{label}：排名頁中位數 {row['group']:g}、同網站其他主題頁 {row['others']:g}"
            f"（{'—' if ratio == float('inf') else f'{ratio:g} 倍'}，中位數檢定 p={p_val:.3f}）")
    if ratio >= 2 and p_val < 0.01:
        return [Draft(h.id, "for", "strong", [prof.id], note + "：排名頁明顯更多")]
    if ratio >= 1.5 and p_val < 0.05:
        return [Draft(h.id, "for", "moderate", [prof.id], note + "：排名頁明顯更多")]
    if ratio >= 1.3 and p_val < 0.2:
        return [Draft(h.id, "for", "weak", [prof.id], note + "：排名頁較多，但樣本還不足以確定")]
    if ratio <= 0.67 and p_val < 0.05:
        return [Draft(h.id, "against", "strong", [prof.id], note + "：排名頁反而較少")]
    if 0.8 <= ratio <= 1.25 and enough:
        return [Draft(h.id, "against", "moderate", [prof.id], note + "：兩邊差不多，不是排名頁特有的做法")]
    return [Draft(h.id, "none", "weak", [prof.id], note + "：差異不明確，不計入證據")]


def _prevalence(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    prof = _onpage(ctx, h)
    if prof is None or strategy.DIMENSIONS[h.dimension].kind != "bool":
        return []
    has_serp = prof.data["has_serp"]
    share = (prof.data["ranking_profile"] if has_serp else prof.data["others_profile"]).get(h.dimension)
    n = prof.data["n_ranking"] if has_serp else prof.data["n_others"]
    if share is None or n < 3:
        return [Draft(h.id, "none", "weak", [prof.id], f"可量測的頁面不足（{n} 頁），不計入證據")]
    who = "排名頁" if has_serp else "主題頁（沒有排名資料）"
    note = f"{strategy.label(h.dimension)}：競品{who} {n} 頁中 {share:.0%} 採用"
    weak = not has_serp   # 沒有排名資料時，普及率只能說明「競品主題頁怎麼做」，證據力較弱
    if share >= 0.8:
        return [Draft(h.id, "for", "weak" if weak else "moderate", [prof.id], note + "：普遍採用")]
    if share >= 0.6:
        return [Draft(h.id, "for", "weak", [prof.id], note + "：多數採用")]
    if share <= 0.2:
        return [Draft(h.id, "against", "moderate" if weak else "strong", [prof.id], note + "：很少採用，不是它的策略")]
    return [Draft(h.id, "none", "weak", [prof.id], note + "：一半左右，不計入證據")]


def _adoption(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    prof = _onpage(ctx, h)
    if prof is None:
        return []
    n, k = prof.data.get("n_diffed", 0), prof.data.get("adopted_in_window", {}).get(h.dimension, 0)
    label = strategy.label(h.dimension)
    if n == 0:
        return [Draft(h.id, "none", "weak", [prof.id], f"{label}：排名頁沒有可比對的歷史版本，無法判斷採用時間")]
    note = f"{label}：比對改版前後的 {n} 個排名頁中，{k} 頁在研究期間內才加上"
    if k >= 2 and k / n >= 0.4:
        return [Draft(h.id, "for", "moderate", [prof.id], note + "：期間內的策略性改版")]
    if k >= 1:
        return [Draft(h.id, "for", "weak", [prof.id], note)]
    return [Draft(h.id, "none", "weak", [prof.id], note + "：可能是長期做法（不影響它是否為策略）")]


def _link(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    lp = _latest(ctx, "link_profile", site=h.subject_site)
    if lp is None:
        return [Draft(h.id, "none", "weak", [], f"沒有 {h.subject_site} 的反向連結資料，不計入證據")]
    d, dim = lp.data, h.dimension
    if dim == "link_growth":
        growth, new = d.get("growth_ratio"), d.get("new_ref_domains_in_window", 0)
        if growth is None:
            return [Draft(h.id, "none", "weak", [lp.id], "反向連結資料沒有首次發現日期，無法判斷成長")]
        note = f"期間內新增引用網域 {new} 個，平常同長度約 {d.get('baseline_in_same_length', 0):g} 個（{growth:g} 倍）"
        if growth >= 2 and new >= 5:
            return [Draft(h.id, "for", "strong", [lp.id], note + "：明顯加速取得連結")]
        if growth >= 1.5 and new >= 3:
            return [Draft(h.id, "for", "moderate", [lp.id], note + "：連結成長加快")]
        if new == 0 or growth < 1:
            return [Draft(h.id, "against", "moderate", [lp.id], note + "：沒有比平常更積極")]
        return [Draft(h.id, "none", "weak", [lp.id], note + "：和平常差不多，不計入證據")]
    if dim == "anchor_keyword":
        share = (d.get("anchor_share") or {}).get("keyword", 0.0)
        note = f"錨點文字含關鍵字的連結占 {share:.0%}（{d.get('n_links', 0)} 條）"
        if share >= 0.5:
            return [Draft(h.id, "for", "strong", [lp.id], note + "：大量使用關鍵字錨點")]
        if share >= 0.3:
            return [Draft(h.id, "for", "moderate", [lp.id], note)]
        if share <= 0.05:
            return [Draft(h.id, "against", "moderate", [lp.id], note + "：幾乎沒有關鍵字錨點")]
        return [Draft(h.id, "none", "weak", [lp.id], note + "：不計入證據")]
    if dim == "link_to_ranking":
        share = d.get("share_to_ranking_pages")
        if share is None:
            return [Draft(h.id, "none", "weak", [lp.id], "沒有排名資料：無法判斷連結是否指向排名頁")]
        note = f"指向排名頁的連結占 {share:.0%}"
        if share >= 0.7:
            return [Draft(h.id, "for", "strong", [lp.id], note + "：外部連結集中在排名頁")]
        if share >= 0.4:
            return [Draft(h.id, "for", "moderate", [lp.id], note)]
        if share <= 0.1:
            return [Draft(h.id, "against", "moderate", [lp.id], note + "：連結並沒有指向排名頁")]
        return [Draft(h.id, "none", "weak", [lp.id], note + "：不計入證據")]
    return []


_EVAL = {"serp_template_share": _serp_share, "template_burst": _burst, "template_change_sample": _sample,
         "ranking_contrast": _contrast, "ranking_prevalence": _prevalence, "adoption_timing": _adoption,
         "link_signal": _link}


def evaluate(ctx: EpisodeContext, probe: Probe) -> list[Draft]:
    drafts: list[Draft] = []
    for hid in probe.hypothesis_ids:
        h = ctx.state.hypothesis(hid)
        if h is not None and h.status != "rejected":
            drafts += _EVAL[probe.kind](ctx, h)
    return drafts
