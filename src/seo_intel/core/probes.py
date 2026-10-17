"""證據探針：取得客觀證據並以固定規則判定方向與強度（LLM 不決定證據的方向與數字）。

免費探針只使用既有觀察，一律執行；需要花費（抓取 / API）的探針由 LLM 依鑑別力挑選。
探針缺少資料時，會把所需的行動排入證據佇列，使用保留給驗證的預算。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from seo_intel.core.context import EpisodeContext
from seo_intel.core.frontier import site_recent_share, template_key
from seo_intel.core.models import Action, Hypothesis, Observation, Phase, Probe, Strength

COMPETITOR = frozenset({"competitor_content_change", "competitor_new_pages"})


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
}
FREE = ("serp_template_share", "template_burst")   # 排名歷史類的探針（對照組、時序、競品排名變化）需要逐日排名，真實網站拿不到
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


_EVAL = {"serp_template_share": _serp_share, "template_burst": _burst, "template_change_sample": _sample}


def evaluate(ctx: EpisodeContext, probe: Probe) -> list[Draft]:
    drafts: list[Draft] = []
    for hid in probe.hypothesis_ids:
        h = ctx.state.hypothesis(hid)
        if h is not None and h.status != "rejected":
            drafts += _EVAL[probe.kind](ctx, h)
    return drafts
