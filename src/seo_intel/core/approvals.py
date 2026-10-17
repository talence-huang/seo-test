"""人工核准：請求、決定、續跑時套用（見 docs/06 §2.4）。

- 請求：PolicyGate 判定「需核准」，或預算用盡但還有待完成的驗證工作時建立；episode 存檔後硬暫停。
- 決定：人用 `seo-intel approvals approve / reject`（或 --step 模式當場）決定，寫回檢查點。
- 套用：續跑時把核准轉成「一次性授權」或「預算延長」；把拒絕轉成「拒絕範圍」——同一規則、同一範圍之後
  直接拒絕、不再詢問（避免核准疲勞），並把未完成的工作寫進報告的「限制」。
- 核准有期限（policy.yaml 的 approval_ttl_hours）；過期視同未核准，續跑時會重新請求。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from seo_intel.core.models import Action, Approval, EpisodeState

if TYPE_CHECKING:   # 避免循環匯入（context → policy → approvals）
    from seo_intel.core.context import EpisodeContext

RESOURCE_ZH = {"max_fetches": "抓取次數", "max_llm_usd": "LLM 花費（US$）", "max_wall_time_s": "執行時間（秒）"}


class ApprovalPending(Exception):
    """還沒有人做決定，不能續跑。"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _expired(a: Approval, now: datetime) -> bool:
    return now > datetime.fromisoformat(a.expires_at)


def request(ctx: EpisodeContext, rule: str, reason: str, *, action: Action | None = None,
            extend: dict[str, float] | None = None, estimate: dict | None = None,
            now: datetime | None = None) -> Approval:
    now = now or utc_now()
    a = Approval(id=f"A-{secrets.token_hex(3)}", rule=rule, reason=reason, kind="action" if action else "budget",
                 action=action, extend=extend or {}, estimate=estimate or {}, created_at=_iso(now),
                 expires_at=_iso(now + timedelta(hours=ctx.settings.policy.approval_ttl_hours)))
    ctx.state.approvals.append(a)
    ctx.state.status = "waiting_approval"
    return a


def denied(st: EpisodeState, rule: str, scope: str) -> dict | None:
    return next((d for d in st.denials if d["rule"] == rule and d["scope"] == scope), None)


def take_grant(st: EpisodeState, rule: str, signature: str) -> dict | None:
    """一次性授權：用掉就失效。"""
    g = next((g for g in st.grants if g["rule"] == rule and g["signature"] == signature), None)
    if g is not None:
        st.grants.remove(g)
        a = next((x for x in st.approvals if x.id == g["approval"]), None)
        if a is not None:
            a.status = "used"
    return g


def find(st: EpisodeState, approval_id: str | None) -> Approval | None:
    if approval_id is None:
        return st.approvals[-1] if st.approvals else None
    return next((a for a in st.approvals if a.id == approval_id), None)


def decide(st: EpisodeState, approval_id: str | None, approve: bool, note: str | None = None,
           now: datetime | None = None) -> Approval:
    now = now or utc_now()
    a = find(st, approval_id)
    if a is None or a.status != "pending":
        raise ValueError(f"沒有待決定的核准請求{f' {approval_id}' if approval_id else ''}"
                         + (f"（目前狀態：{a.status}）" if a else ""))
    a.decided_at, a.note = _iso(now), note
    if _expired(a, now):
        a.status = "expired"
        return a
    a.status = "approved" if approve else "rejected"
    return a


def resume(ctx: EpisodeContext, now: datetime | None = None) -> list[str]:
    """續跑前套用人工決定，回傳要寫入 trace 的說明。尚未決定則丟出 ApprovalPending。"""
    st = ctx.state
    if st.status != "waiting_approval":
        return []
    now = now or utc_now()
    a = st.approvals[-1]
    lines = [f"{a.id}（{a.rule}）：{a.reason}"]
    if a.status == "pending" and not _expired(a, now):
        raise ApprovalPending(f"{a.id} 尚未決定：請先執行 seo-intel approvals approve/reject {a.id}")
    if a.status in ("pending", "approved") and _expired(a, now):
        a.status = "expired"
        lines.append(f"已超過期限 {a.expires_at}，視同未核准；若仍需要，Agent 會重新請求")
    elif a.status == "approved" and a.kind == "budget":
        for k, v in a.extend.items():
            st.budget.extra[k] = st.budget.extra.get(k, 0) + v
            lines.append(f"人工核准：{RESOURCE_ZH.get(k, k)}上限延長 {v:g}（新上限 {ctx.ledger.cap(k):g}）")
        a.status = "used"
    elif a.status == "approved":
        st.grants.append({"approval": a.id, "rule": a.rule, "signature": a.action.signature})
        lines.append(f"人工核准：允許執行一次 {a.action.label()}")
    elif a.status == "rejected":
        st.denials.append({"approval": a.id, "rule": a.rule, "scope": a.scope_key})
        what = RESOURCE_ZH.get(a.scope_key, a.scope_key)
        st.limitations.append(f"人工拒絕 {a.id}（{a.rule}，{what}）：{a.reason}"
                              + (f"；備註：{a.note}" if a.note else ""))
        lines.append(f"人工拒絕：之後 {a.rule}（{what}）直接拒絕、不再詢問，改走降級路徑")
    if a.note:
        lines.append(f"備註：{a.note}")
    st.status = "running"
    return lines
