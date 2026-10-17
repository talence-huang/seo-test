"""行動候選產生與階段判定（確定性）。

資訊需求（LLM 決定）→ 對應的工具行動（程式決定）。每個候選都附分數與理由，
SELECT_ACTION 會把前幾名連同分數明細寫入 trace，讓人看得到「為什麼選這個、不選那個」。
"""

from __future__ import annotations

from seo_intel.core import profiles
from seo_intel.core.context import EpisodeContext
from seo_intel.core.frontier import Entry, template_key
from seo_intel.core.models import DISCOVERY_KINDS, Action, InfoNeed, Phase

MIN_SAMPLES_PER_TEMPLATE = 3
MAX_DIFFS_PER_TEMPLATE = 5
MAX_FETCHES_PER_TEMPLATE = 15   # 定向抓取時單一範本的上限：之後的頁面多半只是重複同一種訊號
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
    focus = set(st.goal.focus_domains) | set(st.serp_gainers)
    out: list[Action] = []
    for n in _open(ctx):
        b = _base(n)
        if n.kind == "serp_competitors":
            out.append(Action(tool="serp_scan", args={"need": n.id}, need_id=n.id, phase=Phase.DISCOVERY, score=b,
                              breakdown={"priority": b}, reason=f"回答 {n.id}：這些關鍵字目前誰排在前面、排的是哪些頁面"))
        elif n.kind == "site_inventory":
            for d in n.target_domains:
                bonus = 0.05 if d in focus else 0.0
                out.append(Action(tool="discover", args={"domain": d}, need_id=n.id, phase=Phase.DISCOVERY,
                                  score=round(b + bonus, 3), breakdown={"priority": b, "focus": bonus},
                                  reason=f"回答 {n.id}：從 robots 與 sitemap 取得 {d} 的頁面範本與更新時間"))
        elif n.kind == "backlink_profile" and ctx.backlinks is not None:
            out.append(Action(tool="backlinks", args={"need": n.id, "domains": n.target_domains}, need_id=n.id,
                              phase=Phase.DISCOVERY, score=b, breakdown={"priority": b},
                              reason=f"回答 {n.id}：讀取使用者提供的反向連結資料（本機檔案，不連網）"))
        elif n.kind == "own_site_profile":
            inventoried = {o.data["domain"] for o in st.observations if o.kind == "site_inventory"}
            for d in n.target_domains:
                if d not in inventoried:
                    out.append(Action(tool="discover", args={"domain": d}, need_id=n.id, phase=Phase.DISCOVERY,
                                      score=round(b - 0.05, 3), breakdown={"priority": b, "cost": -0.05},
                                      reason=f"回答 {n.id}：取得自家 {d} 的頁面清單，之後量測主題頁"))
    return out


def _fetch_reason(ctx: EpisodeContext, e: Entry, phase: Phase) -> str:
    bits = []
    if "serp" in e.sources:
        bits.append("出現在 SERP")
    if e.relevance >= 0.75:
        bits.append("網址與主題高度相關")
    if e.lastmod:
        bits.append(f"lastmod={e.lastmod}")
    stat = ctx.frontier.templates[e.template]
    if stat.prior_source:
        bits.append(f"經驗先驗 {stat.prior_mean:.2f}（{stat.prior_source}，{stat.prior_n:.0f} 次觀察）")
    label = "抽樣範本" if phase == Phase.SAMPLING else "定向抓取"
    return f"{label} {e.template}：" + ("、".join(bits) or "範本探索")


def signal_templates(ctx: EpisodeContext) -> set[str]:
    """有訊號的範本：SERP 排名網址所屬、期間內更新爆發（≥ 2 倍）、或經驗先驗價值高。"""
    fr = ctx.frontier
    out: set[str] = set()
    serp = [o for o in ctx.state.observations if o.kind == "serp_shift"]
    if serp:
        out |= {template_key(u) for urls in serp[-1].data["target_urls_after"].values() for u in urls}
    out |= {s.key for s in fr.templates.values() if (s.burst or 0) >= 2.0 or (s.prior_mean or 0) >= 0.3}
    return out


