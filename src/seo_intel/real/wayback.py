"""真實網站的歷史版本：Internet Archive 的 Wayback Machine（免費、不需金鑰）。

介面：snapshots(url) 列出有快照的日期、snapshot(url, 日期) 取回當時的 HTML（供 tools/history.py 比對）。
用 `id_` 網址取得原始頁面（不含 Wayback 的工具列），並對 web.archive.org 保持禮貌的請求間隔。
"""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlencode

from seo_intel.tools.http import HttpToolkit

CDX = "https://web.archive.org/cdx/search/cdx"
WEB = "https://web.archive.org/web"
INTERVAL_S = 1.5


class WaybackClient:
    real = True

    def __init__(self, http: HttpToolkit) -> None:
        self.http = http
        self.http.api_interval.setdefault("web.archive.org", INTERVAL_S)
        self._ts: dict[str, dict[date, str]] = {}

    def snapshots(self, url: str) -> list[date]:
        q = {"url": url, "output": "json", "fl": "timestamp,statuscode", "filter": "statuscode:200",
             "collapse": "digest", "limit": "200"}
        res = self.http.get_api(f"{CDX}?{urlencode(q)}")
        if not res.ok or not (res.text or "").strip():
            return []
        try:
            rows = json.loads(res.text)
        except json.JSONDecodeError:
            return []
        stamps: dict[date, str] = {}
        for row in rows[1:]:   # 第一列是欄位名稱
            ts = row[0]
            if len(ts) >= 8 and ts[:8].isdigit():
                stamps[date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))] = ts
        self._ts[url] = stamps
        return sorted(stamps)

    def snapshot(self, url: str, d: date) -> str | None:
        ts = self._ts.get(url, {}).get(d) or f"{d:%Y%m%d}"
        res = self.http.get_api(f"{WEB}/{ts}id_/{url}")
        return res.text if res.ok else None
