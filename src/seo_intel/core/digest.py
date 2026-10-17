"""把觀察整理成給 LLM 的精簡摘要：LLM 看到的是程式算好的數字，而不是原始資料。"""

from __future__ import annotations

from collections import Counter

from seo_intel.core import strategy
from seo_intel.core.context import EpisodeContext
from seo_intel.core.frontier import site_recent_share, template_key

PAGE_OBS_LIMIT = 25


def observation_digest(ctx: EpisodeContext) -> dict:
    st, fr = ctx.state, ctx.frontier
    rank = [{"obs": o.id, "site": o.data["site"], "scope": o.data["scope"],
             **{k: o.data["aggregate"].get(k) for k in ("n_queries", "mean_delta", "n_significant", "onset_median")}}
            for o in st.observations if o.kind in ("rank_change", "rank_control")]
    serp = None
    serp_obs = [o for o in st.observations if o.kind == "serp_shift"]
    if serp_obs:
        o = serp_obs[-1]
        serp = {"obs": o.id, "before": o.data["before"], "after": o.data["after"], "queries": o.data["queries"],
                "movement": o.data["movement"][:6],
                "ranking_url_templates": {site: dict(Counter(template_key(u) for u in urls))
                                          for site, urls in o.data["target_urls_after"].items()}}
    inventory = []
    for o in st.observations:
        if o.kind != "site_inventory":
            continue
        rows = o.data["templates"]
        share = site_recent_share(rows)
        inventory.append({"obs": o.id, "site": o.data["domain"],
                          "site_recent_share": round(share, 3) if share is not None else None,
                          "templates": sorted(rows, key=lambda r: -(r["recent_share"] or 0))[:8]})
    findings = []
    seen = set()
    for tkey, urls in st.template_changes.items():
        urls = sorted(set(urls))
        seen.add(tkey)
        types = Counter()
        for ob in st.observations:
            if ob.kind == "page_diff" and ob.url in urls:
                types.update(ob.data.get("change_types", []))
        findings.append({"template": tkey, "changed_pages": len(urls),
                         "new_pages": sum(1 for u in urls if st.fetched.get(u, {}).get("change") == "new"),
                         "diff_change_types": dict(types),
                         "observations": [ob.id for ob in st.observations if ob.url in urls][:5]})
    checked = Counter(i["template"] for i in st.fetched.values() if i.get("on_topic"))
    for tkey, n in checked.items():
        if tkey not in seen and n >= 2:
            findings.append({"template": tkey, "changed_pages": 0, "on_topic_pages_checked": n})
    general = [{"id": o.id, "kind": o.kind, "summary": o.summary} for o in st.observations
               if o.kind not in ("page", "page_diff")]
    profiles = strategy_profiles(ctx)
    pages = [{"id": o.id, "kind": o.kind, "summary": o.summary} for o in st.observations
             if o.kind in ("page", "page_diff")][-PAGE_OBS_LIMIT:]
    known = sorted(t.key for t in fr.templates.values()
                   if t.domain in st.task.allowed_domains and not t.trap and (t.n_sitemap or t.n_urls >= 3))
    return {"rank": rank, "serp": serp, "inventory": inventory, "template_findings": findings,
            "strategy_profiles": profiles, "strategy_dimensions": {k: d.label for k, d in strategy.DIMENSIONS.items()},
            "observations": general + pages, "known_templates": known}


def strategy_profiles(ctx: EpisodeContext) -> list[dict]:
    """三種輪廓的精簡版：排名頁 vs 其他主題頁的對比、自家主題頁、反向連結（數字都由程式算好）。"""
    out = []
    for o in ctx.state.observations:
        d = o.data
        if o.kind == "onpage_profile":
            contrast = sorted((r for r in d["contrast"] if r["p"] < 0.3), key=lambda r: r["p"])[:10]
            out.append({"obs": o.id, "kind": o.kind, "site": d["site"], "has_serp": d["has_serp"],
                        "n_ranking": d["n_ranking"], "n_others": d["n_others"], "ranking_templates": d["ranking_templates"],
                        "ranking_profile": d["ranking_profile"], "contrast": contrast,
                        "adopted_in_window_on_ranking_pages": d["adopted_in_window"]})
        elif o.kind == "own_profile":
            out.append({"obs": o.id, "kind": o.kind, "site": d["site"], "n": d["n"], "profile": d["profile"],
                        "own_ranking_urls": len(d["ranking_urls"])})
        elif o.kind == "link_profile":
            out.append({"obs": o.id, "kind": o.kind, **{k: d.get(k) for k in (
                "site", "own", "n_links", "n_ref_domains", "new_ref_domains_in_window", "baseline_in_same_length",
                "growth_ratio", "anchor_share", "top_targets", "share_to_ranking_pages", "nofollow_share",
                "median_authority")}})
    return out


def hypotheses_digest(ctx: EpisodeContext, include_reasoning: bool = True) -> list[dict]:
    """紅隊驗證時 include_reasoning=False：不提供提出者的機制推理。"""
    out = []
    for h in ctx.state.hypotheses:
        if h.status == "rejected":
            continue
        d = {"id": h.id, "category": h.category, "statement": h.statement, "subject_site": h.subject_site,
             "template": h.template, "prediction": h.prediction, "falsification": h.falsification,
             "posterior": h.posterior, "status": h.status,
             "evidence": [{"id": e.id, "probe": e.probe, "direction": e.direction, "strength": e.strength,
                           "note": e.note, "observations": e.observation_ids} for e in ctx.state.evidence_for(h.id)]}
        if include_reasoning:
            d["mechanism"] = h.mechanism
            d["cited"] = h.cited
        out.append(d)
    return out
