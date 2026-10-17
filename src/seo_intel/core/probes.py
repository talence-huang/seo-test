"""證據探針：取得客觀證據並以固定規則判定方向與強度（LLM 不決定證據的方向與數字）。

免費探針只使用既有觀察，一律執行；需要花費（抓取 / API）的探針由 LLM 依鑑別力挑選。
探針缺少資料時，會把所需的行動排入證據佇列，使用保留給驗證的預算。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date

from seo_intel.core.context import EpisodeContext
from seo_intel.core.frontier import template_key
from seo_intel.core.models import Action, Hypothesis, Observation, Phase, Probe, Strength

COMPETITOR = frozenset({"competitor_content_change", "competitor_new_pages"})


@dataclass(frozen=True)
class ProbeSpec:
    label: str
    cost: str
    applies: frozenset[str]
    description: str


CATALOG: dict[str, ProbeSpec] = {
    "control_vs_topic": ProbeSpec(
        "對照組比較", "免費", COMPETITOR | {"sitewide_factor", "own_site_change"},
        "比較自家站主題查詢與非主題查詢的排名變化；兩者相近代表全站性因素，只有主題下滑代表主題層級原因。"),
    "serp_template_share": ProbeSpec(
        "SERP 排名網址範本", "免費", COMPETITOR,
        "競品在主題查詢 SERP 中的排名網址，有多少比例屬於假設中的範本；競品若不是 SERP 上升者則反對。"),
    "template_burst": ProbeSpec(
        "範本更新爆發度", "免費", COMPETITOR,
        "範本在期間內的更新比例，相對於全站平常的更新比例；接近平常水準代表背景變動而非策略改變。"),
    "timing_alignment": ProbeSpec(
        "時序吻合", "免費", COMPETITOR | {"own_site_change"},
        "範本變動的中位日期與排名變化起始日的先後；變動晚於排名變化就不可能是原因。"),
    "competitor_rank_gain": ProbeSpec(
        "競品排名變化", "API 約 9 次", COMPETITOR,
        "競品在主題查詢上的排名是否在同期明顯進步。"),
    "template_change_sample": ProbeSpec(
        "範本變動抽樣", "抓取最多 4 次 + 歷史快照", COMPETITOR | {"own_site_change"},
        "在假設範本中擴大抽樣並比對歷史版本，計算確實變動的頁面比例。"),
}
FREE = ("control_vs_topic", "serp_template_share", "template_burst", "timing_alignment")
COSTLY = ("competitor_rank_gain", "template_change_sample")
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


def applicable(kind: str, h: Hypothesis, goal_type: str) -> bool:
    if h.category not in CATALOG[kind].applies:
        return False
    if kind == "control_vs_topic" and goal_type != "ranking_drop_cause":
        return False
    return True


def plan(ctx: EpisodeContext, kind: str, hyps: list[Hypothesis], requested_by: str, rationale: str = "") -> Probe | None:
    st = ctx.state
    hyps = [h for h in hyps if applicable(kind, h, st.goal.goal_type)]
    covered = {hid for p in st.probes if p.kind == kind for hid in p.hypothesis_ids}
    hyps = [h for h in hyps if h.id not in covered]
    if not hyps:
        return None
    probe = Probe(id=f"P{len(st.probes) + 1}", kind=kind, hypothesis_ids=[h.id for h in hyps],
                  requested_by=requested_by, rationale=rationale)
    st.probes.append(probe)
    own = st.task.own_site
    if kind == "control_vs_topic":
        if not _latest(ctx, "rank_change", site=own):
            _enqueue(ctx, probe, "rank_scan", {"site": own, "scope": "topic"}, "需要自家站主題排名")
        if not _latest(ctx, "rank_control", site=own):
            _enqueue(ctx, probe, "rank_scan", {"site": own, "scope": "control"}, "需要對照組排名")
    elif kind == "serp_template_share":
        if not _latest(ctx, "serp_shift"):
            _enqueue(ctx, probe, "serp_scan", {"need": "probe"}, "需要事件前後的 SERP")
    elif kind == "template_burst":
        for site in {h.subject_site for h in hyps}:
            if not _latest(ctx, "site_inventory", domain=site):
                _enqueue(ctx, probe, "discover", {"domain": site}, f"需要 {site} 的範本清單")
    elif kind == "competitor_rank_gain":
        for site in {h.subject_site for h in hyps}:
            if not _latest(ctx, "rank_change", site=site):
                _enqueue(ctx, probe, "rank_scan", {"site": site, "scope": "topic"}, f"需要 {site} 的主題排名")
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

def _onset(ctx: EpisodeContext, site: str) -> date | None:
    o = _latest(ctx, "rank_change", site=site)
    raw = o.data["aggregate"].get("onset_median") if o else None
    return date.fromisoformat(raw) if raw else None


def _control(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    own = ctx.state.task.own_site
    topic, control = _latest(ctx, "rank_change", site=own), _latest(ctx, "rank_control", site=own)
    if not topic or not control or not topic.data["aggregate"].get("n_queries"):
        return []
    t, c = topic.data["aggregate"]["mean_delta"], control.data["aggregate"]["mean_delta"]
    if t < 1.0:
        return []
    ratio = c / t
    ids = [topic.id, control.id]
    note = f"主題查詢 {t:+.1f}、對照組 {c:+.1f}（比例 {ratio:.2f}）"
    if h.category == "sitewide_factor":
        if ratio >= 0.7:
            return [Draft(h.id, "for", "strong", ids, note + "：全站一起下滑")]
        if ratio <= 0.4:
            return [Draft(h.id, "against", "strong", ids, note + "：下滑集中在主題查詢")]
        return [Draft(h.id, "for", "weak", ids, note + "：部分為全站性")]
    if ratio <= 0.4:   # 只能證明是主題層級原因，無法區分是哪個範本 → 弱支持
        return [Draft(h.id, "for", "weak", ids, note + "：下滑集中在主題查詢")]
    if ratio >= 0.7:
        return [Draft(h.id, "against", "strong", ids, note + "：全站一起下滑，主題層級原因不足以解釋")]
    return []


def _serp_share(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    serp = _latest(ctx, "serp_shift")
    if not serp:
        return []
    move = {m["domain"]: m for m in serp.data["movement"]}
    gain = move.get(h.subject_site, {}).get("gain", 0.0)
    urls = serp.data["target_urls_after"].get(h.subject_site, [])
    if gain < 1.0:
        return [Draft(h.id, "against", "moderate", [serp.id], f"{h.subject_site} 在主題 SERP 平均名次只變化 {gain:+.1f}，不是上升者")]
    if not urls:
        return []
    inside = [u for u in urls if template_key(u) == h.template]
    share = len(inside) / len(urls)
    note = f"{h.subject_site} 的 SERP 排名網址 {len(inside)}/{len(urls)} 屬於 {h.template}"
    if share >= 0.5:
        return [Draft(h.id, "for", "strong" if len(urls) >= 2 else "moderate", [serp.id], note)]
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
    site_share = sum(r["n_recent"] for r in rows) / max(1, sum(r["n_urls"] for r in rows))
    ratio = row["recent_share"] / max(site_share, 0.01)
    note = f"{name} 期間內更新 {row['n_recent']}/{row['n_urls']}（{row['recent_share']:.0%}），全站平常 {site_share:.0%}，為 {ratio:.1f} 倍"
    if row["recent_share"] >= 0.8:
        return [Draft(h.id, "for", "strong", [inv.id], note + "：幾乎整個範本都在期間內新增或改版")]
    if ratio >= 2.0 and row["recent_share"] >= 0.15:
        return [Draft(h.id, "for", "moderate", [inv.id], note + "：明顯的集中改版")]
    if ratio < 1.3:
        return [Draft(h.id, "against", "moderate", [inv.id], note + "：與全站平常水準相當，屬背景變動")]
    return []


def _timing(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    st = ctx.state
    target = st.task.own_site if st.goal.goal_type == "ranking_drop_cause" else h.subject_site
    onset = _onset(ctx, target) or _onset(ctx, st.task.own_site)
    urls = st.template_changes.get(h.template or "", [])
    dates = []
    for u in urls:
        info = st.fetched.get(u, {})
        raw = info.get("published") if info.get("change") == "new" else info.get("modified")
        if raw:
            dates.append(date.fromisoformat(raw[:10]))
    if not onset or not dates:
        return []
    # 用「最早」的變動日，而非中位數：漸進式上線時，前幾頁就足以開始影響排名；
    # 且觀察到的最早日期只是真實最早日期的上限（我們只看到樣本）。
    first = min(dates)
    lag = (onset - first).days
    ids = _page_obs_ids(ctx, urls)[:5] + [o.id for o in st.observations if o.kind == "rank_change"][:1]
    note = f"最早觀察到的變動 {first}（共 {len(dates)} 頁），排名變化起始 {onset}，相差 {lag} 天"
    if -3 <= lag <= 45:
        return [Draft(h.id, "for", "moderate", ids, note + "：變動先於排名變化")]
    if lag < -7:
        return [Draft(h.id, "against", "strong", ids, note + "：連最早的變動都晚於排名變化，不可能是原因")]
    if lag > 60:
        return [Draft(h.id, "against", "weak", ids, note + "：變動遠早於排名變化")]
    return []


def _rank_gain(ctx: EpisodeContext, h: Hypothesis) -> list[Draft]:
    o = _latest(ctx, "rank_change", site=h.subject_site)
    if not o or not o.data["aggregate"].get("n_queries"):
        return []
    d = o.data["aggregate"]["mean_delta"]
    note = f"{h.subject_site} 主題查詢平均排名變化 {d:+.1f}（負值＝進步）"
    if d <= -1.5:
        return [Draft(h.id, "for", "moderate", [o.id], note)]
    if d > -0.5:
        return [Draft(h.id, "against", "moderate", [o.id], note + "：同期沒有進步")]
    return []


def _site_base_rate(ctx: EpisodeContext, site: str) -> float | None:
    inv = _latest(ctx, "site_inventory", domain=site)
    if not inv:
        return None
    rows = inv.data["templates"]
    return sum(r["n_recent"] for r in rows) / max(1, sum(r["n_urls"] for r in rows))


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


_EVAL = {"control_vs_topic": _control, "serp_template_share": _serp_share, "template_burst": _burst,
         "timing_alignment": _timing, "competitor_rank_gain": _rank_gain, "template_change_sample": _sample}


def evaluate(ctx: EpisodeContext, probe: Probe) -> list[Draft]:
    drafts: list[Draft] = []
    for hid in probe.hypothesis_ids:
        h = ctx.state.hypothesis(hid)
        if h is not None and h.status != "rejected":
            drafts += _EVAL[probe.kind](ctx, h)
    return drafts
