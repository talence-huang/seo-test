"""歷史快照（模擬 Wayback Machine）。

只有部分頁面被收錄（依範本的 archived_rate）；快照時間 = 各版本日期後 0–4 天，
加上每 60–150 天一次的例行擷取。因此變動日期只能確定在「兩個快照之間」。
"""

from __future__ import annotations

from datetime import date, timedelta

from simweb.rng import h64, unit
from simweb.world import Page, World


class Archive:
    def __init__(self, world: World) -> None:
        self.w = world

    def is_archived(self, page: Page) -> bool:
        t = self.w.sites[page.domain].templates[page.template]
        return unit(self.w.seed, page.url, "archived") < t.archived_rate

    def snapshots(self, page: Page) -> list[date]:
        if not self.is_archived(page):
            return []
        dates = {v.date + timedelta(days=h64(self.w.seed, page.url, v.date, "cap") % 5) for v in self.w.versions(page)}
        step = 60 + h64(self.w.seed, page.url, "step") % 90
        d = page.created + timedelta(days=step)
        while d <= self.w.now:
            dates.add(d)
            d += timedelta(days=step)
        return sorted(x for x in dates if page.created <= x <= self.w.now)

    def digest(self, page: Page, d: date) -> str:
        v = self.w.version_at(page, d)
        return f"{h64(page.url, v.date if v else 'none'):016x}"

    def snapshot_on_or_before(self, page: Page, d: date) -> date | None:
        candidates = [s for s in self.snapshots(page) if s <= d]
        return candidates[-1] if candidates else None
