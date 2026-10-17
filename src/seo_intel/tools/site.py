"""網站探勘：robots.txt → sitemap index → 子 sitemap，不必逐頁抓取即可取得數萬個 URL 與 lastmod。"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlsplit

from seo_intel.tools.extract import PageFeatures, extract
from seo_intel.tools.http import HttpResult, HttpToolkit

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>(?:\s*<lastmod>\s*([^<\s]+)\s*</lastmod>)?")
_SITEMAP_LINE = re.compile(r"^\s*sitemap:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def _locs(xml: str) -> list[tuple[str, date | None]]:
    out = []
    for loc, lastmod in _LOC.findall(xml):
        try:
            d = date.fromisoformat(lastmod[:10]) if lastmod else None
        except ValueError:
            d = None
        out.append((loc, d))
    return out


FALLBACK_SITEMAPS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")   # 一般網站與 WordPress 的慣例位置


def discover(http: HttpToolkit, domain: str, max_sitemaps: int = 40) -> dict:
    robots_parser = http.robots(domain)  # 已載入時不會重抓
    robots = http.get_site(f"https://{domain}/robots.txt")
    declared = _SITEMAP_LINE.findall(robots.text or "")
    entries: list[tuple[str, date | None]] = []
    fetched, errors = [], []
    queue = list(declared) or [f"https://{domain}{p}" for p in FALLBACK_SITEMAPS[:1]]
    fallbacks = [] if declared else [f"https://{domain}{p}" for p in FALLBACK_SITEMAPS[1:]]
    while queue and len(fetched) < max_sitemaps:
        sm = queue.pop(0)
        res = http.get_site(sm)
        fetched.append(sm)
        if not res.ok:
            errors.append({"url": sm, "error": res.error})
            if not queue and fallbacks and not entries:   # robots 沒宣告且慣例位置找不到：再試下一個慣例位置
                queue.append(fallbacks.pop(0))
            continue
        if "<sitemapindex" in res.text:
            queue += [loc for loc, _ in _locs(res.text)]
        else:
            entries += [(loc, d) for loc, d in _locs(res.text) if urlsplit(loc).hostname == domain]
    source = "sitemap"
    if not entries:   # 沒有 sitemap：從首頁的站內連結開始（真實網站常見）
        home = http.get_site(f"https://{domain}/")
        if home.ok:
            entries = [(u, None) for u, _ in dict.fromkeys(extract(home.text, f"https://{domain}/").links)]
            source = "homepage_links"
    return {
        "domain": domain,
        "robots_found": robots_parser is not None,
        "sitemaps": fetched,
        "errors": errors,
        "entries": [(u, d.isoformat() if d else None) for u, d in entries],
        "truncated": bool(queue),
        "source": source,
    }


def fetch_page(http: HttpToolkit, url: str) -> tuple[HttpResult, PageFeatures | None]:
    res = http.get_site(url)
    return res, (extract(res.text, url) if res.ok else None)
