"""歷史快照比對：取得頁面在指定日期前的版本（Wayback）並與目前版本比較。"""

from __future__ import annotations

from datetime import date

from seo_intel.tools.diff import diff_features
from seo_intel.tools.extract import PageFeatures, extract


def history_diff(ac, url: str, current: PageFeatures, before: date) -> dict:
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
