"""世界模型：網站、頁面、頁面版本與植入事件。所有頁面皆惰性產生並快取。"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date, timedelta

from simweb.archetypes import ARCHETYPES, Archetype
from simweb.rng import h64, pick, rng, slugify, unit
from simweb.spec import EventSpec, ScenarioSpec, SiteSpec, TemplateSpec

_SLUG_RE = re.compile(r"^(?P<slug>[a-z0-9-]+)-(?P<idx>\d+)$")


@dataclass(frozen=True)
class Page:
    domain: str
    template: str
    index: int
    path: str
    cluster: str
    modifier: str
    created: date

    @property
    def url(self) -> str:
        return f"https://{self.domain}{self.path}"


@dataclass(frozen=True)
class Version:
    date: date
    words: int
    sections: tuple[str, ...]
    schema_types: tuple[str, ...]
    faq: int
    table: bool
    events: tuple[str, ...] = ()


class SiteModel:
    def __init__(self, spec: SiteSpec) -> None:
        self.spec = spec
        self.domain = spec.domain
        self.archetype: Archetype = ARCHETYPES[spec.archetype]
        self.templates: dict[str, TemplateSpec] = spec.templates()
        self._by_prefix = sorted(self.templates.values(), key=lambda t: -len(t.prefix))

    def template_for_path(self, path: str) -> TemplateSpec | None:
        return next((t for t in self._by_prefix if path.startswith(t.prefix)), None)

    def has_kind(self, kind: str) -> bool:
        return any(t.kind == kind for t in self.templates.values())

    def first_of_kind(self, kind: str) -> TemplateSpec | None:
        return next((t for t in self.templates.values() if t.kind == kind), None)


class World:
    def __init__(self, spec: ScenarioSpec) -> None:
        self.spec = spec
        self.seed = spec.seed
        self.now = spec.now
        self.archetype = ARCHETYPES[spec.archetype]
        self.sites: dict[str, SiteModel] = {s.domain: SiteModel(s) for s in spec.sites}
        self.own = spec.goal.own_site
        self._cluster_idx: dict[tuple[str, str, str], list[int]] = {}
        self._pages: dict[tuple[str, str, int], Page] = {}
        self._versions: dict[tuple[str, str, int], tuple[Version, ...]] = {}
        self._changes: dict[tuple[str, str, int], list[tuple[date, EventSpec]]] = defaultdict(list)
        self._created: dict[tuple[str, str, int], date] = {}
        self.event_pages: dict[str, list[int]] = {}
        self._apply_events()
        self._injection = self._choose_injection_pages()

    # ---------- 主題與頁面 ----------

    def clusters_for(self, t: TemplateSpec) -> tuple[str, ...]:
        return t.clusters or self.archetype.clusters

    def cluster_of(self, domain: str, t: TemplateSpec, index: int) -> str:
        return pick(self.clusters_for(t), self.seed, domain, t.name, index, "cluster")

    def cluster_indices(self, domain: str, template: str, cluster: str) -> list[int]:
        key = (domain, template, cluster)
        if key not in self._cluster_idx:
            t = self.sites[domain].templates[template]
            self._cluster_idx[key] = [i for i in range(t.count) if self.cluster_of(domain, t, i) == cluster]
        return self._cluster_idx[key]

    def page(self, domain: str, template: str, index: int) -> Page:
        key = (domain, template, index)
        if key in self._pages:
            return self._pages[key]
        t = self.sites[domain].templates[template]
        if not 0 <= index < t.count:
            raise IndexError(f"{domain}{t.prefix} 沒有第 {index} 頁")
        cluster = self.cluster_of(domain, t, index)
        modifier = pick(self.archetype.modifiers, self.seed, domain, template, index, "modifier")
        created = self._created.get(key) or self.now - timedelta(
            days=30 + h64(self.seed, domain, template, index, "created") % 1400
        )
        changes = self._changes.get(key)
        if changes:
            earliest = min(d for d, _ in changes)
            if created >= earliest:  # 被改寫的頁面必須早於改寫日存在
                created = earliest - timedelta(days=60 + h64(self.seed, domain, template, index, "pre") % 300)
        path = f"{t.prefix}{slugify(f'{cluster} {modifier}')}-{index}"
        page = Page(domain, template, index, path, cluster, modifier, created)
        self._pages[key] = page
        return page

    def page_by_path(self, domain: str, path: str) -> Page | None:
        site = self.sites.get(domain)
        if site is None:
            return None
        t = site.template_for_path(path)
        if t is None:
            return None
        m = _SLUG_RE.match(path[len(t.prefix):])
        if not m:
            return None
        index = int(m.group("idx"))
        if index >= t.count:
            return None
        page = self.page(domain, t.name, index)
        return page if page.path == path and page.created <= self.now else None

    def iter_pages(self, domain: str, template: str) -> Iterator[Page]:
        for i in range(self.sites[domain].templates[template].count):
            page = self.page(domain, template, i)
            if page.created <= self.now:
                yield page

    def sitemap_size(self, domain: str) -> int:
        return sum(t.count for t in self.sites[domain].templates.values())

    def is_injection(self, page: Page) -> bool:
        return self._injection.get(page.domain) == (page.template, page.index)

    def injection_page(self, domain: str) -> Page | None:
        loc = self._injection.get(domain)
        return self.page(domain, *loc) if loc else None

    # ---------- 版本 ----------

    def versions(self, page: Page) -> tuple[Version, ...]:
        key = (page.domain, page.template, page.index)
        if key in self._versions:
            return self._versions[key]
        t = self.sites[page.domain].templates[page.template]
        r = rng(self.seed, page.url, "base")
        n_sections = r.randint(*t.sections)
        v = Version(
            date=page.created,
            words=r.randint(*t.words),
            sections=tuple(r.sample(self.archetype.headings, min(n_sections, len(self.archetype.headings)))),
            schema_types=t.schema_types,
            faq=r.randint(4, 8) if t.kind == "faq" else 0,
            table=t.has_table,
        )
        timeline: list[tuple[date, int, object]] = []
        if unit(self.seed, page.url, "noise") < t.noise_edit_rate:
            span = (self.now - page.created).days
            if span > 45:
                d = page.created + timedelta(days=30 + h64(self.seed, page.url, "noise-day") % (span - 30))
                timeline.append((d, 0, 0.97 + 0.08 * unit(self.seed, page.url, "noise-mult")))
        for d, ev in self._changes.get(key, []):
            timeline.append((d, 1, ev))
        timeline.sort(key=lambda x: (x[0], x[1]))

        out = [v]
        for d, kind, payload in timeline:
            if d > self.now:
                continue
            if kind == 0:  # 一般小幅編輯：只改字數與更新日期
                v = replace(v, date=d, words=max(1, round(v.words * payload)))
            else:
                c = payload.changes
                extra = tuple(h for h in self.archetype.headings if h not in v.sections)[: c.add_sections]
                v = replace(
                    v,
                    date=d,
                    words=max(1, round(v.words * c.words_mult)),
                    sections=v.sections + extra,
                    schema_types=v.schema_types + tuple(s for s in c.add_schema if s not in v.schema_types),
                    faq=v.faq + c.add_faq,
                    table=v.table or c.add_table,
                    events=v.events + (payload.id,),
                )
            out.append(v)
        result = tuple(out)
        self._versions[key] = result
        return result

    def version_at(self, page: Page, d: date) -> Version | None:
        current = None
        for v in self.versions(page):
            if v.date <= d:
                current = v
        return current

    def current(self, page: Page) -> Version:
        v = self.version_at(page, self.now)
        assert v is not None
        return v

    def lastmod(self, page: Page) -> date:
        return self.current(page).date

    # ---------- 事件 ----------

    def _apply_events(self) -> None:
        for ev in self.spec.events:
            t = self.sites[ev.site].templates[ev.template]
            pool = self.cluster_indices(ev.site, t.name, ev.cluster) if ev.cluster else list(range(t.count))
            if ev.kind == "refresh":
                chosen = sorted(pool, key=lambda i: h64(self.seed, ev.id, i))[: ev.pages]
                for i in chosen:
                    d = ev.date + timedelta(days=h64(self.seed, ev.id, i, "day") % (ev.spread_days + 1))
                    self._changes[(ev.site, t.name, i)].append((d, ev))
            else:
                chosen = pool[-ev.pages:] if ev.kind == "bulk_add" else pool
                for i in chosen:
                    d = ev.date + timedelta(days=h64(self.seed, ev.id, i, "day") % (ev.spread_days + 1))
                    self._created[(ev.site, t.name, i)] = d
            if not chosen:
                raise ValueError(f"事件 {ev.id} 沒有選到任何頁面，請檢查 cluster 與 pages")
            self.event_pages[ev.id] = sorted(chosen)

    def _choose_injection_pages(self) -> dict[str, tuple[str, int]]:
        out: dict[str, tuple[str, int]] = {}
        for site in self.sites.values():
            inj = site.spec.injection
            if inj is None:
                continue
            t = site.templates[inj.template]
            pool = self.cluster_indices(site.domain, t.name, inj.cluster) if inj.cluster else list(range(t.count))
            out[site.domain] = (t.name, min(pool, key=lambda i: h64(self.seed, "inject", i)))
        return out
