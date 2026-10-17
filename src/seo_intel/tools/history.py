"""歷史快照工具（SimWeb 中模擬 Wayback）：取得頁面在指定日期前的版本並與目前版本比較。"""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlencode

from seo_intel.tools.diff import diff_features
from seo_intel.tools.extract import PageFeatures, extract
from seo_intel.tools.http import HttpToolkit


class ArchiveClient:
    def __init__(self, toolkit: HttpToolkit, base: str = "https://archive.sim") -> None:
        self.http = toolkit
        self.base = base

    def snapshots(self, url: str) -> list[date]:
        res = self.http.get_api(f"{self.base}/cdx?{urlencode({'url': url})}")
        if not res.ok:
            return []
        return [date(int(s["timestamp"][:4]), int(s["timestamp"][4:6]), int(s["timestamp"][6:8]))
                for s in json.loads(res.text)["snapshots"]]

    def snapshot(self, url: str, d: date) -> str | None:
        res = self.http.get_api(f"{self.base}/web/{d:%Y%m%d}/{url}")
        return res.text if res.ok else None


def history_diff(ac: ArchiveClient, url: str, current: PageFeatures, before: date) -> dict:
    """before：要比較的基準日（取該日之前最近的快照）。"""
    snaps = ac.snapshots(url)
    prior = [d for d in snaps if d < before]
    if not prior:
        return {"url": url, "status": "no_snapshot_before", "n_snapshots": len(snaps),
                "first_snapshot": snaps[0].isoformat() if snaps else None}
    html = ac.snapshot(url, prior[-1])
    if html is None:
        return {"url": url, "status": "snapshot_unavailable", "snapshot_date": prior[-1].isoformat()}
    old = extract(html, url)
    return {"url": url, "status": "ok", "snapshot_date": prior[-1].isoformat(),
            "current_modified": current.modified, **diff_features(old, current)}
