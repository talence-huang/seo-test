"""真實網站研究的執行環境：真實時鐘、HTTP client、任務建立、工具（SERP 來源與 Wayback）。

設定存進 run.json 的 "env"，續跑時依此重建；API 金鑰只從環境變數讀取，不寫進任何檔案。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from seo_intel.config import PROJECT_ROOT
from seo_intel.core.models import TaskInput
from seo_intel.real.serp import CsvProvider, RealRankClient, SerpApiProvider, SerpHistory
from seo_intel.real.wayback import WaybackClient
from seo_intel.tools.http import USER_AGENT, HttpToolkit

SERP_HISTORY = PROJECT_ROOT / "data" / "serp_history.jsonl"
DEFAULT_PERIOD_DAYS = 90
_CJK = re.compile(r"[㐀-鿿]")
_DOMAIN = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


def real_client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(20.0), headers={"user-agent": USER_AGENT})


def parse_domain(raw: str) -> str:
    """接受網址或網域（https://2030.tw、2030.tw/abc、www.example.com），回傳主機名稱。"""
    raw = raw.strip()
    host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
    if not _DOMAIN.match(host):
        raise ValueError(f"看不懂的網址或網域：{raw}")
    return host


def canonical_host(client: httpx.Client, domain: str) -> str:
    """請求一次首頁、跟隨轉址，取得網站實際使用的主機名稱（例如 2030.tw → www.2030.tw）。"""
    try:
        r = client.get(f"https://{domain}/")
        return (r.url.host or domain).lower()
    except httpx.HTTPError:
        return domain


def build_task(p: dict, client: httpx.Client | None = None, today: date | None = None) -> TaskInput:
    competitors = [parse_domain(x) for x in p.get("competitors", []) if x.strip()]
    keywords = [k.strip() for k in p.get("keywords", []) if k.strip()]
    if not competitors or not keywords:
        raise ValueError("至少需要一個競品網址與一個關鍵字")
    own = parse_domain(p["own_site"]) if (p.get("own_site") or "").strip() else ""
    if client is not None and p.get("resolve", True):
        competitors = [canonical_host(client, d) for d in competitors]
        own = canonical_host(client, own) if own else ""
    today = today or date.today()
    start = date.fromisoformat(p["start"]) if p.get("start") else today - timedelta(days=DEFAULT_PERIOD_DAYS)
    end = date.fromisoformat(p["end"]) if p.get("end") else today
    if start >= end:
        raise ValueError("研究期間的開始日必須早於結束日")
    question = (f"競品 {'、'.join(competitors)} 在" + "".join(f"「{k}」" for k in keywords)
                + "相關的頁面做了什麼內容策略？哪些頁面範本與內容變化可能讓它們在這些關鍵字上取得排名？")
    return TaskInput(question=question, own_site=own, competitors=list(dict.fromkeys(competitors)), today=today,
                     period_hint=(start, end), site_language="zh-tw" if any(_CJK.search(k) for k in keywords) else "en",
                     keywords=keywords, serp_source=p.get("serp_source") or "none")


def env_config(p: dict, csv_path: Path | None = None) -> dict:
    """存進 run.json 的環境設定（不含金鑰）。"""
    return {"serp_source": p.get("serp_source") or "none",
            "csv_path": str(csv_path) if csv_path else None}


def check_serp(env: dict) -> None:
    """開始前就檢查排名來源可用，避免研究跑到一半才失敗。"""
    if env["serp_source"] == "serpapi" and not os.environ.get("SERPAPI_API_KEY"):
        raise ValueError("使用 SerpApi 需要在 .env 填入 SERPAPI_API_KEY")
    if env["serp_source"] == "csv":
        if not env.get("csv_path") or not Path(env["csv_path"]).is_file():
            raise ValueError("找不到排名 CSV")
        CsvProvider.from_path(Path(env["csv_path"]))   # 欄位不對時在這裡就報錯


def tools_factory(env: dict, today: date, history: Path = SERP_HISTORY) -> Callable[[HttpToolkit], tuple]:
    def make(http: HttpToolkit) -> tuple:
        src = env["serp_source"]
        provider = None
        if src == "serpapi":
            provider = SerpApiProvider(http, os.environ.get("SERPAPI_API_KEY", ""))
        elif src == "csv":
            provider = CsvProvider.from_path(Path(env["csv_path"]))
        return RealRankClient(provider, today, SerpHistory(history)), WaybackClient(http)
    return make
