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


def discover(http: HttpToolkit, domain: str, max_sitemaps: int = 40) -> dict:
    robots_parser = http.robots(domain)  # 已載入時不會重抓
    robots = http.get_site(f"https://{domain}/robots.txt")
    sitemap_urls = _SITEMAP_LINE.findall(robots.text or "") or [f"https://{domain}/sitemap.xml"]
    entries: list[tuple[str, date | None]] = []
    fetched, errors = [], []
    queue = list(sitemap_urls)
    while queue and len(fetched) < max_sitemaps:
        sm = queue.pop(0)
        res = http.get_site(sm)
        fetched.append(sm)
        if not res.ok:
            errors.append({"url": sm, "error": res.error})
            continue
        if "<sitemapindex" in res.text:
            queue += [loc for loc, _ in _locs(res.text)]
        else:
            entries += [(loc, d) for loc, d in _locs(res.text) if urlsplit(loc).hostname == domain]
    return {
        "domain": domain,
        "robots_found": robots_parser is not None,
        "sitemaps": fetched,
        "errors": errors,
        "entries": [(u, d.isoformat() if d else None) for u, d in entries],
        "truncated": bool(queue),
    }


def fetch_page(http: HttpToolkit, url: str) -> tuple[HttpResult, PageFeatures | None]:
    res = http.get_site(url)
    return res, (extract(res.text, url) if res.ok else None)
