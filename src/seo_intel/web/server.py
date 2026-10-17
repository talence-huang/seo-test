"""本機網頁介面（`seo-intel web`）：Python 標準庫的 HTTP 伺服器，不需額外套件。

- 只綁定 127.0.0.1；檢查 Host 標頭（防 DNS rebinding），寫入類請求必須帶啟動時產生的令牌（防其他網站偷打本機 API）。
- 研究在背景執行緒進行，前端輪詢事件；逐步模式由網頁的按鈕控制。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from seo_intel import __version__
from seo_intel.config import PROJECT_ROOT, get_settings
from seo_intel.core import approvals
from seo_intel.core.episode import RUNS_DIR, load_checkpoint
from seo_intel.core.runner import DEFAULT_MEMORY
from seo_intel.web.jobs import JobManager, episode_summary, make_llm

SERVE_ROOTS = {"runs": RUNS_DIR}
_EPISODE = re.compile(r"^E-[\w-]+$")


def _static(name: str) -> bytes:
    return resources.files("seo_intel.web").joinpath("static", name).read_bytes()


def _episode_dirs() -> list[Path]:
    if not RUNS_DIR.is_dir():
        return []
    return [d for d in sorted(RUNS_DIR.glob("E-*"), reverse=True) if (d / "checkpoint.json").is_file()]


_STATE_CACHE: dict[Path, tuple[float, dict]] = {}


def _state_dict(run_dir: Path) -> dict:
    """只解析檢查點的 state 部分（frontier 可能有數 MB），並依修改時間快取：列表不必每次重讀所有檢查點。"""
    path = run_dir / "checkpoint.json"
    mtime = path.stat().st_mtime
    hit = _STATE_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    text = path.read_text(encoding="utf-8")
    start = text.index('"state"') + len('"state"')
    start = text.index(":", start) + 1
    while text[start].isspace():
        start += 1
    state, _ = json.JSONDecoder().raw_decode(text, start)
    _STATE_CACHE[path] = (mtime, state)
    return state


def label(key: str) -> str:
    from seo_intel.core.strategy import label as dimension_label

    return dimension_label(key)


def _label(task: dict) -> str:
    return f"{'、'.join(task['competitors'])}｜{'、'.join(task.get('keywords') or [])}"


def _legacy(st: dict) -> bool:
    """舊版的模擬情境 episode（模擬世界已移除，無法開啟或續跑）。"""
    return bool(st["task"].get("scenario")) or not st["task"].get("keywords")


def _pending(st: dict) -> dict | None:
    return next((a for a in reversed(st.get("approvals") or []) if a["status"] == "pending"), None)


def project_path(raw: str | None, default: Path) -> Path:
    """使用者輸入的路徑：相對路徑一律以專案根目錄為準（不受啟動時的目前目錄影響）。"""
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _memory_path(q: dict) -> Path:
    return project_path((q.get("path") or [""])[0], DEFAULT_MEMORY)


class App:
    def __init__(self, jobs: JobManager | None = None) -> None:
        self.jobs = jobs or JobManager()
        self.token = secrets.token_urlsafe(24)

    # ---------- 讀取 ----------

    def status(self) -> dict:
        from seo_intel.env import has_anthropic_credentials

        busy = self.jobs.busy()
        return {"version": __version__, "has_key": has_anthropic_credentials(),
                "has_serp_key": bool(os.environ.get("SERPAPI_API_KEY")), "busy": busy.info() if busy else None,
                "memory": str(DEFAULT_MEMORY), "budget": get_settings().budget.model_dump()}

    def episodes(self) -> list[dict]:
        out = []
        for d in _episode_dirs():
            st = _state_dict(d)
            if _legacy(st):
                continue
            job = self.jobs.by_episode.get(st["id"])
            pa = _pending(st)
            out.append({"id": st["id"], "label": _label(st["task"]),
                        "status": st["status"], "end_reason": st.get("end_reason"), "seq": st["seq"],
                        "fetches": st["budget"]["fetches"], "llm_usd": round(st["budget"]["llm_usd"], 4),
                        "claims": [label(c["dimension"]) if c.get("dimension") else c["template"]
                                   for c in st.get("claims") or [] if c.get("validated", True)],
                        "pending_approval": pa["id"] if pa else None, "job": job.info() if job else None})
        return out

    def pending_approvals(self) -> list[dict]:
        out = []
        for d in _episode_dirs():
            st = _state_dict(d)
            if not _legacy(st) and (a := _pending(st)) is not None:
                out.append({"episode": st["id"], "label": _label(st["task"]), **a})
        return out

    def memory(self, path: Path) -> dict:
        from seo_intel.memory.priors import LABEL
        from seo_intel.memory.store import MemoryStore

        if not path.is_file():
            return {"path": str(path), "exists": False, "lessons": [], "outcomes": [], "stats": [], "corrections": []}
        store = MemoryStore(path)
        try:
            lessons = [{**x.model_dump(mode="json"), "success_rate": x.success_rate,
                        "biases_text": [b.describe() for b in x.biases]} for x in store.lessons()]
            outcomes = [dict(r) for r in store.conn.execute("SELECT * FROM episode_outcomes ORDER BY ts")]
            stats = [{**dict(r), "label": LABEL.get(r["scope"], r["scope"])} for r in store.top_stats(30)]
            corrections = [dict(r) for r in store.corrections()]
        finally:
            store.close()
        return {"path": str(path), "exists": True, "lessons": lessons, "outcomes": outcomes, "stats": stats,
                "corrections": corrections}

    def lesson(self, path: Path, lesson_id: str) -> dict:
        from seo_intel.memory.store import MemoryStore

        store = MemoryStore(path)
        try:
            x = store.lesson(lesson_id)
            if x is None:
                raise KeyError(lesson_id)
            return {**x.model_dump(mode="json"), "events": [dict(e) for e in store.events(lesson_id)]}
        finally:
            store.close()

    # ---------- 寫入 ----------

    def feedback(self, p: dict) -> dict:
        from seo_intel.memory.correct import correct
        from seo_intel.memory.store import MemoryStore
        from seo_intel.store import Database
        from seo_intel.store.db import utc_now

        eid, claim, verdict, reason = p["episode"], p["claim"], p["verdict"], (p.get("reason") or "").strip()
        if verdict not in ("correct", "wrong", "partial") or not reason:
            raise ValueError("需要判定（correct / wrong / partial）與理由")
        run_dir = RUNS_DIR / eid
        state, _ = load_checkpoint(run_dir)
        client = None if verdict == "correct" or p.get("no_llm") else make_llm(get_settings(), run_dir)
        store = MemoryStore(project_path(p.get("memory"), DEFAULT_MEMORY))
        try:
            res = correct(store, state, claim, verdict, reason, client)
        finally:
            store.close()
        db = Database(self.jobs.db_path)
        try:
            db.insert("feedback", {"episode_id": eid, "claim_id": f"{eid}/{claim}", "verdict": verdict,
                                   "reason": reason, "created_at": utc_now()})
        except sqlite3.IntegrityError:
            pass
        finally:
            db.close()
        return {"lines": res.lines, "lesson": res.lesson.model_dump(mode="json") if res.lesson else None}

    def retire(self, p: dict) -> dict:
        from seo_intel.memory.store import MemoryStore

        store = MemoryStore(project_path(p.get("path"), DEFAULT_MEMORY))
        try:
            x = store.lesson(p["lesson"])
            if x is None:
                raise KeyError(p["lesson"])
            x.status, x.retired_reason = "retired", f"人工淘汰：{p.get('reason') or '（未填理由）'}"
            store.save_lesson(x)
            store.event(x.id, None, "retired", x.retired_reason)
        finally:
            store.close()
        return {"ok": True}


def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = f"seo-intel/{__version__}"

        def log_message(self, fmt, *args) -> None:   # 安靜：主控台留給使用者
            pass

        # ---------- 回應 ----------

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, code: int, message: str) -> None:
            self._json({"error": message}, code)

        def _host_ok(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._error(403, "Host 不符")
                return False
            return True

        # ---------- GET ----------

        def do_GET(self) -> None:   # noqa: N802
            if not self._host_ok():
                return
            u = urlsplit(self.path)
            path, q = unquote(u.path), parse_qs(u.query)
            try:
                if path == "/":
                    html = _static("index.html").decode("utf-8").replace("__TOKEN__", app.token)
                    return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    name = path.removeprefix("/static/")
                    if "/" in name or name.startswith("."):
                        return self._error(404, "not found")
                    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    return self._send(200, _static(name), f"{ctype}; charset=utf-8")
                if path.startswith("/files/"):
                    return self._file(path.removeprefix("/files/"))
                if path == "/api/status":
                    return self._json(app.status())
                if path == "/api/episodes":
                    return self._json(app.episodes())
                if m := re.fullmatch(r"/api/episodes/([\w-]+)", path):
                    if not _EPISODE.match(m[1]) or not (RUNS_DIR / m[1] / "checkpoint.json").is_file():
                        return self._error(404, "找不到 episode")
                    return self._json(episode_summary(RUNS_DIR / m[1]))
                if m := re.fullmatch(r"/api/jobs/(J\d+)", path):
                    job = app.jobs.jobs.get(m[1])
                    if job is None:
                        return self._error(404, "找不到工作")
                    since = int((q.get("since") or ["0"])[0])
                    return self._json({"job": job.info(), "events": job.since(since)})
                if path == "/api/approvals":
                    return self._json(app.pending_approvals())
                if path == "/api/memory":
                    return self._json(app.memory(_memory_path(q)))
                if m := re.fullmatch(r"/api/memory/lesson/(L-\d+)", path):
                    return self._json(app.lesson(_memory_path(q), m[1]))
                return self._error(404, "not found")
            except KeyError as e:
                return self._error(404, f"找不到：{e}")
            except Exception as e:   # noqa: BLE001
                return self._error(500, f"{type(e).__name__}: {e}")

        def _file(self, rel: str) -> None:
            """只提供 runs/ 底下的檔案（時間軸、報告），防止路徑穿越。"""
            root_key, _, rest = rel.partition("/")
            root = SERVE_ROOTS.get(root_key)
            if root is None:
                return self._error(404, "not found")
            target = (root / rest).resolve()
            if not target.is_file() or not target.is_relative_to(root.resolve()):
                return self._error(404, "not found")
            if target.suffix not in (".html", ".md", ".json", ".jsonl"):
                return self._error(404, "not found")
            ctype = {".html": "text/html", ".md": "text/plain", ".json": "application/json",
                     ".jsonl": "text/plain"}[target.suffix]
            self._send(200, target.read_bytes(), f"{ctype}; charset=utf-8")

        # ---------- POST ----------

        def do_POST(self) -> None:   # noqa: N802
            if not self._host_ok():
                return
            if not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                return self._error(403, "令牌不符：請從 seo-intel web 開啟的頁面操作")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                p = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._error(400, "JSON 格式錯誤")
            path = urlsplit(self.path).path
            try:
                if path == "/api/research":
                    return self._json(app.jobs.start_research(p).info())
                if m := re.fullmatch(r"/api/jobs/(J\d+)/control", path):
                    return self._json(app.jobs.control(m[1], p.get("action", "")).info())
                if m := re.fullmatch(r"/api/episodes/(E-[\w-]+)/resume", path):
                    return self._json(app.jobs.resume(m[1], bool(p.get("step"))).info())
                if m := re.fullmatch(r"/api/episodes/(E-[\w-]+)/decide", path):
                    return self._json(app.jobs.decide(m[1], bool(p.get("approve")), p.get("note") or None))
                if path == "/api/feedback":
                    return self._json(app.feedback(p))
                if path == "/api/memory/retire":
                    return self._json(app.retire(p))
                return self._error(404, "not found")
            except approvals.ApprovalPending as e:
                return self._error(409, str(e))
            except (RuntimeError, ValueError) as e:
                return self._error(409, str(e))
            except KeyError as e:
                return self._error(404, f"找不到：{e}")
            except FileNotFoundError as e:
                return self._error(404, str(e))
            except Exception as e:   # noqa: BLE001
                return self._error(500, f"{type(e).__name__}: {e}")

    return Handler


def serve(port: int = 8765, app: App | None = None) -> tuple[ThreadingHTTPServer, App]:
    """port=0 時由系統挑選可用埠（測試用）；Host 檢查使用實際的埠。"""
    app = app or App()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    httpd.RequestHandlerClass = make_handler(app, httpd.server_address[1])
    return httpd, app
