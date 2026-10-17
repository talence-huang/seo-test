"""Frontier：URL 範本分群、陷阱偵測與評分。決定「在數萬頁中先抓哪一頁」。

分數 = 頁面層級的靜態分數（相關度、新鮮度、權威、SERP 訊號、焦點網域）
     + 範本層級的動態分數（證據產出率、探索加分）− 懲罰（避開、封鎖成本、陷阱）。
每個範本維護一個最大堆積，選擇時只需掃過範本數，而非全部 URL；每次選擇都保留分數明細。
"""

from __future__ import annotations

import heapq
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from seo_intel.tools.rank import tokens

_SLUG_WITH_ID = re.compile(r"^[a-z0-9-]+-\d+$")
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|sessionid)")
_PAGE_FILE = re.compile(r"[a-z].*\.(html?|php|aspx?)$", re.IGNORECASE)
_NON_HTML = re.compile(r"\.(xml|txt|json|js|css|png|jpe?g|gif|svg|webp|pdf|zip)$", re.IGNORECASE)
MIN_TEMPLATE_SIZE_FOR_SAMPLING = 3


@dataclass(frozen=True)
class Weights:
    relevance: float = 0.35
    template_value: float = 0.25
    freshness: float = 0.15
    authority: float = 0.10
    serp: float = 0.20
    focus: float = 0.10
    explore: float = 0.15
    avoid: float = 0.50
    cost: float = 0.30
    trap: float = 1.00


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)))
    return f"https://{(parts.hostname or '').lower()}{parts.path or '/'}" + (f"?{query}" if query else "")


def template_key(url: str) -> str:
    parts = urlsplit(url)
    segs = []
    raw = [s for s in parts.path.split("/") if s]
    for i, seg in enumerate(raw):
        last = i == len(raw) - 1
        if seg.isdigit():
            segs.append("{n}")
        elif (_SLUG_WITH_ID.match(seg) or (len(seg) > 20 and "-" in seg)
              or not unquote(seg).isascii()                                    # 中文等非 ASCII（含百分比編碼）
              or (last and (_PAGE_FILE.search(seg) or (len(seg) >= 8 and ("-" in seg or "_" in seg))))):
            segs.append("{slug}")   # 真實網站的文章代稱：WordPress 的短代稱、.html 頁面
        else:
            segs.append(seg)
    keys = sorted({k for k, _ in parse_qsl(parts.query)})
    return f"{parts.hostname}/" + "/".join(segs) + (f"?{','.join(keys)}" if keys else "")


def detect_trap(tkey: str) -> str | None:
    path = tkey.split("/", 1)[1] if "/" in tkey else ""
    if "?" in path:
        return "query_params"
    if path.count("{n}") >= 3 and path.endswith("{n}"):   # /2024/05/12 是日曆；/2024/05/12/{slug} 是一般文章網址
        return "calendar"
    return None


@dataclass
class Entry:
    url: str
    domain: str
    template: str
    depth: int
    relevance: float
    lastmod: str | None = None
    sources: list[str] = field(default_factory=list)
    inlinks: int = 0
    anchor: str = ""
    status: str = "new"          # new / fetched
    version: int = 0


