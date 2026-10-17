"""真實網站模式的搜尋結果（SERP）來源。

不直接爬 Google（違反服務條款、容易被封鎖），改用可插拔的來源：
- SerpApiProvider：透過 SerpApi 取得 Google 的真實結果（需要 .env 的 SERPAPI_API_KEY，按次計費）
- CsvProvider：使用者從自己的 SEO 工具（Semrush、Ahrefs、Search Console…）匯出的排名 CSV
沒有來源時，Agent 只分析競品網站本身。

每次取得的 SERP 都存進歷史檔；下次研究同一個關鍵字時就能算出名次變化——等於從現在開始自己累積排名歷史。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from seo_intel.tools.http import HttpToolkit

TOP_N = 100        # 沒出現在前 N 名視為第 N+1 名
SHOW_TOP = 10


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class SerpProvider(Protocol):
    name: str

    def search(self, keyword: str) -> list[dict]: ...   # [{"position", "url", "domain", "title"}]


class SerpApiProvider:
    name = "serpapi"
    ENDPOINT = "https://serpapi.com/search.json"

    def __init__(self, http: HttpToolkit, api_key: str, *, gl: str = "tw", hl: str = "zh-tw",
                 google_domain: str = "google.com.tw", num: int = TOP_N) -> None:
        self.http, self.api_key = http, api_key
        self.params = {"engine": "google", "gl": gl, "hl": hl, "google_domain": google_domain, "num": num}

    def search(self, keyword: str) -> list[dict]:
        res = self.http.get_api(f"{self.ENDPOINT}?{urlencode({**self.params, 'q': keyword, 'api_key': self.api_key})}")
        if not res.ok:
            raise RuntimeError(f"SerpApi：{res.error}")
        data = json.loads(res.text)
        if data.get("error"):
            raise RuntimeError(f"SerpApi：{data['error']}")
        return [{"position": int(r["position"]), "url": r["link"], "domain": host_of(r["link"]),
                 "title": r.get("title", "")} for r in data.get("organic_results", []) if r.get("link")]


_ALIASES = {
    "keyword": ("keyword", "query", "search term", "關鍵字", "查詢", "關鍵詞"),
    "position": ("position", "rank", "current position", "排名", "名次", "目前排名"),
    "url": ("url", "current url", "landing page", "page", "網址", "頁面", "到達網頁"),
}


class CsvProvider:
    """讀取排名 CSV：欄位名稱容許常見工具的寫法（Keyword / Position / URL、Current position、關鍵字 / 排名 / 網址…）。"""

    name = "csv"

    def __init__(self, text: str) -> None:
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
        pick = {k: next((cols[a] for a in names if a in cols), None) for k, names in _ALIASES.items()}
        if not all(pick.values()):
            missing = [k for k, v in pick.items() if v is None]
            raise ValueError(f"排名 CSV 缺少欄位：{', '.join(missing)}（需要關鍵字、排名、網址三欄）")
        self.rows: dict[str, list[dict]] = {}
        for row in reader:
            kw = (row[pick["keyword"]] or "").strip().lower()
            url = (row[pick["url"]] or "").strip()
            try:
                pos = int(float(row[pick["position"]]))
            except (TypeError, ValueError):
                continue
            if kw and url:
                self.rows.setdefault(kw, []).append({"position": pos, "url": url, "domain": host_of(url), "title": ""})

    @classmethod
    def from_path(cls, path: Path) -> CsvProvider:
        return cls(path.read_text(encoding="utf-8-sig"))

    def search(self, keyword: str) -> list[dict]:
        return sorted(self.rows.get(keyword.strip().lower(), []), key=lambda r: r["position"])


class SerpHistory:
    """每次 SERP 快照存成一行 JSON；用來計算下次研究時的名次變化。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def previous(self, keyword: str, before: date, source: str) -> dict | None:
        if not self.path.is_file():
            return None
        best = None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec["keyword"] == keyword and rec["source"] == source and rec["date"] < before.isoformat():
                if best is None or rec["date"] > best["date"]:
                    best = rec
        return best

    def save(self, keyword: str, today: date, source: str, results: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"date": today.isoformat(), "keyword": keyword, "source": source,
                                "results": results[:TOP_N]}, ensure_ascii=False) + "\n")


class RealRankClient:
    """真實網站模式的排名工具：只有「目前的 SERP」（加上自己累積的歷史），沒有逐日排名序列。"""

    real = True

    def __init__(self, provider: SerpProvider | None, today: date, history: SerpHistory | None) -> None:
        self.provider, self.today, self.history = provider, today, history

    def queries(self, site: str) -> list[str]:
        raise RuntimeError("真實網站模式沒有排名歷史（查詢清單與逐日排名）")

    def series(self, *a, **kw):
        raise RuntimeError("真實網站模式沒有排名歷史（查詢清單與逐日排名）")

    def serp_snapshot(self, keywords: list[str], targets: list[str]) -> dict:
        if self.provider is None:
            raise RuntimeError("沒有設定排名資料來源（SerpApi 或排名 CSV）")
        now: dict[str, list[dict]] = {}
        prev: dict[str, list[dict]] = {}
        prev_dates = set()
        for kw in keywords:
            rows = self.provider.search(kw)
            now[kw] = rows
            if self.history is not None:
                old = self.history.previous(kw, self.today, self.provider.name)
                if old:
                    prev[kw] = old["results"]
                    prev_dates.add(old["date"])
                self.history.save(kw, self.today, self.provider.name, rows)
        n = len(keywords) or 1

        def best(rows: list[dict], domain: str) -> int:
            return min((r["position"] for r in rows if r["domain"] == domain), default=TOP_N + 1)

        shown = {r["domain"] for rows in now.values() for r in rows[:SHOW_TOP]} | set(targets)
        movement = []
        for d in shown:
            after = sum(best(now[k], d) for k in keywords) / n
            before = sum(best(prev[k], d) for k in prev) / len(prev) if prev else None
            movement.append({"domain": d, "before": round(before, 2) if before is not None else None,
                             "after": round(after, 2), "gain": round(before - after, 2) if before is not None else 0.0})
        movement.sort(key=lambda m: (-m["gain"], m["after"]))
        gainers = ([m["domain"] for m in movement if m["gain"] >= 1.0] if prev else
                   [d for d in targets if any(best(rows[:SHOW_TOP], d) <= SHOW_TOP for rows in now.values())])
        return {
            "queries": keywords, "source": self.provider.name, "snapshot_only": not prev,
            "before": max(prev_dates) if prev_dates else None, "after": self.today.isoformat(),
            "movement": movement, "gainers": gainers,
            "target_urls_after": {d: sorted({r["url"] for rows in now.values() for r in rows if r["domain"] == d})
                                  for d in targets},
            "top": {kw: [{"position": r["position"], "domain": r["domain"], "url": r["url"]} for r in rows[:SHOW_TOP]]
                    for kw, rows in now.items()},
        }
