"""PolicyGate：自治邊界的強制執行（Phase 2 基本版：robots、目標網域、預算、規模、封鎖率）。

程式只負責辨識一個行動觸發了哪條規則；規則屬於「禁止」或「需核准」由 config/policy.yaml 決定。
LLM 的任何輸出都無法改變判定，因為行動只能由程式產生、且必須通過這裡。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from seo_intel.config import PolicyConfig
from seo_intel.core.budget import BudgetLedger
from seo_intel.core.models import Action, TaskInput
from seo_intel.tools.http import HttpToolkit

MIN_FETCHES_FOR_BLOCK_RATE = 10
EST_DISCOVER_FETCHES = 3   # robots + sitemap index + 至少一個子 sitemap


class Verdict(BaseModel):
    decision: Literal["allow", "require_approval", "deny"]
    rule: str | None = None
    reason: str = ""


def action_domain(action: Action) -> str | None:
    if "url" in action.args:
        return urlsplit(action.args["url"]).hostname
    return action.args.get("domain") or action.args.get("site")


class PolicyGate:
    def __init__(self, cfg: PolicyConfig, task: TaskInput, http: HttpToolkit, ledger: BudgetLedger,
                 block_rate: Callable[[str], tuple[float, int]]) -> None:
        self.cfg = cfg
        self.task = task
        self.http = http
        self.ledger = ledger
        self.block_rate = block_rate   # domain → (封鎖率, 已抓取數)

    def _verdict(self, rule: str, reason: str) -> Verdict:
        if rule in self.cfg.forbidden:
            return Verdict(decision="deny", rule=rule, reason=reason)
        if rule in self.cfg.require_approval:
            return Verdict(decision="require_approval", rule=rule, reason=reason)
        return Verdict(decision="allow", rule=rule, reason=f"規則 {rule} 未列入政策，放行")

    def check(self, action: Action) -> Verdict:
        domain = action_domain(action)
        if domain and domain not in self.task.allowed_domains:
            return self._verdict("off_target_domain", f"{domain} 不在任務指定的網域內")
        if action.tool == "fetch":
            url = action.args["url"]
            if not self.http.allowed(url):
                return self._verdict("ignore_robots", f"robots.txt 禁止抓取 {urlsplit(url).path}")
            if not self.ledger.can_fetch(domain):
                return self._verdict("exceed_budget", "抓取次數將超過預算上限")
            rate, n = self.block_rate(domain)
            if n >= MIN_FETCHES_FOR_BLOCK_RATE and rate > self.cfg.block_rate_threshold:
                return self._verdict("continue_after_blocks",
                                     f"{domain} 封鎖率 {rate:.0%} 超過門檻 {self.cfg.block_rate_threshold:.0%}")
        if action.tool == "discover" and not self.ledger.can_fetch(domain, EST_DISCOVER_FETCHES):
            return self._verdict("exceed_budget", "探勘所需抓取次數將超過預算上限")
        if action.tool in ("rank_scan", "serp_scan", "history_diff") and not self.ledger.can_call_api(2):
            return self._verdict("exceed_budget", "API 呼叫次數將超過預算上限")
        if int(action.args.get("pages", 1)) > self.cfg.bulk_crawl_threshold:
            return self._verdict("bulk_crawl", f"單次抓取 {action.args['pages']} 頁超過門檻")
        return Verdict(decision="allow")