@dataclass
class TemplateStat:
    key: str
    domain: str
    n_urls: int = 0
    n_sitemap: int = 0
    n_recent: int = 0             # lastmod 落在研究期間內
    n_fetched: int = 0
    n_evidence: int = 0
    n_blocked: int = 0
    n_errors: int = 0
    alpha: float = 1.0
    beta: float = 1.0
    trap: str | None = None
    burst: float | None = None          # 期間內更新比例 ÷ 全站平常比例（探勘後才知道）
    prior_source: str | None = None     # M2 先驗來源（None＝無經驗，使用 Beta(1,1)）
    prior_mean: float | None = None
    prior_n: float = 0.0
    avoid_source: str | None = None     # 套用的 M1 經驗 ID

    @property
    def value(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def min_samples(self, default: int, avoid: float) -> int:
        """確信是低價值的範本只抽 1 頁；被經驗標記要避開的範本不抽樣。"""
        if avoid >= 1.0:
            return 0
        if self.prior_mean is not None and self.prior_mean < 0.15 and self.prior_n >= 6:
            return 1
        return default


class Frontier:
    def __init__(self, keywords: list[str], window: tuple[date, date], target_domains: list[str],
                 weights: Weights = Weights()) -> None:
        self.keywords = keywords
        self.phrases = [t for t in (tokens(k) for k in keywords) if t]   # 每組關鍵字片語各自的詞彙
        self.kw_tokens = set().union(*self.phrases) if self.phrases else set()
        self.window = window
        self.targets = list(target_domains)
        self.focus: set[str] = set()
        self.w = weights
        self.entries: dict[str, Entry] = {}
        self.templates: dict[str, TemplateStat] = {}
        self.excluded: dict[str, int] = {}
        self.template_avoid: dict[str, float] = {}   # M1：經驗標記要避開的範本
        self.prior_fn = None    # M2：(範本鍵, URL 數, 爆發度) → PriorInfo | None（不寫入檢查點）
        self.avoid_fn = None    # M1：範本鍵 → (避開強度, 經驗) | None
        self._heaps: dict[str, list[tuple[float, int, str]]] = {}

    # ---------- 記憶掛勾 ----------

    def attach_memory(self, prior_fn, avoid_fn) -> None:
        self.prior_fn, self.avoid_fn = prior_fn, avoid_fn
        for stat in self.templates.values():
            self._prime(stat)

    def _prime(self, stat: TemplateStat) -> None:
        if self.avoid_fn is not None:
            hit = self.avoid_fn(stat.key)
            if hit:
                self.template_avoid[stat.key] = hit[0]
                stat.avoid_source = hit[1].id
        if self.prior_fn is None or stat.n_fetched:
            return
        info = self.prior_fn(stat.key, stat.n_urls, stat.burst)
        if info is None:
            return
        stat.alpha, stat.beta = info.alpha0, info.beta0
        stat.prior_source, stat.prior_mean, stat.prior_n = info.source, info.mean, info.n

    def reprime(self, domain: str, rows: list[dict]) -> None:
        """探勘後已知範本規模與更新爆發度：重新查詢先驗（只針對尚未抓取的範本）。"""
        share = sum(r["n_recent"] for r in rows) / max(1, sum(r["n_urls"] for r in rows))
        by_name = {r["template"]: r for r in rows}
        for stat in self.stats([domain]):
            row = by_name.get(stat.key.split("/", 1)[1])
            if row and row["n_urls"]:
                stat.burst = round(row["recent_share"] / max(share, 0.01), 2)
            self._prime(stat)

    # ---------- 加入 ----------

    def phrase_coverage(self, text_tokens: set[str]) -> float:
        """涵蓋度最高的那組關鍵字片語的比例（關鍵字越多不會稀釋相關度）。"""
        return max((len(p & text_tokens) / len(p) for p in self.phrases), default=0.0)

    def _relevance(self, url: str, anchor: str) -> float:
        return self.phrase_coverage(tokens(urlsplit(url).path) | tokens(anchor))

    def add(self, url: str, *, source: str, lastmod: str | None = None, anchor: str = "",
            allowed: bool = True) -> bool:
        url = normalize_url(url)
        domain = urlsplit(url).hostname or ""
        if domain not in self.targets:
            self.excluded["off_target"] = self.excluded.get("off_target", 0) + 1
            return False
        if _NON_HTML.search(urlsplit(url).path):
            self.excluded["non_html"] = self.excluded.get("non_html", 0) + 1
            return False
        if not allowed:
            self.excluded["robots"] = self.excluded.get("robots", 0) + 1
            return False
        entry = self.entries.get(url)
        if entry is None:
            tkey = template_key(url)
            entry = Entry(url=url, domain=domain, template=tkey, depth=urlsplit(url).path.count("/"),
                          relevance=self._relevance(url, anchor), lastmod=lastmod, sources=[source], anchor=anchor)
            self.entries[url] = entry
            stat = self.templates.get(tkey)
            if stat is None:
                stat = self.templates[tkey] = TemplateStat(key=tkey, domain=domain, trap=detect_trap(tkey))
                self._prime(stat)
            stat.n_urls += 1
            stat.n_sitemap += source == "sitemap"
            stat.n_recent += self._in_window(lastmod)
        else:
            if source not in entry.sources:
                entry.sources.append(source)
            if source == "link":
                entry.inlinks += 1
            if anchor and not entry.anchor:
                entry.anchor = anchor
                entry.relevance = max(entry.relevance, self._relevance(url, anchor))
            if lastmod and not entry.lastmod:
                entry.lastmod = lastmod
        if entry.status == "new":
            entry.version += 1
            heapq.heappush(self._heaps.setdefault(entry.template, []), (-self._static(entry)[0], entry.version, url))
        return True

    def _in_window(self, lastmod: str | None) -> bool:
        if not lastmod:
            return False
        d = date.fromisoformat(lastmod[:10])
        return self.window[0] <= d <= self.window[1]

    # ---------- 評分 ----------

    def _static(self, e: Entry) -> tuple[float, dict[str, float]]:
        fresh = 0.0
        if e.lastmod:
            d = date.fromisoformat(e.lastmod[:10])
            fresh = 1.0 if self.window[0] <= d <= self.window[1] else 0.4 if d >= self.window[0] - timedelta(days=90) else 0.0
        auth = min(1.0, math.log1p(e.inlinks) / math.log(20)) - 0.05 * max(0, e.depth - 2)
        parts = {
            "relevance": self.w.relevance * e.relevance,
            "freshness": self.w.freshness * fresh,
            "authority": self.w.authority * auth,
            "serp": self.w.serp * ("serp" in e.sources),
            "focus": self.w.focus * (e.domain in self.focus),
        }
        return sum(parts.values()), parts

    def _dynamic(self, stat: TemplateStat, total_fetched: int) -> dict[str, float]:
        value = stat.value   # 先驗已折算為 Beta 的虛擬觀察次數，本次實測會逐步蓋過它
        explore = min(1.0, math.sqrt(2 * math.log(total_fetched + 2) / (stat.n_fetched + 1))) / 2
        cost = (stat.n_blocked + stat.n_errors) / (stat.n_fetched + 1)
        return {
            "template_value": self.w.template_value * value,
            "explore": self.w.explore * explore,
            "avoid": -self.w.avoid * self.template_avoid.get(stat.key, 0.0),
            "cost": -self.w.cost * min(1.0, cost),
            "trap": -self.w.trap * (stat.trap is not None),
        }

    def _top_of(self, tkey: str) -> Entry | None:
        heap = self._heaps.get(tkey, [])
        while heap:
            _, version, url = heap[0]
            e = self.entries[url]
            if e.status == "new" and e.version == version:
                return e
            heapq.heappop(heap)
        return None

    def best(self, k: int = 5, *, domains: list[str] | None = None, under_sampled: int | None = None,
             exclude_traps: bool = False, templates: list[str] | None = None,
             only: set[str] | None = None, max_fetched: int | None = None) -> list[tuple[Entry, float, dict[str, float]]]:
        """回傳前 k 個候選與分數明細。一般情況每個範本最多一個（確保多樣性）；指定 templates 時取該範本的前 k 個。"""
        if templates is not None:
            return self._best_within(templates, k)
        total_fetched = sum(s.n_fetched for s in self.templates.values())
        out = []
        for tkey, stat in self.templates.items():
            if domains is not None and stat.domain not in domains:
                continue
            if only is not None and tkey not in only:
                continue
            if exclude_traps and stat.trap:
                continue
            if max_fetched is not None and stat.n_fetched >= max_fetched:
                continue   # 同一範本抓再多頁，資訊量遞減；避免單一範本吃掉預算
            if under_sampled is not None:
                need = stat.min_samples(under_sampled, self.template_avoid.get(tkey, 0.0))
                if (stat.n_fetched >= min(need, stat.n_urls) or stat.n_blocked >= 2
                        or (stat.n_sitemap == 0 and stat.n_urls < MIN_TEMPLATE_SIZE_FOR_SAMPLING)):
                    continue   # 抽樣只針對有規模的範本（首頁等單一頁面不算範本）
            e = self._top_of(tkey)
            if e is None:
                continue
            _, parts = self._static(e)
            parts.update(self._dynamic(stat, total_fetched))
            out.append((e, round(sum(parts.values()), 4), {p: round(v, 4) for p, v in parts.items()}))
        out.sort(key=lambda x: -x[1])
        return out[:k]

    def _best_within(self, templates: list[str], k: int) -> list[tuple[Entry, float, dict[str, float]]]:
        total_fetched = sum(s.n_fetched for s in self.templates.values())
        out = []
        for tkey in templates:
            stat = self.templates.get(tkey)
            if stat is None:
                continue
            dynamic = self._dynamic(stat, total_fetched)
            for e in sorted((x for x in self.entries.values() if x.template == tkey and x.status == "new"),
                            key=lambda x: -self._static(x)[0])[:k]:
                parts = {**self._static(e)[1], **dynamic}
                out.append((e, round(sum(parts.values()), 4), {p: round(v, 4) for p, v in parts.items()}))
        out.sort(key=lambda x: -x[1])
        return out[:k]

    # ---------- 回饋 ----------

    def mark_fetched(self, url: str, *, ok: bool, evidence: bool, blocked: bool) -> None:
        e = self.entries.get(normalize_url(url))
        if e is None or e.status == "fetched":
            return
        e.status = "fetched"
        stat = self.templates[e.template]
        stat.n_fetched += 1
        if blocked:
            stat.n_blocked += 1
        elif not ok:
            stat.n_errors += 1
        if ok:
            stat.n_evidence += evidence
            stat.alpha += evidence
            stat.beta += not evidence

    def exclude(self, url: str, reason: str) -> None:
        e = self.entries.get(normalize_url(url))
        if e is not None and e.status == "new":
            e.status = "excluded"
            self.excluded[reason] = self.excluded.get(reason, 0) + 1

    def set_focus(self, domains: list[str]) -> None:
        self.focus = set(domains)
        for e in self.entries.values():   # 焦點改變會影響靜態分數，重建堆積
            if e.status == "new":
                e.version += 1
        self._heaps = {}
        for e in self.entries.values():
            if e.status == "new":
                heapq.heappush(self._heaps.setdefault(e.template, []), (-self._static(e)[0], e.version, e.url))

    def stats(self, domains: list[str] | None = None) -> list[TemplateStat]:
        rows = [s for s in self.templates.values() if domains is None or s.domain in domains]
        return sorted(rows, key=lambda s: (s.domain, -s.n_urls))

    # ---------- 序列化（檢查點）----------

    def to_dict(self) -> dict:
        return {
            "keywords": self.keywords,
            "window": [d.isoformat() for d in self.window],
            "targets": self.targets,
            "focus": sorted(self.focus),
            "entries": [asdict(e) for e in self.entries.values()],
            "templates": [asdict(s) for s in self.templates.values()],
            "excluded": self.excluded,
            "template_avoid": self.template_avoid,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Frontier:
        f = cls(d["keywords"], (date.fromisoformat(d["window"][0]), date.fromisoformat(d["window"][1])), d["targets"])
        f.excluded = d["excluded"]
        f.template_avoid = d.get("template_avoid", {})
        f.templates = {s["key"]: TemplateStat(**s) for s in d["templates"]}
        f.entries = {e["url"]: Entry(**e) for e in d["entries"]}
        f.set_focus(d["focus"])
        return f
