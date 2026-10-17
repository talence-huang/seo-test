"""排名資料工具（SimWeb 中模擬 GSC / SERP）：主題排名掃描、對照組掃描、SERP 位移。"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from urllib.parse import urlencode

from seo_intel.tools.http import HttpToolkit
from seo_intel.tools.stats import aggregate, change_stats

BASELINE_DAYS = 30
_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset({"the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "vs", "how", "what", "best"})


def tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS}


def matches_topic(query: str, keywords: list[str]) -> bool:
    """查詢包含任一關鍵字片語的全部詞彙即視為同主題。"""
    q = tokens(query)
    return any(kw and tokens(kw) <= q for kw in keywords)


class RankClient:
    def __init__(self, toolkit: HttpToolkit, base: str = "https://rank.sim") -> None:
        self.http = toolkit
        self.base = base

    def _get(self, path: str, params: dict) -> dict:
        res = self.http.get_api(f"{self.base}{path}?{urlencode(params)}")
        if not res.ok:
            raise RuntimeError(res.error or f"http_{res.status}")
        return json.loads(res.text)

    def queries(self, site: str) -> list[str]:
        return [row["query"] for row in self._get("/v1/queries", {"site": site})["queries"]]

    def series(self, site: str, query: str, start: date, end: date) -> list[dict]:
        return self._get("/v1/series", {"site": site, "query": query, "start": start.isoformat(),
                                        "end": end.isoformat()})["points"]

    def serp(self, query: str, d: date) -> list[dict]:
        return self._get("/v1/serp", {"query": query, "date": d.isoformat()})["results"]


def rank_scan(rc: RankClient, site: str, keywords: list[str], window: tuple[date, date], scope: str,
              max_queries: int = 8) -> dict:
    """scope='topic'：與關鍵字相符的查詢；scope='control'：其他查詢（對照組）。"""
    all_queries = rc.queries(site)
    topic = [q for q in all_queries if matches_topic(q, keywords)]
    if scope == "topic":
        chosen = topic[:max_queries]
    else:
        others = [q for q in all_queries if q not in topic]
        step = max(1, len(others) // max_queries)
        chosen = others[::step][:max_queries]
    start = window[0] - timedelta(days=BASELINE_DAYS)
    per_query = {}
    for q in chosen:
        stats = change_stats(rc.series(site, q, start, window[1]), baseline_end=window[0])
        if stats:
            per_query[q] = stats
    return {"site": site, "scope": scope, "queries": per_query, "aggregate": aggregate(per_query),
            "n_available": len(all_queries), "n_topic": len(topic)}


def serp_scan(rc: RankClient, queries: list[str], before: date, after: date, target_domains: list[str]) -> dict:
    """比較兩個日期的 SERP，找出排名上升最多的網域與其排名網址。未進前 10 視為第 11 名。"""
    rank_at: dict[str, dict[str, list[int]]] = {"before": {}, "after": {}}
    urls_after: dict[str, set[str]] = {}
    for q in queries:
        for label, d in (("before", before), ("after", after)):
            for row in rc.serp(q, d):
                rank_at[label].setdefault(row["domain"], []).append(row["rank"])
                if label == "after":
                    urls_after.setdefault(row["domain"], set()).add(row["url"])
    domains = set(rank_at["before"]) | set(rank_at["after"])
    n = len(queries)

    def avg(label: str, d: str) -> float:
        ranks = rank_at[label].get(d, [])
        return (sum(ranks) + 11 * (n - len(ranks))) / n

    movement = sorted(({"domain": d, "before": round(avg("before", d), 2), "after": round(avg("after", d), 2),
                        "gain": round(avg("before", d) - avg("after", d), 2)} for d in domains),
                      key=lambda x: -x["gain"])
    return {
        "queries": queries,
        "before": before.isoformat(),
        "after": after.isoformat(),
        "movement": movement,
        "gainers": [m["domain"] for m in movement if m["gain"] >= 1.0],
        "target_urls_after": {d: sorted(urls_after.get(d, set())) for d in target_domains},
    }
