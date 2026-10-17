"""三種輪廓（研究目標的三個問題的證據基礎）：

- 排名頁輪廓（ranking_pages）：競品在 SERP 上有排名的頁面，和同網站其他主題頁比，哪些做法明顯更普遍；
  排名頁中研究期間前就存在的頁面，再用 Wayback 比對改版前後，判斷做法是不是期間內才採用
  （沒有排名資料時，只能描述競品主題頁的做法，報告會註明）
- 自家網站輪廓（own_site_profile）：自家主題頁在同一套維度上的狀態，用來算差距
- 反向連結輪廓（backlink_profile）：使用者提供的 CSV（競品與自家），Off-page 的證據

這裡決定「要抓哪些頁」（行動候選）與「資料是否足夠」（解答資訊需求並寫成觀察），數字一律由 core/strategy 計算。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date

from seo_intel.core import strategy
from seo_intel.core.context import EpisodeContext, StepOutcome
from seo_intel.core.frontier import normalize_url
from seo_intel.core.models import Action, InfoNeed, Phase

RANKING_MAX = 10       # 每個競品最多抓幾個排名頁
RANKING_DIFFS = 5      # 每個競品最多比對幾個排名頁的改版前後
CONTRAST_TARGET = 8    # 對比用的同網站其他主題頁
OWN_TARGET = 8         # 自家主題頁
PROFILE_KINDS = ("ranking_pages", "own_site_profile")
PRIORITY_SCORE = {"ranking": 2.0, "diff": 1.9, "contrast": 1.8, "own": 1.7}   # 高於一般抽樣：三個問題的證據優先


def _open(ctx: EpisodeContext, kind: str) -> list[InfoNeed]:
    return [n for n in ctx.state.info_needs if n.kind == kind and n.status == "open"]


def _hash(url: str) -> bytes:
    return hashlib.blake2b(url.encode(), digest_size=8).digest()


def serp_urls(ctx: EpisodeContext, domain: str) -> list[str]:
    """這個網域在目前 SERP 上的排名網址（最多 RANKING_MAX 個，依出現順序）。"""
    serp = [o for o in ctx.state.observations if o.kind == "serp_shift"]
    if not serp:
        return []
    urls = serp[-1].data.get("target_urls_after", {}).get(domain, [])
    return list(dict.fromkeys(normalize_url(u) for u in urls))[:RANKING_MAX]


def _fetched_ok(ctx: EpisodeContext, url: str) -> bool:
    info = ctx.state.fetched.get(url)
    return bool(info) and info.get("status") == 200 and not info.get("js_shell") and "strategy" in info


def _tried(ctx: EpisodeContext, url: str) -> bool:
    return url in ctx.state.fetched or any(s.startswith("fetch:") and url in s for s in ctx.state.done_signatures)


def _comparable(ctx: EpisodeContext, domain: str, exclude: set[str], templates: set[str]) -> list[str]:
    """同網站的其他主題頁：網址或錨點與關鍵字相關，或和排名頁同一個範本（同範本內的差異最能說明做法）。"""
    fr = ctx.frontier
    out = []
    for e in fr.entries.values():
        if e.domain != domain or e.url in exclude:
            continue
        stat = fr.templates.get(e.template)
        if stat is None or stat.trap or fr.template_avoid.get(e.template, 0) >= 1.0:
            continue
        if e.relevance >= 0.75 or e.template in templates:
            out.append(e.url)
    return sorted(out, key=lambda u: (-fr.entries[u].relevance, _hash(u)))


def _contrast_pool(ctx: EpisodeContext, domain: str, ranking: list[str]) -> tuple[list[str], list[str]]:
    """（已抓到的對比頁, 還可以抓的候選）。"""
    templates = {ctx.frontier.entries[u].template for u in ranking if u in ctx.frontier.entries}
    pool = _comparable(ctx, domain, set(ranking), templates)
    done = [u for u in pool if _fetched_ok(ctx, u)]
    done += [u for u, i in ctx.state.fetched.items() if u not in pool and u not in ranking and i.get("on_topic")
             and u.split("/")[2] == domain and _fetched_ok(ctx, u)]   # 從連結抓到的主題頁也算
    left = [u for u in pool if not _tried(ctx, u)]
    return list(dict.fromkeys(done)), left


def _own_pool(ctx: EpisodeContext, own: str) -> tuple[list[str], list[str]]:
    ranking = serp_urls(ctx, own)
    pool = list(dict.fromkeys(ranking + _comparable(ctx, own, set(), set())))
    if not pool:   # 網址看不出主題：退而求其次，取 frontier 中自家相關度最高的頁面
        entries = [e for e in ctx.frontier.entries.values() if e.domain == own and e.depth > 1]
        pool = [e.url for e in sorted(entries, key=lambda e: (-e.relevance, _hash(e.url)))][:OWN_TARGET * 2]
    done = [u for u in pool if _fetched_ok(ctx, u)]
    return done, [u for u in pool if not _tried(ctx, u)]


def _fetch(url: str, need: InfoNeed, phase: Phase, kind: str, reason: str, tool: str = "fetch") -> Action:
    score = PRIORITY_SCORE[kind]
    return Action(tool=tool, args={"url": url}, need_id=need.id, phase=phase, score=score,
                  breakdown={"priority": score}, reason=reason)


def _diff_targets(ctx: EpisodeContext, ranking: list[str]) -> tuple[list[str], list[str]]:
    """排名頁中研究期間前就存在的頁面：（已比對的, 還要比對的）。期間內新發布的頁面沒有「之前」可比。"""
    start = ctx.state.goal.window_start
    eligible = []
    for u in ranking:
        info = ctx.state.fetched.get(u, {})
        if not _fetched_ok(ctx, u):
            continue
        try:
            published = date.fromisoformat((info.get("published") or "")[:10])
        except ValueError:
            published = None
        if published is None or published < start:
            eligible.append(u)
    eligible = eligible[:RANKING_DIFFS]
    done = [u for u in eligible if ctx.state.fetched[u].get("diffed")]
    left = [u for u in eligible if u not in done and not any(s.startswith("history_diff:") and u in s
                                                             for s in ctx.state.done_signatures)]
    return done, left


def candidates(ctx: EpisodeContext, phase: Phase) -> list[Action]:
    """輪廓需要的抓取：先抓排名頁，再抓對比頁與自家主題頁（各有上限）。"""
    if ctx.frontier is None:
        return []
    out: list[Action] = []
    for need in _open(ctx, "ranking_pages"):
        for domain in need.target_domains:
            ranking = serp_urls(ctx, domain)
            out += [_fetch(u, need, phase, "ranking", f"回答 {need.id}：{domain} 的排名頁（SERP 上的網址），量測它採取的做法")
                    for u in ranking if not _tried(ctx, u)]
            out += [_fetch(u, need, phase, "diff", f"回答 {need.id}：比對排名頁改版前後，判斷做法是不是研究期間內才採用",
                           tool="history_diff") for u in _diff_targets(ctx, ranking)[1]]
            done, left = _contrast_pool(ctx, domain, ranking)
            if len(done) < CONTRAST_TARGET:
                what = "和排名頁對比的同網站其他主題頁" if ranking else "競品的主題頁（沒有排名資料，無法區分有沒有排名）"
                out += [_fetch(u, need, phase, "contrast", f"回答 {need.id}：{what}")
                        for u in left[:CONTRAST_TARGET - len(done)]]
    for need in _open(ctx, "own_site_profile"):
        for own in need.target_domains:
            done, left = _own_pool(ctx, own)
            if len(done) < OWN_TARGET:
                out += [_fetch(u, need, phase, "own", f"回答 {need.id}：自家 {own} 的主題頁，量測同一套做法以計算差距")
                        for u in left[:OWN_TARGET - len(done)]]
    return out


def _values(ctx: EpisodeContext, url: str) -> dict:
    entry = ctx.frontier.entries.get(url)
    return {**ctx.state.fetched[url]["strategy"], "internal_links": entry.inlinks if entry else 0}


def _top_contrasts(rows: list[dict], k: int = 4) -> list[str]:
    def strength(r):
        return abs(r["diff"]) if r["kind"] == "bool" else abs((r["ratio"] or 10) - 1) / 2

    rows = sorted((r for r in rows if r["p"] < 0.2), key=lambda r: (r["p"], -strength(r)))[:k]
    out = []
    for r in rows:
        if r["kind"] == "bool":
            out.append(f"{strategy.label(r['dimension'])}（{r['group']:.0%} vs {r['others']:.0%}，p={r['p']:.3f}）")
        else:
            out.append(f"{strategy.label(r['dimension'])}（中位數 {r['group']:g} vs {r['others']:g}，p={r['p']:.3f}）")
    return out


def _ranking_profile(ctx: EpisodeContext, need: InfoNeed, domain: str, out: StepOutcome) -> None:
    st = ctx.state
    ranking = serp_urls(ctx, domain)
    done_rank = [u for u in ranking if _fetched_ok(ctx, u)]
    others, _ = _contrast_pool(ctx, domain, ranking)
    rank_vals = [_values(ctx, u) for u in done_rank]
    other_vals = [_values(ctx, u) for u in others]
    if ranking:
        rows = [strategy.compare(rank_vals, other_vals, k) for k in strategy.PAGE_DIMENSIONS] if other_vals else []
        top = _top_contrasts(rows)
        summary = (f"{domain} 排名頁 {len(done_rank)} 頁 vs 同網站其他主題頁 {len(others)} 頁："
                   + ("差異最明顯的做法：" + "、".join(top) if top else "沒有統計上明顯的差異"))
    else:
        rows = []
        summary = f"{domain} 主題頁 {len(others)} 頁的做法（沒有排名資料：無法區分哪些頁面有排名）"
    adopted = Counter(k for u in done_rank for k in st.fetched[u].get("adopted", []))
    diffed = [u for u in done_rank if st.fetched[u].get("diffed")]
    data = {"site": domain, "has_serp": bool(ranking), "ranking_urls": done_rank, "other_urls": others,
            "n_ranking": len(done_rank), "n_others": len(others),
            "ranking_profile": strategy.profile(rank_vals), "others_profile": strategy.profile(other_vals),
            "contrast": rows, "ranking_templates": dict(Counter(ctx.frontier.entries[u].template for u in done_rank
                                                               if u in ctx.frontier.entries)),
            "adopted_in_window": dict(adopted), "n_diffed": len(diffed),
            "new_in_window": sum(1 for u in done_rank if st.fetched[u].get("change") == "new")}
    obs = st.add_observation("onpage_profile", summary, data, need_id=need.id)
    out.observations.append(obs.id)
    out.details.append(f"{obs.id} {summary}")
    if not ranking:
        out.details.append("沒有排名資料：On-page 結論只能描述競品主題頁的做法，無法說明哪些做法和排名有關")


def _own_profile(ctx: EpisodeContext, need: InfoNeed, own: str, out: StepOutcome) -> None:
    done, _ = _own_pool(ctx, own)
    vals = [_values(ctx, u) for u in done]
    prof = strategy.profile(vals)
    shown = [f"{strategy.label(k)} {v:.0%}" for k, v in prof.items()
             if strategy.DIMENSIONS[k].kind == "bool" and (not k.startswith("schema_") or k == "schema_faq")][:6]
    summary = (f"自家 {own} 主題頁 {len(done)} 頁："
               + (f"字數中位數 {prof.get('content_depth', 0):g}；" + "、".join(shown) if done else "沒有抓到可量測的主題頁"))
    data = {"site": own, "urls": done, "n": len(done), "ranking_urls": serp_urls(ctx, own), "profile": prof}
    obs = ctx.state.add_observation("own_profile", summary, data, need_id=need.id)
    out.observations.append(obs.id)
    out.details.append(f"{obs.id} {summary}")


def check(ctx: EpisodeContext, out: StepOutcome, force: bool = False) -> None:
    """資料足夠（或已經沒有可抓的頁面、force）時，寫出輪廓觀察並解答資訊需求。"""
    if ctx.frontier is None or ctx.state.goal is None:
        return
    st = ctx.state
    # 有設定排名來源時，SERP 回來之前不知道哪些是排名頁；自家網站要先探勘才知道有哪些頁面
    serp_pending = st.task.serp_source != "none" and not any(o.kind == "serp_shift" for o in st.observations)
    inventoried = {o.data["domain"] for o in st.observations if o.kind == "site_inventory"}
    for need in _open(ctx, "ranking_pages"):
        if serp_pending and not force:
            continue
        ready = []
        for domain in need.target_domains:
            ranking = serp_urls(ctx, domain)
            done, left = _contrast_pool(ctx, domain, ranking)
            pending_rank = [u for u in ranking if not _tried(ctx, u)] + _diff_targets(ctx, ranking)[1]
            ready.append(force or (not pending_rank and (len(done) >= CONTRAST_TARGET or not left)))
        if not all(ready):
            continue
        for domain in need.target_domains:
            _ranking_profile(ctx, need, domain, out)
        _resolve(need, "已量測排名頁與對比頁的做法（見輪廓觀察）", out)
    for need in _open(ctx, "own_site_profile"):
        ready = []
        for own in need.target_domains:
            done, left = _own_pool(ctx, own)
            ready.append(force or (own in inventoried and not serp_pending and (len(done) >= OWN_TARGET or not left)))
        if not all(ready):
            continue
        for own in need.target_domains:
            _own_profile(ctx, need, own, out)
        _resolve(need, "已量測自家主題頁的做法（見輪廓觀察）", out)


def link_profiles(ctx: EpisodeContext, need: InfoNeed, raw: dict, out: StepOutcome) -> None:
    """backlinks 工具的結果：每個網站一筆反向連結輪廓觀察。"""
    st = ctx.state
    for site, prof in raw["profiles"].items():
        own = site == st.task.own_site
        anchors = prof.get("anchor_share", {})
        growth = prof.get("growth_ratio")
        summary = (f"{'自家 ' if own else ''}{site} 反向連結：{prof['n_links']} 條、{prof['n_ref_domains']} 個引用網域；"
                   f"期間內新增引用網域 {prof['new_ref_domains_in_window']} 個"
                   + (f"（平常同長度約 {prof['baseline_in_same_length']:g} 個，{growth:g} 倍）" if growth is not None else "（沒有日期）")
                   + f"；錨點含關鍵字 {anchors.get('keyword', 0):.0%}"
                   + (f"；指向排名頁 {prof['share_to_ranking_pages']:.0%}" if prof.get("share_to_ranking_pages") is not None else ""))
        obs = st.add_observation("link_profile", summary, {**prof, "own": own}, need_id=need.id)
        out.observations.append(obs.id)
        out.details.append(f"{obs.id} {summary}")
    missing = [d for d in need.target_domains if d not in raw["profiles"]]
    if missing:
        out.details.append(f"反向連結資料沒有 {'、'.join(missing)}：這些網站的 Off-page 無法研究")
    _resolve(need, f"已取得 {len(raw['profiles'])} 個網站的反向連結輪廓", out)


def _resolve(need: InfoNeed, text: str, out: StepOutcome) -> None:
    if need.status == "open":
        need.status, need.resolution = "resolved", text
        out.details.append(f"{need.id} 已解答：{text}")
