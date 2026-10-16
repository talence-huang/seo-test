"""排名模型（模擬 GSC / SERP）。

位置 = 基準位置 + 每日雜訊 + 植入效應（漸進生效）+ 混淆因子（自家站全站性波動）。
自家站在受影響主題的查詢上變差；其他主題的查詢是天然的對照組。
"""

from __future__ import annotations

from datetime import date, timedelta

from simweb.archetypes import CONTENT_KINDS
from simweb.rng import gauss, h64, pick, slugify, unit
from simweb.world import Page, World

NOISE_SD = 0.5
DATA_LAG_DAYS = 2           # 類似 GSC 的資料延遲
HISTORY_DAYS = 480          # 可查詢的最早日期距離 now 的天數


def _ramp(d: date, start: date, ramp_days: int) -> float:
    if d < start:
        return 0.0
    return min(1.0, ((d - start).days + 1) / ramp_days)


class RankModel:
    def __init__(self, world: World) -> None:
        self.w = world
        arch = world.archetype
        self.query_cluster: dict[str, str] = {}
        for c in arch.clusters:
            for q in (c, *(f"{c} {m}" for m in arch.modifiers[:7])):
                self.query_cluster[q] = c
        self.fillers = arch.fillers

    @property
    def latest_date(self) -> date:
        return self.w.now - timedelta(days=DATA_LAG_DAYS)

    @property
    def earliest_date(self) -> date:
        return self.w.now - timedelta(days=HISTORY_DAYS)

    def queries(self) -> list[tuple[str, str]]:
        return list(self.query_cluster.items())

    def _base(self, domain: str, query: str) -> float:
        u = unit(self.w.seed, domain, query, "base")
        if domain == self.w.own:
            return 3.5 + 8 * u
        if domain in self.w.sites:
            return 4.0 + 12 * u
        return 1.0 + 25 * u

    def position(self, domain: str, query: str, d: date) -> float:
        cluster = self.query_cluster[query]
        pos = self._base(domain, query) + NOISE_SD * gauss(self.w.seed, domain, query, d.isoformat())
        for fx in self.w.spec.effects:
            if fx.cluster != cluster:
                continue
            ev = self.w.spec.event(fx.cause)
            frac = _ramp(d, ev.date + timedelta(days=fx.lag_days), fx.ramp_days)
            if domain == self.w.own:
                pos += fx.own_loss * frac
            if domain == ev.site:
                pos -= fx.competitor_gain * frac
        if domain == self.w.own:
            for cf in self.w.spec.confounders:
                pos += cf.own_shift * _ramp(d, cf.date, cf.ramp_days)
        return round(min(60.0, max(1.0, pos)), 1)

    def ranking_url(self, domain: str, query: str, d: date) -> str:
        if domain not in self.w.sites:
            return f"https://{domain}/{slugify(query)}"
        cluster = self.query_cluster[query]
        for fx in self.w.spec.effects:
            ev = self.w.spec.event(fx.cause)
            if ev.site != domain or fx.cluster != cluster or d < ev.date + timedelta(days=fx.lag_days):
                continue
            pages = [p for p in (self.w.page(domain, ev.template, i) for i in self.w.event_pages[ev.id])
                     if p.created <= d and p.cluster == cluster]
            if pages:
                return pick(pages, self.w.seed, query, "rank-url").url
        return self._default_page(domain, cluster, query).url

    def _default_page(self, domain: str, cluster: str, query: str) -> Page:
        site = self.w.sites[domain]
        candidates = [t for t in site.templates.values()
                      if t.kind in CONTENT_KINDS and t.launched_by is None
                      and self.w.cluster_indices(domain, t.name, cluster)]
        t = pick(candidates, self.w.seed, domain, query, "rank-tpl")
        idx = self.w.cluster_indices(domain, t.name, cluster)
        return self.w.page(domain, t.name, pick(idx, self.w.seed, domain, query, "rank-idx"))

    def series(self, domain: str, query: str, start: date, end: date) -> list[dict]:
        end = min(end, self.latest_date)
        out, d = [], start
        while d <= end:
            out.append({"date": d.isoformat(), "position": self.position(domain, query, d),
                        "url": self.ranking_url(domain, query, d)})
            d += timedelta(days=1)
        return out

    def serp(self, query: str, d: date, top: int = 10) -> list[dict]:
        domains = [*self.w.sites, *self.fillers]
        ranked = sorted(domains, key=lambda x: (self.position(x, query, d), h64(self.w.seed, x, query, "tie")))
        return [{"rank": i + 1, "domain": x, "url": self.ranking_url(x, query, d)} for i, x in enumerate(ranked[:top])]