def _fetching(ctx: EpisodeContext, phase: Phase) -> list[Action]:
    st, fr = ctx.state, ctx.frontier
    domains = page_change_domains(ctx)
    if not domains or fr is None:
        return []
    need_id = next(n.id for n in _open(ctx) if n.kind == "page_changes")
    out = []
    sampling = phase == Phase.SAMPLING
    kwargs = {"k": 6, "domains": domains, "under_sampled": MIN_SAMPLES_PER_TEMPLATE if sampling else None,
              "exclude_traps": sampling, "max_fetched": None if sampling else MAX_FETCHES_PER_TEMPLATE}
    picked, lessons = [], []
    focus = ctx.memory.biases("sampling_focus") if ctx.memory else []
    if focus and sampling:   # M1 經驗：先抽樣有訊號的範本
        signals = signal_templates(ctx)
        picked = fr.best(**kwargs, only=signals) if signals else []
        if picked:
            lessons = [focus[0][0].id]
    elif focus and focus_satisfied(ctx):
        # 已依經驗跳過普查：定向抓取不再探索沒抽樣過的範本（否則探索加分會把預算花在被跳過的範本上）
        known = signal_templates(ctx) | {k for k, s in fr.templates.items() if s.n_fetched > 0}
        picked = fr.best(**kwargs, only=known) or [(None, 0.0, {})]
        lessons = [focus[0][0].id]
    for e, score, parts in picked or fr.best(**kwargs):
        if e is None:
            break
        reason = _fetch_reason(ctx, e, phase)
        if lessons:
            reason = f"依經驗 {lessons[0]} " + ("先抽樣有訊號的範本；" if sampling else "只在已知範本內定向抓取；") + reason
        out.append(Action(tool="fetch", args={"url": e.url}, need_id=need_id, phase=phase, score=score,
                          breakdown=parts, reason=reason, lessons=lessons))
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
        profiles.candidates(ctx, phase) + _fetching(ctx, phase) if phase in (Phase.SAMPLING, Phase.TARGETED) else []
    unique: dict[str, Action] = {}
    for a in cands:
        if a.signature not in done and a.signature not in unique:
            unique[a.signature] = a
    return sorted(unique.values(), key=lambda a: -a.score)


FOCUS_EVIDENCE_TO_SKIP = 2


def focus_satisfied(ctx: EpisodeContext) -> str | None:
    """M1 經驗「先抽樣有訊號的範本」：有訊號的範本已有足夠證據候選時，跳過其餘範本的普查。

    經驗必須改變「做多少」而不只是「先做什麼」才會帶來效率（Phase 4 的實測發現）。回傳經驗 ID。
    """
    focus = ctx.memory.biases("sampling_focus") if ctx.memory else []
    if not focus:
        return None
    signals = signal_templates(ctx)
    n = sum(1 for i in ctx.state.fetched.values() if i.get("evidence") and i.get("template") in signals)
    return focus[0][0].id if n >= FOCUS_EVIDENCE_TO_SKIP else None


def compute_phase(ctx: EpisodeContext) -> Phase:
    st, ledger = ctx.state, ctx.ledger
    if any(n.kind in DISCOVERY_KINDS or n.kind == "own_site_profile" for n in _open(ctx)) \
            and generate(ctx, Phase.DISCOVERY):
        return Phase.DISCOVERY
    if any(a.signature not in st.done_signatures for a in profiles.candidates(ctx, Phase.SAMPLING)):
        for phase in (Phase.SAMPLING, Phase.TARGETED):   # 三個問題的證據：排名頁、對比頁、自家頁優先
            if ledger.fetch_room(phase) > 0:
                return phase
    if page_change_domains(ctx):
        skip_broad = focus_satisfied(ctx)
        if skip_broad and ledger.fetch_room(Phase.TARGETED) > 0 and generate(ctx, Phase.TARGETED):
            ctx.scratch["focus_skip"] = skip_broad
            return Phase.TARGETED
        if ledger.fetch_room(Phase.SAMPLING) > 0 and generate(ctx, Phase.SAMPLING):
            return Phase.SAMPLING
        if ledger.fetch_room(Phase.TARGETED) > 0:
            return Phase.TARGETED
    return Phase.VERIFICATION
