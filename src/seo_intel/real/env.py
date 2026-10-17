"""真實網站研究的執行環境：真實時鐘、HTTP client、任務建立、工具（SERP 來源、Wayback、反向連結 CSV）。

設定存進 run.json 的 "env"，續跑時依此重建；API 金鑰只從環境變數讀取，不寫進任何檔案。
研究要回答三個問題（On-page 策略、Off-page 策略、優先行動），各自需要的資料不同；
data_gaps() 在開始前就說清楚「缺了什麼、哪個問題因此答不了」，介面、CLI 與報告都用同一份說明。
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
from seo_intel.real.backlinks import BacklinkData, bare
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


def research_question(competitors: list[str], keywords: list[str], own: str) -> str:
    who = f"自家網站 {own}" if own else "我們"
    return (f"競品 {'、'.join(competitors)} 在" + "".join(f"「{k}」" for k in keywords)
            + f"這些關鍵字上：(1) 採取了哪些關鍵的 On-page SEO 策略？(2) 採取了哪些關鍵的 Off-page SEO 策略？"
            + f"(3) {who}若要與之競爭，最值得優先採取的行動為何？")


def build_task(p: dict, client: httpx.Client | None = None, today: date | None = None,
               backlinks: BacklinkData | None = None) -> TaskInput:
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
    competitors = list(dict.fromkeys(competitors))
    linked = set(backlinks.domains()) if backlinks is not None else set()
    return TaskInput(question=research_question(competitors, keywords, own), own_site=own, competitors=competitors,
                     today=today, period_hint=(start, end),
                     site_language="zh-tw" if any(_CJK.search(k) for k in keywords) else "en",
                     keywords=keywords, serp_source=p.get("serp_source") or "none",
                     backlink_source="csv" if backlinks is not None else "none",
                     backlink_domains=[d for d in (*competitors, own) if d and bare(d) in linked])


def env_config(p: dict, csv_path: Path | None = None) -> dict:
    """存進 run.json 的環境設定（不含金鑰）。"""
    return {"serp_source": p.get("serp_source") or "none",
            "csv_path": str(csv_path) if csv_path else None,
            "backlink_paths": [str(Path(x)) for x in p.get("backlink_paths") or []]}


def load_backlinks(env: dict, task_domains: list[str] | None = None) -> BacklinkData | None:
    """讀取反向連結 CSV（沒有提供時回傳 None）。開始前就檢查：檔案存在、欄位正確、有指向任務網站的連結。"""
    paths = [Path(x) for x in env.get("backlink_paths") or []]
    if not paths:
        return None
    missing = [p.name for p in paths if not p.is_file()]
    if missing:
        raise ValueError(f"找不到反向連結 CSV：{', '.join(missing)}")
    data = BacklinkData.from_paths(paths)
    if not data.links:
        raise ValueError("反向連結 CSV 沒有任何有效的連結（需要來源網址與目標網址兩欄）")
    if task_domains:
        wanted = {bare(d) for d in task_domains if d}
        if not wanted & set(data.domains()):
            top = "、".join(d for d, _ in data.domains().most_common(3))
            raise ValueError(f"反向連結 CSV 裡沒有指向競品或自家網站的連結（檔案中的目標網域：{top}）")
    return data


def data_gaps(task: TaskInput) -> list[str]:
    """缺少的資料與它影響的問題（開始前提醒、報告的限制都用這份說明）。"""
    gaps = []
    if task.serp_source == "none":
        gaps.append("沒有排名資料（SerpApi 或排名 CSV）：Agent 不知道哪些頁面真的有排名，只能從 sitemap 推測主題頁；"
                    "On-page 策略無法比較「有排名」與「沒排名」的頁面，結論會弱很多")
    if task.backlink_source == "none":
        gaps.append("沒有反向連結 CSV：無法研究 Off-page 策略（反向連結無法靠爬網站取得）")
    else:
        lacking = [c for c in task.competitors if c not in task.backlink_domains]
        if lacking:
            gaps.append(f"反向連結 CSV 沒有 {'、'.join(lacking)} 的資料：這些競品的 Off-page 策略無法研究")
    if not task.own_site:
        gaps.append("沒有自家網址：優先行動只能依競品的做法排序，無法和自家網站比較差距")
    elif task.backlink_source == "csv" and task.own_site not in task.backlink_domains:
        gaps.append("反向連結 CSV 沒有自家網站的資料：Off-page 的行動無法和自家比較")
    return gaps


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
