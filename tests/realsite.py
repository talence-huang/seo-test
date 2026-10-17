"""測試用的「真實網站」：以 httpx.MockTransport 模擬一個中文茶葉網站、Wayback 與 SerpApi（不連網、不花錢）。

真相：tea.example.tw 在 8–9 月改版 /guide/ 下的高山茶指南（字數大幅增加、加入 FAQPage），並新增 4 篇；
/blog/ 是一年前的舊文章，/tag/ 是背景變動。SERP 上排名的是 /guide/ 頁面。
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, quote, urlsplit

import httpx

SITE = "tea.example.tw"
OTHER = "other-tea.tw"
KEYWORDS = ["高山茶", "烏龍茶"]
GUIDE = [f"/guide/{quote(f'高山茶-指南-{i}')}/" for i in range(12)]
NEW_GUIDES = set(GUIDE[8:])
BLOG = [f"/blog/post-{i}-tea-notes/" for i in range(20)]
TAGS = [f"/tag/{quote(f'標籤{i}')}/" for i in range(30)]
BODY = "高山茶的沖泡方式、烏龍茶的產地差異與保存方法。"


def page(title: str, n: int, published: str, modified: str, faq: int = 0) -> str:
    graph = [{"@type": "Article", "datePublished": published, "dateModified": modified}]
    if faq:
        graph.append({"@type": "FAQPage", "mainEntity": [{"@type": "Question"}] * faq})
    links = "".join(f'<a href="{u}">指南</a>' for u in GUIDE[:3])
    return (f"<html><head><title>{title}</title><script type=\"application/ld+json\">"
            f"{json.dumps({'@context': 'https://schema.org', '@graph': graph}, ensure_ascii=False)}</script></head>"
            f"<body><h1>{title}</h1>" + "".join(f"<h2>段落 {k}</h2><p>{BODY}</p>" for k in range(n))
            + f"<nav>{links}</nav></body></html>")


def _rows(paths: list[str], lastmod) -> str:
    return "".join(f"<url><loc>https://{SITE}{p}</loc><lastmod>{lastmod(p)}</lastmod></url>" for p in paths)


def _guide_lastmod(p: str) -> str:
    return "2026-09-10" if GUIDE.index(p) % 3 != 2 or p in NEW_GUIDES else "2025-11-01"


def handler(log: list[str]):
    def handle(request: httpx.Request) -> httpx.Response:
        u = request.url
        log.append(str(u))
        host, path = u.host, u.path
        if host == SITE:
            if path == "/robots.txt":
                return httpx.Response(200, text=f"User-agent: *\nDisallow: /cart\nSitemap: https://{SITE}/sitemap_index.xml\n")
            if path == "/sitemap_index.xml":
                return httpx.Response(200, text=f"<sitemapindex><sitemap><loc>https://{SITE}/post-sitemap.xml</loc></sitemap>"
                                                f"<sitemap><loc>https://{SITE}/tag-sitemap.xml</loc></sitemap></sitemapindex>")
            if path == "/post-sitemap.xml":
                return httpx.Response(200, text=f"<urlset>{_rows(GUIDE, _guide_lastmod)}"
                                                f"{_rows(BLOG, lambda p: '2025-09-01')}</urlset>")
            if path == "/tag-sitemap.xml":
                return httpx.Response(200, text="<urlset>" + _rows(TAGS, lambda p: "2026-09-01" if TAGS.index(p) % 5 == 0
                                                                     else "2026-01-01") + "</urlset>")
            raw = u.raw_path.decode().split("?")[0]   # 比對百分比編碼的原始路徑（與 sitemap 中的網址一致）
            if raw in GUIDE:
                i = GUIDE.index(raw)
                new = raw in NEW_GUIDES
                updated = _guide_lastmod(raw).startswith("2026")
                return httpx.Response(200, text=page(f"高山茶 烏龍茶 指南 {i}", 30 if updated else 10,
                                                     "2026-08-20" if new else "2024-03-01",
                                                     "2026-09-10" if updated else "2025-11-01", faq=6 if updated else 0))
            if raw in BLOG:
                return httpx.Response(200, text=page(f"茶葉筆記 {BLOG.index(raw)}", 8, "2025-09-01", "2025-09-01"))
            if raw in TAGS:
                return httpx.Response(200, text=page("標籤彙整 高山茶", 3, "2023-01-01", "2026-09-01"))
            if path == "/":
                return httpx.Response(200, text=page("茶葉專賣", 2, "2020-01-01", "2026-01-01"))
            return httpx.Response(404, text="not found")
        if host == "web.archive.org" and path == "/cdx/search/cdx":
            target = parse_qs(u.query.decode())["url"][0]
            tpath = urlsplit(target).path
            if tpath in GUIDE and tpath not in NEW_GUIDES:
                return httpx.Response(200, json=[["timestamp", "statuscode"], ["20260601000000", "200"],
                                                 ["20260925000000", "200"]])
            return httpx.Response(200, json=[])
        if host == "web.archive.org" and path.startswith("/web/"):
            return httpx.Response(200, text=page("高山茶 指南（舊版）", 10, "2024-03-01", "2025-11-01"))
        if host == "serpapi.com":
            q = parse_qs(u.query.decode())
            assert q["api_key"] == ["test-key"]
            results = [{"position": 1, "link": f"https://{OTHER}/a"},
                       {"position": 2, "link": f"https://{SITE}{GUIDE[0]}"},
                       {"position": 3, "link": f"https://{SITE}{GUIDE[1]}"},
                       {"position": 4, "link": f"https://{OTHER}/b"},
                       {"position": 5, "link": f"https://{SITE}{GUIDE[3]}"}]
            return httpx.Response(200, json={"organic_results": results})
        return httpx.Response(404, text="unknown host")
    return handle


def client(log: list[str]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler(log)), follow_redirects=True)
