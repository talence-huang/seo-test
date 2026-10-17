"""把觀察整理成給 LLM 的精簡摘要：LLM 看到的是程式算好的數字，而不是原始資料。"""

from __future__ import annotations

from collections import Counter

from seo_intel.core.context import EpisodeContext
from seo_intel.core.frontier import template_key

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
        share = sum(r["n_recent"] for r in rows) / max(1, sum(r["n_urls"] for r in rows))
        inventory.append({"obs": o.id, "site": o.data["domain"], "site_recent_share": round(share, 3),
                          "templates": sorted(rows, key=lambda r: -r["recent_share"])[:8]})
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
    pages = [{"id": o.id, "kind": o.kind, "summary": o.summary} for o in st.observations
             if o.kind in ("page", "page_diff")][-PAGE_OBS_LIMIT:]
    known = sorted(t.key for t in fr.templates.values()
                   if t.domain in st.task.allowed_domains and not t.trap and (t.n_sitemap or t.n_urls >= 3))
    return {"rank": rank, "serp": serp, "inventory": inventory, "template_findings": findings,
            "observations": general + pages, "known_templates": known}


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
