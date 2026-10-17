"""PolicyGate：自治邊界的強制執行（見 docs/06 §2）。

程式只負責辨識一個行動觸發了哪條規則；規則屬於「禁止」或「需核准」由 config/policy.yaml 決定。
LLM 的任何輸出都無法改變判定：行動只能由程式產生（工具名稱是封閉的型別），且必須通過這裡。

檢查順序：禁止類（不論誰同意都不做）→ 需核准類 → 放行。需核准時，若人已核准過這個行動（一次性授權）
就放行；若人拒絕過同一規則、同一範圍，就直接拒絕而不再詢問。
政策檔沒有列出的規則一律保守處理為「需核准」（fail-closed）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from seo_intel.config import PolicyConfig
from seo_intel.core import approvals
from seo_intel.core.budget import BudgetLedger
from seo_intel.core.models import Action, EpisodeState, TaskInput, action_scope
from seo_intel.tools.http import HttpToolkit

MIN_FETCHES_FOR_BLOCK_RATE = 10
EST_DISCOVER_FETCHES = 3   # robots + sitemap index + 至少一個子 sitemap
LOGIN_PATHS = re.compile(r"^/(account|cart|checkout|login|signin|sign-in|my-account|wp-admin)(/|$)", re.IGNORECASE)
# 行動參數中出現這些鍵，代表試圖改變身分或繞過防護：工具層本來就不支援，這裡明確拒絕並記錄
FORBIDDEN_ARGS = {"user_agent": "spoof_user_agent", "headers": "spoof_user_agent", "proxy": "rotate_ip",
                  "ip": "rotate_ip", "captcha_token": "bypass_captcha", "solve_challenge": "bypass_captcha",
                  "cookies": "access_login_gated", "credentials": "access_login_gated"}


class Verdict(BaseModel):
    decision: Literal["allow", "require_approval", "deny"]
    rule: str | None = None
    reason: str = ""


def action_domain(action: Action) -> str | None:
    return action_scope(action) or None


class PolicyGate:
    def __init__(self, cfg: PolicyConfig, task: TaskInput, http: HttpToolkit, ledger: BudgetLedger,
                 block_rate: Callable[[str], tuple[float, int]], state: EpisodeState | None = None,
                 known_url: Callable[[str], bool] | None = None) -> None:
        self.cfg = cfg
        self.task = task
        self.http = http
        self.ledger = ledger
        self.block_rate = block_rate   # domain → (封鎖率, 已抓取數)
        self.state = state
        self.known_url = known_url     # url → 是否由程式從 sitemap / 連結 / SERP 取得

    def classify(self, rule: str) -> Literal["allow", "require_approval", "deny"]:
        if rule in self.cfg.forbidden:
            return "deny"
        return "require_approval"   # 列在需核准清單，或政策沒有列出（保守處理）

    def _verdict(self, rule: str, reason: str) -> Verdict:
        decision = self.classify(rule)
        if decision == "require_approval" and rule not in self.cfg.require_approval:
            reason += "（此規則未列入政策，保守處理為需核准）"
        return Verdict(decision=decision, rule=rule, reason=reason)

    def _identify(self, action: Action) -> tuple[str, str] | None:
        """辨識行動觸發的規則（禁止類優先）。沒有觸發任何規則則回傳 None。"""
        for key, rule in FORBIDDEN_ARGS.items():
            if key in action.args:
                return rule, f"行動參數含 {key}：不得偽造身分、輪替 IP、繞過驗證或使用登入憑證"
        domain = action_domain(action)
        if action.tool == "fetch":
            url = action.args["url"]
            path = urlsplit(url).path
            if LOGIN_PATHS.match(path):
                return "access_login_gated", f"{path} 屬於需登入的區域"
            in_scope = domain in self.task.allowed_domains   # 範圍外的網域交給 off_target_domain：擴大範圍由人決定
            if in_scope and self.known_url is not None and not self.known_url(url):
                return "follow_page_instructions", "網址不是程式從 sitemap、連結或 SERP 取得的（可能來自頁面文字或 LLM 輸出）"
            if in_scope and not self.http.allowed(url):
                return "ignore_robots", f"robots.txt 禁止抓取 {path}"
        if domain and domain not in self.task.allowed_domains:
            return "off_target_domain", f"{domain} 不在任務指定的網域內"
        rate = action.args.get("rate_interval")
        if rate is not None and float(rate) < self.cfg.rate_limit_seconds_per_domain:
            return "raise_rate", f"抓取間隔 {rate}s 短於政策的 {self.cfg.rate_limit_seconds_per_domain}s"
        if int(action.args.get("pages", 1)) > self.cfg.bulk_crawl_threshold:
            return "bulk_crawl", f"單次抓取 {action.args['pages']} 頁超過門檻 {self.cfg.bulk_crawl_threshold}"
        if action.tool == "fetch":
            if not self.ledger.can_fetch(domain):
                return "exceed_budget", "抓取次數將超過預算上限"
            rate_b, n = self.block_rate(domain)
            if n >= MIN_FETCHES_FOR_BLOCK_RATE and rate_b > self.cfg.block_rate_threshold:
                return "continue_after_blocks", (f"{domain} 封鎖率 {rate_b:.0%} 超過門檻 "
                                                 f"{self.cfg.block_rate_threshold:.0%}")
        if action.tool == "discover" and not self.ledger.can_fetch(domain, EST_DISCOVER_FETCHES):
            return "exceed_budget", "探勘所需抓取次數將超過預算上限"
        if action.tool in ("rank_scan", "serp_scan", "history_diff") and not self.ledger.can_call_api(2):
            return "exceed_budget", "API 呼叫次數將超過預算上限"
        return None

    def check(self, action: Action) -> Verdict:
        hit = self._identify(action)
        if hit is None:
            return Verdict(decision="allow")
        v = self._verdict(*hit)
        st = self.state
        if v.decision != "require_approval" or st is None:
            return v
        if (d := approvals.denied(st, v.rule, action_scope(action))) is not None:
            return Verdict(decision="deny", rule=v.rule, reason=f"{v.reason}；人工已拒絕（{d['approval']}），不再詢問")
        if (g := approvals.take_grant(st, v.rule, action.signature)) is not None:
            return Verdict(decision="allow", rule=v.rule, reason=f"{v.reason}；人工已核准（{g['approval']}，一次性）")
        return v
