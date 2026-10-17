"""把每一步寫入 SQLite（episodes / steps / llm_calls / tool_calls / observations）。"""

from __future__ import annotations

import json

from seo_intel.core.models import EpisodeState
from seo_intel.observe.record import StepRecord
from seo_intel.store import Database
from seo_intel.store.db import utc_now


class DbRecorder:
    def __init__(self, db: Database, state: EpisodeState) -> None:
        self.db = db
        self.state = state
        if not db.query("SELECT id FROM episodes WHERE id = ?", (state.id,)):
            db.insert("episodes", {
                "id": state.id, "scenario": state.task.scenario, "goal_json": state.task.model_dump(mode="json"),
                "status": state.status, "budget_json": state.budget.model_dump(), "started_at": utc_now(),
            })

    def truncate(self, max_seq: int) -> None:
        with self.db.transaction() as conn:
            step_ids = [r[0] for r in conn.execute("SELECT id FROM steps WHERE episode_id = ? AND seq > ?",
                                                   (self.state.id, max_seq))]
            for sid in step_ids:
                conn.execute("DELETE FROM llm_calls WHERE step_id = ?", (sid,))
                conn.execute("DELETE FROM tool_calls WHERE step_id = ?", (sid,))
            conn.execute("DELETE FROM steps WHERE episode_id = ? AND seq > ?", (self.state.id, max_seq))
            keep = {f"{self.state.id}/{o.id}" for o in self.state.observations}
            for (oid,) in conn.execute("SELECT id FROM observations WHERE episode_id = ?", (self.state.id,)).fetchall():
                if oid not in keep:
                    conn.execute("DELETE FROM observations WHERE id = ?", (oid,))

    def emit(self, r: StepRecord) -> None:
        now = utc_now()
        step_id = self.db.insert("steps", {
            "episode_id": r.episode_id, "seq": r.seq, "step": r.step,
            "decision_json": {"summary": r.summary, "details": r.details, "decision": r.decision,
                              "candidates": r.candidates},
            "reason": r.reason, "budget_after_json": r.budget, "ts": now,
        })
        for rec in r.llm:
            if not rec.get("model"):
                continue
            u = rec.get("usage", {})
            self.db.insert("llm_calls", {
                "episode_id": r.episode_id, "step_id": step_id, "role": rec["role"], "model": rec["model"],
                "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
                "cache_read_tokens": u.get("cache_read_tokens", 0), "cache_write_tokens": u.get("cache_write_tokens", 0),
                "usd": rec.get("usd", 0.0), "stop_reason": rec.get("stop_reason"),
                "served_by_fallback": rec.get("served_by_fallback", False), "request_id": rec.get("request_id"),
                "latency_ms": rec.get("latency_ms"), "output_json": rec.get("output") or {"error": rec.get("error")},
                "ts": now,
            })
        tool = (r.decision.get("action") or {}).get("tool", "http")
        for call in r.tool_calls:
            self.db.insert("tool_calls", {
                "episode_id": r.episode_id, "step_id": step_id, "tool": tool,
                "args_json": {"url": call["url"], "attempt": call["attempt"], "waited_s": call["waited_s"]},
                "status": "ok" if call["status"] == 200 else "error", "error": call["error"], "ts": now,
            })
        by_id = {o.id: o for o in self.state.observations}
        for o in r.observations:
            full = by_id[o["id"]]
            self.db.insert("observations", {
                "id": f"{r.episode_id}/{full.id}", "episode_id": r.episode_id, "kind": full.kind,
                "data_json": {"summary": full.summary, "url": full.url, "need_id": full.need_id, **full.data},
                "ts": now,
            })

    def finish(self, state: EpisodeState) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE episodes SET status = ?, budget_json = ?, ended_at = ? WHERE id = ?",
                         (state.status, state.budget.model_dump_json(),
                          utc_now() if state.status in ("done", "failed") else None, state.id))
        record_approvals(self.db, state)
        if state.status != "done":
            return
        now, eid = utc_now(), state.id
        with self.db.transaction() as conn:   # 結束時寫入研究結論（續跑時先清除舊紀錄）
            hyp_ids = [f"{eid}/{h.id}" for h in state.hypotheses]
            for hid in hyp_ids:
                conn.execute("DELETE FROM evidence WHERE hypothesis_id = ?", (hid,))
            conn.execute("DELETE FROM hypotheses WHERE episode_id = ?", (eid,))
            conn.execute("DELETE FROM claims WHERE episode_id = ?", (eid,))
        for h in state.hypotheses:
            self.db.insert("hypotheses", {
                "id": f"{eid}/{h.id}", "episode_id": eid, "category": h.category, "statement": h.statement,
                "prediction": h.prediction, "falsification": h.falsification, "prior": h.prior,
                "posterior": min(0.999, max(0.001, h.posterior)), "status": h.status, "created_at": now, "updated_at": now,
            })
        for e in state.evidence:
            for oid in e.observation_ids:
                self.db.insert("evidence", {
                    "hypothesis_id": f"{eid}/{e.hypothesis_id}", "observation_id": f"{eid}/{oid}",
                    "direction": e.direction, "strength": e.strength, "note": f"{e.id} [{e.probe}] {e.note}", "ts": now,
                })
        for c in state.claims:
            self.db.insert("claims", {
                "id": f"{eid}/{c.id}", "episode_id": eid, "claim_json": c.model_dump(mode="json"),
                "confidence": c.confidence, "evidence_ids_json": c.evidence_ids, "validated": c.validated,
            })


def record_approvals(db: Database, state: EpisodeState) -> None:
    """核准請求的稽核紀錄（決定以檢查點為準，這裡是可查詢的鏡像）。"""
    with db.transaction() as conn:
        for a in state.approvals:
            action = a.action.model_dump(mode="json") if a.action else {"kind": "budget", "extend": a.extend}
            conn.execute(
                """INSERT OR REPLACE INTO approvals (id, episode_id, action_json, reason, est_cost_json, status, note,
                       created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a.id, state.id, json.dumps(action, ensure_ascii=False), f"[{a.rule}] {a.reason}",
                 json.dumps(a.estimate, ensure_ascii=False), a.status, a.note, a.created_at, a.expires_at))
