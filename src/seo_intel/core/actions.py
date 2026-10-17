"""行動候選產生與階段判定（確定性）。

資訊需求（LLM 決定）→ 對應的工具行動（程式決定）。每個候選都附分數與理由，
SELECT_ACTION 會把前幾名連同分數明細寫入 trace，讓人看得到「為什麼選這個、不選那個」。
"""

from __future__ import annotations

from seo_intel.core.context import EpisodeContext
from seo_intel.core.frontier import Entry
from seo_intel.core.models import DISCOVERY_KINDS, Action, InfoNeed, Phase

MIN_SAMPLES_PER_TEMPLATE = 3
MAX_DIFFS_PER_TEMPLATE = 5
COMPONENT_LABELS = {
    "priority": "優先序", "relevance": "相關", "template_value": "範本價值", "freshness": "新鮮度",
    "authority": "權威", "serp": "SERP", "focus": "焦點", "explore": "探索", "avoid": "避開",
    "cost": "成本", "trap": "陷阱", "dependency": "前置未完成",
}


def _base(need: InfoNeed) -> float:
    return round(1.0 - 0.1 * (need.priority - 1), 3)


def _open(ctx: EpisodeContext) -> list[InfoNeed]:
    return sorted((n for n in ctx.state.info_needs if n.status == "open"), key=lambda n: (n.priority, n.id))


def page_change_domains(ctx: EpisodeContext) -> list[str]:
    domains: list[str] = []
    for n in _open(ctx):
        if n.kind == "page_changes":
            domains += [d for d in n.target_domains if d not in domains]
    return domains


def _discovery(ctx: EpisodeContext) -> list[Action]:
    st = ctx.state
    own = st.task.own_site
    focus = set(st.goal.focus_domains) | set(st.serp_gainers)
    rank_ready = any(o.kind == "rank_change" and o.data.get("site") == own for o in st.observations)
    out: list[Action] = []
    for n in _open(ctx):
        b = _base(n)
        if n.kind == "rank_change":
            for d in n.target_domains:
                out.append(Action(tool="rank_scan", args={"site": d, "scope": "topic"}, need_id=n.id,
                                  phase=Phase.DISCOVERY, score=b, breakdown={"priority": b},
                                  reason=f"回答 {n.id}：{d} 在主題查詢上的排名變化與起始時間"))
        elif n.kind == "control_check":
            out.append(Action(tool="rank_scan", args={"site": own, "scope": "control"}, need_id=n.id,
                              phase=Phase.DISCOVERY, score=b, breakdown={"priority": b},
                              reason=f"回答 {n.id}：以非主題查詢作對照組，排除全站性因素"))
        elif n.kind == "serp_competitors":
            penalty = 0.0 if rank_ready else -0.3
            out.append(Action(tool="serp_scan", args={"need": n.id}, need_id=n.id, phase=Phase.DISCOVERY,
                              score=round(b + penalty, 3), breakdown={"priority": b, "dependency": penalty},
                              reason=f"回答 {n.id}：比較主題查詢前後的 SERP，找出取代者"
                                     + ("" if rank_ready else "（尚無排名變化資料，先用關鍵字相符的查詢）")))
        elif n.kind == "site_inventory":
            for d in n.target_domains:
                bonus = 0.05 if d in focus else 0.0
                out.append(Action(tool="discover", args={"domain": d}, need_id=n.id, phase=Phase.DISCOVERY,
                                  score=round(b + bonus, 3), breakdown={"priority": b, "focus": bonus},
                                  reason=f"回答 {n.id}：從 robots 與 sitemap 取得 {d} 的頁面範本與更新時間"))
    return out


def _fetch_reason(e: Entry, phase: Phase) -> str:
    bits = []
    if "serp" in e.sources:
        bits.append("出現在 SERP")
    if e.relevance >= 0.75:
        bits.append("網址與主題高度相關")
    if e.lastmod:
        bits.append(f"lastmod={e.lastmod}")
    label = "抽樣範本" if phase == Phase.SAMPLING else "定向抓取"
    return f"{label} {e.template}：" + ("、".join(bits) or "範本探索")


def _fetching(ctx: EpisodeContext, phase: Phase) -> list[Action]:
    st, fr = ctx.state, ctx.frontier
    domains = page_change_domains(ctx)
    if not domains or fr is None:
        return []
    need_id = next(n.id for n in _open(ctx) if n.kind == "page_changes")
    out = []
    sampling = phase == Phase.SAMPLING
    for e, score, parts in fr.best(k=6, domains=domains, under_sampled=MIN_SAMPLES_PER_TEMPLATE if sampling else None,
                                   exclude_traps=sampling):
        out.append(Action(tool="fetch", args={"url": e.url}, need_id=need_id, phase=phase, score=score,
                          breakdown=parts, reason=_fetch_reason(e, phase)))
    if phase == Phase.TARGETED:
        for url, info in st.fetched.items():
            tkey = info.get("template")
            if (info.get("evidence") and not info.get("diffed")
                    and len(st.template_changes.get(tkey, [])) < MAX_DIFFS_PER_TEMPLATE):
                score = round(0.75 + 0.25 * info.get("relevance", 0), 4)
                out.append(Action(tool="history_diff", args={"url": url}, need_id=need_id, phase=phase,
                                  score=score, breakdown={"priority": 0.75, "relevance": score - 0.75},
                                  reason=f"比對 {tkey} 頁面改版前後（修改日 {info.get('modified')}）"))
    return out


def generate(ctx: EpisodeContext, phase: Phase) -> list[Action]:
    done = set(ctx.state.done_signatures)
    cands = _discovery(ctx) if phase == Phase.DISCOVERY else \
        _fetching(ctx, phase) if phase in (Phase.SAMPLING, Phase.TARGETED) else []
    unique: dict[str, Action] = {}
    for a in cands:
        if a.signature not in done and a.signature not in unique:
            unique[a.signature] = a
    return sorted(unique.values(), key=lambda a: -a.score)


def compute_phase(ctx: EpisodeContext) -> Phase:
    st, ledger = ctx.state, ctx.ledger
    if any(n.kind in DISCOVERY_KINDS for n in _open(ctx)) and generate(ctx, Phase.DISCOVERY):
        return Phase.DISCOVERY
    if page_change_domains(ctx):
        if ledger.fetch_room(Phase.SAMPLING) > 0 and generate(ctx, Phase.SAMPLING):
            return Phase.SAMPLING
        if ledger.fetch_room(Phase.TARGETED) > 0:
            return Phase.TARGETED
    return Phase.VERIFICATION
