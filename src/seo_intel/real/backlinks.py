"""反向連結資料（Off-page）：讀取使用者從 SEO 工具匯出的 CSV（Ahrefs、Semrush…）。

反向連結無法靠爬網站取得；沒有提供 CSV 時，Agent 不研究 Off-page，並在報告寫明。
可以上傳多個檔案；每一列依「目標網址」的網域歸到競品或自家網站。
支援 UTF-8 / UTF-16（Ahrefs 的「Excel 用」匯出）與逗號、Tab、分號分隔。
"""

from __future__ import annotations

import csv
import io
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from seo_intel.core.frontier import template_key
from seo_intel.tools.text import tokens

_ALIASES = {
    "source": ("referring page url", "source url", "source page", "referring page", "linking page", "backlink url",
               "來源網址", "來源頁面", "反向連結網址"),
    "target": ("target url", "target page", "linked page", "landing page", "目標網址", "目標頁面"),
    "anchor": ("anchor", "anchor text", "錨點文字", "錨文字", "錨點"),
    "first_seen": ("first seen", "first_seen", "first found", "found date", "date found", "首次發現", "發現日期"),
    "nofollow": ("nofollow", "is nofollow", "rel", "link type", "follow"),
    "authority": ("domain rating", "dr", "domain authority", "da", "authority score", "page ascore", "ascore"),
}
REQUIRED = ("source", "target")
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%b %d, %Y", "%m/%d/%Y", "%d/%m/%Y")
_TRUE = {"true", "1", "yes", "y", "nofollow", "是"}
BASELINE_DAYS = 365   # 「平常速度」：研究期間之前一年的新增引用網域


@dataclass(frozen=True)
class Backlink:
    source_url: str
    source_domain: str
    target_url: str
    target_domain: str
    anchor: str
    first_seen: date | None
    nofollow: bool | None
    authority: float | None


def bare(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _nofollow(raw: str, column: str) -> bool | None:
    v = (raw or "").strip().lower()
    if not v:
        return None
    if column == "follow":          # 有些工具的欄位是「是否 follow」
        return v not in _TRUE
    if column in ("rel", "link type"):
        return "nofollow" in v
    return v in _TRUE


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def parse(text: str) -> list[Backlink]:
    sample = text[:4096]
    delimiter = max((",", "\t", ";"), key=lambda d: sample.splitlines()[0].count(d) if sample else 0)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
    pick = {k: next((cols[a] for a in names if a in cols), None) for k, names in _ALIASES.items()}
    missing = [k for k in REQUIRED if pick[k] is None]
    if missing:
        raise ValueError(f"反向連結 CSV 缺少欄位：{', '.join(missing)}（至少需要來源網址與目標網址；"
                         f"建議也包含錨點文字與首次發現日期）")
    nofollow_col = next((a for a in _ALIASES["nofollow"] if a in cols), "")
    out = []
    for row in reader:
        src, tgt = (row.get(pick["source"]) or "").strip(), (row.get(pick["target"]) or "").strip()
        if not src.startswith("http") or not tgt.startswith("http"):
            continue
        authority = None
        if pick["authority"]:
            try:
                authority = float(row.get(pick["authority"]) or "")
            except ValueError:
                authority = None
        out.append(Backlink(source_url=src, source_domain=bare(urlsplit(src).hostname or ""),
                            target_url=tgt, target_domain=bare(urlsplit(tgt).hostname or ""),
                            anchor=(row.get(pick["anchor"]) or "").strip() if pick["anchor"] else "",
                            first_seen=_parse_date(row.get(pick["first_seen"]) or "") if pick["first_seen"] else None,
                            nofollow=_nofollow(row.get(pick["nofollow"]) or "", nofollow_col) if pick["nofollow"] else None,
                            authority=authority))
    return out


class BacklinkData:
    """一或多個匯出檔的反向連結；依目標網域取出某個網站的連結。"""

    def __init__(self, links: list[Backlink], files: list[str]) -> None:
        self.links, self.files = links, files

    @classmethod
    def from_paths(cls, paths: list[Path]) -> BacklinkData:
        links: list[Backlink] = []
        for p in paths:
            links += parse(_decode(Path(p).read_bytes()))
        return cls(links, [Path(p).name for p in paths])

    def domains(self) -> Counter:
        return Counter(x.target_domain for x in self.links)

    def for_site(self, domain: str) -> list[Backlink]:
        d = bare(domain)
        return [x for x in self.links if x.target_domain == d]


def _anchor_kind(anchor: str, site: str, phrases: list[set[str]]) -> str:
    a = anchor.strip().lower()
    if not a:
        return "empty"
    if re.match(r"^(https?://|www\.)", a) or bare(site) in a:
        return "url"
    toks = tokens(a)
    if any(p and len(p & toks) / len(p) >= 0.75 for p in phrases):
        return "keyword"
    brand = bare(site).split(".")[0]
    if brand and brand in a.replace(" ", ""):
        return "brand"
    return "other"


def link_profile(links: list[Backlink], site: str, window: tuple[date, date], keywords: list[str],
                 ranking_urls: set[str] = frozenset()) -> dict:
    """一個網站的反向連結輪廓（Off-page 證據）：數字都由程式計算。"""
    ws, we = window
    phrases = [t for t in (tokens(k) for k in keywords) if t]
    first_by_domain: dict[str, date | None] = {}
    for x in links:
        d = x.first_seen
        cur = first_by_domain.get(x.source_domain, "missing")
        if cur == "missing" or (d is not None and (cur is None or d < cur)):
            first_by_domain[x.source_domain] = d
    dated = {k: v for k, v in first_by_domain.items() if v is not None}
    new_in_window = sorted(k for k, v in dated.items() if ws <= v <= we)
    base_start = ws - timedelta(days=BASELINE_DAYS)
    baseline = [k for k, v in dated.items() if base_start <= v < ws]
    window_days = max(1, (we - ws).days)
    baseline_rate = len(baseline) / BASELINE_DAYS * window_days     # 換算成同樣長度期間的平常數量
    anchors = Counter(_anchor_kind(x.anchor, site, phrases) for x in links)
    targets = Counter(template_key(x.target_url) for x in links)
    ranking = {u.rstrip("/") for u in ranking_urls}
    to_ranking = sum(1 for x in links if x.target_url.rstrip("/") in ranking)
    nofollow = [x.nofollow for x in links if x.nofollow is not None]
    authority = [x.authority for x in links if x.authority is not None]
    n = len(links)
    return {
        "site": site, "n_links": n, "n_ref_domains": len(first_by_domain), "n_dated_domains": len(dated),
        "new_ref_domains_in_window": len(new_in_window), "new_ref_domains_sample": new_in_window[:10],
        "baseline_in_same_length": round(baseline_rate, 1),
        "growth_ratio": round(len(new_in_window) / max(baseline_rate, 0.5), 2) if dated else None,
        "anchor_share": {k: round(v / n, 3) for k, v in anchors.most_common()} if n else {},
        "top_targets": [{"template": t, "links": c, "share": round(c / n, 3)} for t, c in targets.most_common(5)],
        "share_to_ranking_pages": round(to_ranking / n, 3) if n and ranking else None,
        "nofollow_share": round(sum(nofollow) / len(nofollow), 3) if nofollow else None,
        "median_authority": statistics.median(authority) if authority else None,
        "top_ref_domains": [d for d, _ in Counter(x.source_domain for x in links).most_common(5)],
    }
