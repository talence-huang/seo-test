"""HTTP 工具包裝層：所有對外請求的唯一出口。

依序套用：快取 → robots 檢查 → 斷路器 → 預算 → 速率限制（取 policy 與 crawl-delay 較大者）
→ 請求 → 重試（僅 429 / 5xx / 逾時，遵守 Retry-After）。每次嘗試都留下紀錄供 trace 使用。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from seo_intel.core.budget import BudgetLedger

USER_AGENT = "seo-intel-research/0.1 (+research agent; respects robots.txt)"
SECRET_PARAMS = {"api_key", "apikey", "key", "token", "access_token", "auth"}


def redact(url: str) -> str:
    """寫進 trace / 資料庫前遮蔽網址中的金鑰類參數（例如 SerpApi 的 api_key）。"""
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [(k, "***" if k.lower() in SECRET_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return parts._replace(query=urlencode(q)).geturl()


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass
class HttpResult:
    url: str
    status: int | None
    text: str | None
    error: str | None = None
    attempts: int = 0
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.text is not None


@dataclass
class AttemptRecord:
    url: str
    status: int | None
    error: str | None
    attempt: int
    t: float
    waited_s: float = 0.0


@dataclass
class HttpToolkit:
    client: httpx.Client
    clock: Clock
    ledger: BudgetLedger
    rate_interval: float = 2.0
    max_attempts: int = 3
    breaker_threshold: int = 5
    attempts_log: list[AttemptRecord] = field(default_factory=list)
    api_interval: dict[str, float] = field(default_factory=dict)   # 對外部 API 的禮貌間隔（例如 Wayback）
    host_counts: Counter = field(default_factory=Counter)          # 每個主機的請求數（真實網站的環境紀錄）
    status_counts: Counter = field(default_factory=Counter)
    _robots: dict[str, RobotFileParser | None] = field(default_factory=dict)
    _next_ok: dict[str, float] = field(default_factory=dict)
    _cache: dict[str, HttpResult] = field(default_factory=dict)
    _fails: Counter = field(default_factory=Counter)

    # ---------- robots ----------

    def robots(self, domain: str) -> RobotFileParser | None:
        if domain not in self._robots:
            res = self._request(f"https://{domain}/robots.txt", site=True)
            parser = None
            if res.ok:
                parser = RobotFileParser()
                parser.parse(res.text.splitlines())
            self._robots[domain] = parser
        return self._robots[domain]

    def allowed(self, url: str) -> bool:
        parser = self.robots(urlsplit(url).hostname or "")
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    def interval(self, domain: str) -> float:
        parser = self._robots.get(domain)
        delay = parser.crawl_delay(USER_AGENT) if parser else None
        return max(self.rate_interval, float(delay or 0))

    def breaker_open(self, domain: str) -> bool:
        return self._fails[domain] >= self.breaker_threshold

    # ---------- 對外介面 ----------

    def get_site(self, url: str) -> HttpResult:
        if url in self._cache:
            cached = self._cache[url]
            return HttpResult(cached.url, cached.status, cached.text, cached.error, 0, from_cache=True)
        domain = urlsplit(url).hostname or ""
        if not self.allowed(url):
            return HttpResult(url, None, None, "robots_disallowed")
        if self.breaker_open(domain):
            return HttpResult(url, None, None, "circuit_open")
        return self._request(url, site=True)

    def get_api(self, url: str) -> HttpResult:
        if url in self._cache:
            cached = self._cache[url]
            return HttpResult(cached.url, cached.status, cached.text, cached.error, 0, from_cache=True)
        return self._request(url, site=False)

    def drain_attempts(self) -> list[AttemptRecord]:
        out, self.attempts_log = self.attempts_log, []
        return out

    # ---------- 核心 ----------

    def _request(self, url: str, *, site: bool) -> HttpResult:
        domain = urlsplit(url).hostname or ""
        status: int | None = None
        error: str | None = None
        text: str | None = None
        attempt = 0
        for attempt in range(1, self.max_attempts + 1):
            waited = 0.0
            if site:
                if not self.ledger.can_fetch(domain):
                    return HttpResult(url, None, None, "budget_exhausted", attempt - 1)
                waited = max(0.0, self._next_ok.get(domain, 0.0) - self.clock.now())
                if waited:
                    waited += 0.001   # 多等 1ms，避免浮點誤差讓間隔略短於 crawl-delay
                    self.clock.sleep(waited)
                self._next_ok[domain] = self.clock.now() + self.interval(domain)
                self.ledger.charge_fetch(domain)
            else:
                if not self.ledger.can_call_api():
                    return HttpResult(url, None, None, "budget_exhausted", attempt - 1)
                if (gap := self.api_interval.get(domain)) is not None:
                    waited = max(0.0, self._next_ok.get(domain, 0.0) - self.clock.now())
                    if waited:
                        self.clock.sleep(waited)
                    self._next_ok[domain] = self.clock.now() + gap
                self.ledger.charge_api()
            self.host_counts[domain] += 1
            try:
                r = self.client.get(url, headers={"user-agent": USER_AGENT})
            except httpx.TimeoutException:
                status, error, text = None, "timeout", None
                self.attempts_log.append(AttemptRecord(redact(url), None, error, attempt, self.clock.now(), waited))
                self.clock.sleep(2 ** attempt)
                continue
            except httpx.HTTPError as e:   # 連線失敗多半是暫時性的：與逾時一樣退避後重試
                status, error, text = None, f"connect_error: {e}", None
                self.attempts_log.append(AttemptRecord(redact(url), None, error, attempt, self.clock.now(), waited))
                self.clock.sleep(2 ** attempt)
                continue
            status, text = r.status_code, r.text
            self.status_counts[status] += 1
            error = None if status == 200 else f"http_{status}"
            self.attempts_log.append(AttemptRecord(redact(url), status, error, attempt, self.clock.now(), waited))
            if status == 429:
                retry_after = float(r.headers.get("retry-after", "5"))
                if site:
                    self._next_ok[domain] = self.clock.now() + retry_after
                else:
                    self.clock.sleep(retry_after)
                continue
            if status >= 500:
                self.clock.sleep(2 ** attempt)
                continue
            break

        result = HttpResult(url, status, text if status == 200 else None, error, attempt)
        if site:
            if result.ok or status == 404:
                self._fails[domain] = 0
            elif status in (403, 429) or status is None or (status and status >= 500):
                self._fails[domain] += 1
        if status in (200, 403, 404):
            self._cache[url] = result
        return result
