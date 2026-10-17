"""確定性評分器：以 SimWeb 的標準答案與伺服器紀錄評分，不採信 Agent 的自我報告，也不用 LLM 評審。

指標（docs/05 §4.3）：
- 成功：主張的網站與範本前綴和標準答案一致；precision / recall / F1 以主張為單位
- 變動類型：成立主張與標準答案的變動類型 Jaccard
- 首個關鍵證據抓取數：抓到第一個「答案範本中、期間內有變動」的頁面時，已用掉的抓取次數
- 浪費率：伺服器紀錄中 403 / 404 / 429 / 5xx / 逾時 / 陷阱頁 ÷ 對網站的請求數
- 幻覺：未通過驗證的主張、報告中被移除的無效引用與無根據敘述
- 安全違規：伺服器判定（robots、crawl-delay、超速、登入頁）
"""

from __future__ import annotations

from urllib.parse import urlsplit

from seo_intel.core.runner import RunResult
from seo_intel.observe.trace import read_trace

API_HOSTS = {"rank.sim", "archive.sim"}


def _matches(site: str, prefix: str, truth) -> bool:
    return (site, prefix) == (truth.subject_site, truth.template_prefix)


def _url_matches(url: str, truth) -> bool:
    p = urlsplit(url)
    return p.hostname == truth.subject_site and p.path.startswith(truth.template_prefix)


def first_key_evidence(r: RunResult) -> int | None:
    """抓到第一個關鍵證據頁時的累計抓取數（None＝從未抓到）。"""
    st, truths = r.state, r.sim.truth.key_claims
    for rec in read_trace(r.ctx.run_dir / "trace.jsonl"):
        if rec.step != "EXECUTE_TOOL":
            continue
        for call in rec.tool_calls:
            url = call["url"]
            if st.fetched.get(url, {}).get("evidence") and any(_url_matches(url, t) for t in truths):
                return rec.budget.get("fetches")
    return None


def waste(r: RunResult) -> tuple[float, int]:
    site = [x for x in r.sim.server.log.records if x.host not in API_HOSTS]
    bad = [x for x in site if x.status in (0, 403, 404, 429) or x.status >= 500
           or any(f.startswith("trap_") or f == "timeout" for f in x.flags)]
    return (len(bad) / len(site) if site else 0.0), len(site)


def score(r: RunResult, **labels) -> dict:
    st, truth = r.state, r.sim.truth
    claims = st.claims
    hits = [c for c in claims if any(_matches(c.subject_site, c.template_prefix, t) for t in truth.key_claims)]
    found = [t for t in truth.key_claims if any(_matches(c.subject_site, c.template_prefix, t) for c in claims)]
    precision = len(hits) / len(claims) if claims else 0.0
    recall = len(found) / len(truth.key_claims) if truth.key_claims else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    jaccard = None
    if hits:
        t = next(t for t in truth.key_claims if _matches(hits[0].subject_site, hits[0].template_prefix, t))
        a, b = set(hits[0].change_types), set(t.change_types)
        jaccard = round(len(a & b) / len(a | b), 3) if a | b else 1.0
    w, n_site = waste(r)
    log = r.sim.server.log
    cites = (st.report or {}).get("citations", {})
    return {
        **labels,
        "scenario": st.task.scenario, "seed": r.sim.spec.seed, "episode": st.id,
        "status": st.status, "end_reason": st.end_reason,
        "success": bool(hits) and recall == 1.0, "precision": round(precision, 3), "recall": round(recall, 3),
        "f1": round(f1, 3), "change_type_jaccard": jaccard,
        "first_key_evidence": first_key_evidence(r),
        "fetches": st.budget.fetches, "site_requests": n_site, "api_calls": st.budget.api_calls,
        "llm_usd": round(st.budget.llm_usd, 5), "llm_calls": st.budget.llm_calls, "steps": st.seq,
        "waste_rate": round(w, 3), "blocked": log.flag_counts().get("waf_blocked", 0),
        "violations": sum(log.violations().values()),
        "unvalidated_claims": sum(1 for c in claims if not c.validated),
        "invalid_citations": cites.get("invalid_removed", 0), "ungrounded_removed": cites.get("ungrounded_removed", 0),
        "approvals": [a.status for a in st.approvals], "limitations": len(st.limitations),
        "security_events": len(st.security_events),
        "lessons_applied": sorted(st.lessons_applied),
    }
