"""記憶資料庫存取（memory.db）。所有經驗狀態變更都寫入 lesson_events 稽核軌跡。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from importlib import resources
from pathlib import Path

from seo_intel.memory.models import Bias, Lesson
from seo_intel.store.db import utc_now


class MemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 網頁介面在請求執行緒開啟、在背景工作執行緒使用；同一時間只有一個工作使用同一份記憶
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        sql = resources.files("seo_intel.memory").joinpath("schema.sql").read_text(encoding="utf-8")
        self.conn.executescript(sql)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def snapshot(self, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.conn.commit()
        backup = sqlite3.connect(str(dest))
        self.conn.backup(backup)
        backup.close()
        return dest

    @staticmethod
    def copy(src: str | Path, dest: str | Path) -> Path:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return Path(dest)

    # ---------- 經驗 ----------

    def _row_to_lesson(self, r: sqlite3.Row) -> Lesson:
        return Lesson(id=r["id"], signature=r["signature"], source=r["source"], scope=json.loads(r["scope_json"]),
                      trigger=r["trigger"], lesson=r["lesson"],
                      biases=[Bias.model_validate(b) for b in json.loads(r["biases_json"])],
                      status=r["status"], support=r["support"], applied=r["applied"],
                      applied_success=r["applied_success"], conflicts=r["conflicts"],
                      provenance=json.loads(r["provenance_json"]), retired_reason=r["retired_reason"])

    def lessons(self, status: str | None = None) -> list[Lesson]:
        sql = "SELECT * FROM lessons" + (" WHERE status = ?" if status else "") + " ORDER BY id"
        return [self._row_to_lesson(r) for r in self.conn.execute(sql, (status,) if status else ())]

    def lesson(self, lesson_id: str) -> Lesson | None:
        r = self.conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        return self._row_to_lesson(r) if r else None

    def by_signature(self, signature: str) -> Lesson | None:
        r = self.conn.execute("SELECT * FROM lessons WHERE signature = ?", (signature,)).fetchone()
        return self._row_to_lesson(r) if r else None

    def next_lesson_id(self) -> str:
        n = self.conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        return f"L-{n + 1}"

    def save_lesson(self, lesson: Lesson) -> None:
        now = utc_now()
        self.conn.execute(
            """INSERT INTO lessons (id, signature, source, scope_json, trigger, lesson, biases_json, status, support,
                   applied, applied_success, conflicts, provenance_json, retired_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET scope_json = excluded.scope_json, trigger = excluded.trigger,
                   lesson = excluded.lesson, biases_json = excluded.biases_json, status = excluded.status,
                   support = excluded.support, applied = excluded.applied, applied_success = excluded.applied_success,
                   conflicts = excluded.conflicts, provenance_json = excluded.provenance_json,
                   retired_reason = excluded.retired_reason, updated_at = excluded.updated_at""",
            (lesson.id, lesson.signature, lesson.source, json.dumps(lesson.scope, ensure_ascii=False), lesson.trigger,
             lesson.lesson, json.dumps([b.model_dump() for b in lesson.biases], ensure_ascii=False), lesson.status,
             lesson.support, lesson.applied, lesson.applied_success, lesson.conflicts,
             json.dumps(lesson.provenance, ensure_ascii=False), lesson.retired_reason, now, now))
        self.conn.commit()

    def event(self, lesson_id: str, episode_id: str | None, event: str, detail: str = "") -> None:
        self.conn.execute("INSERT INTO lesson_events (lesson_id, episode_id, event, detail, ts) VALUES (?, ?, ?, ?, ?)",
                          (lesson_id, episode_id, event, detail, utc_now()))
        self.conn.commit()

    def events(self, lesson_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM lesson_events WHERE lesson_id = ? ORDER BY id", (lesson_id,)).fetchall()

    # ---------- M2 ----------

    def stat(self, scope: str, key: str) -> tuple[float, float, int] | None:
        r = self.conn.execute("SELECT alpha, beta, episodes FROM template_stats WHERE scope = ? AND key = ?",
                              (scope, key)).fetchone()
        return (r["alpha"], r["beta"], r["episodes"]) if r else None

    def add_reward(self, episode_id: str, scope: str, key: str, template: str, hits: float, misses: float) -> None:
        now = utc_now()
        self.conn.execute(
            """INSERT INTO template_stats (scope, key, alpha, beta, episodes, updated_at) VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(scope, key) DO UPDATE SET alpha = alpha + excluded.alpha, beta = beta + excluded.beta,
                   episodes = episodes + 1, updated_at = excluded.updated_at""", (scope, key, hits, misses, now))
        self.conn.execute(
            """INSERT INTO reward_log (episode_id, scope, key, template, hits, misses) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(episode_id, scope, key, template) DO UPDATE SET hits = hits + excluded.hits,
                   misses = misses + excluded.misses""", (episode_id, scope, key, template, hits, misses))
        self.conn.commit()

    def rewards_of(self, episode_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reward_log WHERE episode_id = ?", (episode_id,)).fetchall()

    def reverse_rewards(self, episode_id: str, template: str, weight: float) -> list[tuple[str, str, float]]:
        """人工糾偏：把該 episode 給某範本的「命中」按比例改記為「未命中」。reward_log 同步更新，重複糾偏不會重複扣。"""
        out = []
        now = utc_now()
        for r in self.conn.execute("SELECT * FROM reward_log WHERE episode_id = ? AND template = ? AND hits > 0",
                                   (episode_id, template)).fetchall():
            amt = r["hits"] * weight
            self.conn.execute("UPDATE template_stats SET alpha = MAX(0, alpha - ?), beta = beta + ?, updated_at = ? "
                              "WHERE scope = ? AND key = ?", (amt, amt, now, r["scope"], r["key"]))
            self.conn.execute("UPDATE reward_log SET hits = hits - ?, misses = misses + ? "
                              "WHERE episode_id = ? AND scope = ? AND key = ? AND template = ?",
                              (amt, amt, episode_id, r["scope"], r["key"], template))
            out.append((r["scope"], r["key"], amt))
        self.conn.commit()
        return out

    def top_stats(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT *, alpha / (alpha + beta) AS mean FROM template_stats "
                                 "WHERE alpha + beta > 0 ORDER BY episodes DESC, scope, key LIMIT ?", (limit,)).fetchall()

    # ---------- episode ----------

    def record_outcome(self, row: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO episode_outcomes (episode_id, scenario, goal_type, success, end_reason, fetches,
                   api_calls, llm_usd, lessons_json, corrected, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["episode_id"], row.get("scenario"), row.get("goal_type"), int(row["success"]), row.get("end_reason"),
             row["fetches"], row["api_calls"], row["llm_usd"], json.dumps(row.get("lessons", [])), 0, utc_now()))
        self.conn.commit()

    def success_rate(self, without_lesson: str | None = None) -> tuple[float | None, int]:
        """episode 的成功率與樣本數；without_lesson：只看沒有套用這條經驗的 episode（淘汰經驗時的對照組）。"""
        rows = self.conn.execute("SELECT success, lessons_json FROM episode_outcomes").fetchall()
        if without_lesson is not None:
            rows = [r for r in rows if without_lesson not in json.loads(r["lessons_json"])]
        return (sum(r["success"] for r in rows) / len(rows) if rows else None), len(rows)

    def outcome(self, episode_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM episode_outcomes WHERE episode_id = ?", (episode_id,)).fetchone()

    def mark_corrected(self, episode_id: str, failed: bool) -> None:
        self.conn.execute("UPDATE episode_outcomes SET corrected = 1, success = CASE WHEN ? THEN 0 ELSE success END "
                          "WHERE episode_id = ?", (int(failed), episode_id))
        self.conn.commit()

    def add_correction(self, episode_id: str, claim_id: str, verdict: str, reason: str, lesson_id: str | None) -> None:
        self.conn.execute("INSERT INTO corrections (episode_id, claim_id, verdict, reason, lesson_id, ts) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (episode_id, claim_id, verdict, reason, lesson_id, utc_now()))
        self.conn.commit()

    def corrections(self, episode_id: str | None = None) -> list[sqlite3.Row]:
        if episode_id is None:
            return self.conn.execute("SELECT * FROM corrections ORDER BY id").fetchall()
        return self.conn.execute("SELECT * FROM corrections WHERE episode_id = ? ORDER BY id", (episode_id,)).fetchall()
