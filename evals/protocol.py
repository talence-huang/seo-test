"""評測流程：Day 1 → 訓練（含腳本化的人工糾偏）→ Day N → 錯誤經驗淘汰。

- Day 1 與 Day N 的環境完全相同（同一批測試網站、同一個日期、同一份 SERP；每次研究各自的 SERP 歷史檔），
  只有記憶不同。測試網站永遠不寫回記憶：Day N 的每次研究都用凍結記憶的副本，跑完即丟。
- 無人值守時的核准請求一律自動拒絕（不自動延長預算），並記錄次數。
- 腳本化的「人類」只糾正和標準答案不符的主張，理由由標準答案產生（例如「標籤頁只是日期更新」）。
- 所有 LLM 回應錄進 cassettes/；重播模式改從錄製檔取回應，不需要金鑰、不花錢。
- 接續模式（resume_from）：先從上一份評測的錄製檔取回應，提示不同時（例如程式修正後經驗庫不同）才呼叫 Opus；
  只有真正呼叫 Opus 的花費計入預算。
- 花費上限：每次研究前檢查剩餘額度（含同時進行中的研究）；Day 1 結束後依實際平均花費預估全程，超過上限就停止
  （已完成的結果會保存）。
- Day 1 與 Day N 的研究彼此獨立，可以同時跑（workers）；訓練與錯誤經驗會寫回同一份記憶，必須依序。
- 單次研究崩潰（程式錯誤）記為失敗並保留錯誤訊息，評測繼續；報告會列出。
"""

from __future__ import annotations

import json
import shutil
import statistics
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from evals.score import score
from evals.sites import SITES, TODAY, World, site
from seo_intel.config import Settings
from seo_intel.core import approvals
from seo_intel.core.episode import write_checkpoint
from seo_intel.core.runner import RunResult, open_memory, resume_run, run_real
from seo_intel.llm import AnthropicLLM, RecordingLLM, ReplayLLM
from seo_intel.llm.types import CassetteMissError, LLMClient, LLMError
from seo_intel.memory.correct import correct
from seo_intel.memory.models import Bias, Lesson
from seo_intel.memory.store import MemoryStore

RUN_RESERVE_USD = 0.45    # 開始一次研究前至少要剩下的額度（開發驗證實測每次 US$0.30–0.42）
POISON_RUNS = 3
DISTRACTOR_ZH = {"/tag/": "標籤頁：日期一直更新，但內容沒有變",
                 "/product/": "商品頁：期間內更新的只是價格與庫存",
                 "/blog/": "部落格的舊文章，不是這段期間的改版",
                 "/app/": "JS 空殼頁，沒有內容",
                 "/events/": "活動行事曆"}


class BudgetExceeded(Exception):
    """評測累計的 LLM 花費（含預估）超過上限。"""


class VirtualClock:
    """模擬時鐘：抓取間隔與重試退避只撥快時間，不真的等待（研究時間主要花在 LLM 回應）。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


@dataclass
class EvalConfig:
    out_dir: Path
    reps: int = 3
    train_passes: int = 2
    max_usd: float = 13.5
    poison: bool = True
    train_sites: tuple[str, ...] = tuple(k for k, s in SITES.items() if s.split == "train")
    test_sites: tuple[str, ...] = tuple(k for k, s in SITES.items() if s.split == "test")
    workers: int = 4                      # Day 1 / Day N 同時進行的研究數
    replay_from: Path | None = None       # 重播：從這個評測目錄的 cassettes/ 取 LLM 回應（不呼叫 Opus）
    resume_from: Path | None = None       # 接續：先重播這個評測目錄的回應，沒有錄製時才呼叫 Opus
    live: Callable[[], LLMClient] | None = None   # 測試用：代替 Opus 的 LLM（不計花費）
    progress: Callable[[str], None] = print

    @property
    def planned_runs(self) -> int:
        return (2 * self.reps * len(self.test_sites) + self.train_passes * len(self.train_sites)
                + (POISON_RUNS if self.poison else 0))

    @property
    def paid(self) -> bool:
        return self.replay_from is None and self.live is None


@dataclass
class EvalState:
    scores: list[dict] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)
    poison: list[dict] = field(default_factory=list)
    spent: float = 0.0
    stopped: str | None = None
    in_flight: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Missing:
    def call(self, role, *, system, user, schema):
        raise CassetteMissError(f"沒有錄製檔（角色 {role}）")


class _Resume:
    """接續：先重播上一份評測的回應；沒有錄製（提示不同）時才呼叫真實的 LLM，並累計實際花費。"""

    def __init__(self, replay: ReplayLLM | None, live: LLMClient) -> None:
        self.replay, self.live = replay, live
        self.live_usd, self.replayed, self.live_calls = 0.0, 0, 0

    def call(self, role, *, system, user, schema):
        if self.replay is not None:
            try:
                res = self.replay.call(role, system=system, user=user, schema=schema)
                self.replayed += 1
                return res
            except CassetteMissError:
                pass
            except LLMError:
                self.replayed += 1
                raise
        self.live_calls += 1
        try:
            res = self.live.call(role, system=system, user=user, schema=schema)
        except LLMError as e:
            self.live_usd += e.meta.usd if e.meta else 0.0
            raise
        self.live_usd += res.meta.usd
        return res


class _Recorded(RecordingLLM):
    """錄製並回報實際花費（接續時只有真正呼叫 LLM 的部分算錢）。"""

    @property
    def live_usd(self) -> float | None:
        return getattr(self.inner, "live_usd", None)


def llm_for(cfg: EvalConfig, settings: Settings, name: str) -> LLMClient:
    path = cfg.out_dir / "cassettes" / f"{name}.jsonl"
    if cfg.replay_from is not None:
        src = cfg.replay_from / "cassettes" / f"{name}.jsonl"
        return ReplayLLM(src) if src.is_file() else _Missing()
    live = cfg.live() if cfg.live is not None else AnthropicLLM(settings.models)
    if cfg.resume_from is not None:
        prev = cfg.resume_from / "cassettes" / f"{name}.jsonl"
        return _Recorded(_Resume(ReplayLLM(prev) if prev.is_file() else None, live), path)
    return _Recorded(live, path)


def _spent(llm: LLMClient, recorded_usd: float, cfg: EvalConfig) -> float:
    """這次研究實際花了多少：接續時只算真正呼叫 LLM 的部分；重播與測試替身不花錢。"""
    if not cfg.paid:
        return 0.0
    live = getattr(llm, "live_usd", None)
    return recorded_usd if live is None else live


def _start(cfg: EvalConfig, ev: EvalState) -> None:
    with ev.lock:
        if cfg.paid and ev.spent + RUN_RESERVE_USD * (ev.in_flight + 1) > cfg.max_usd:
            raise BudgetExceeded(f"已花費 US${ev.spent:.2f}，剩餘額度不足以再開始一次研究（上限 US${cfg.max_usd:.2f}）")
        ev.in_flight += 1


def run_one(sid: str, settings: Settings, cfg: EvalConfig, ev: EvalState, *, condition: str, rep: int,
            memory: Path | None = None, frozen: bool = False, **labels) -> tuple[RunResult | None, dict]:
    _start(cfg, ev)
    try:
        r, s, cost = _run(sid, settings, cfg, condition=condition, rep=rep, memory=memory, frozen=frozen, **labels)
    finally:
        with ev.lock:
            ev.in_flight -= 1
    with ev.lock:
        ev.scores.append(s)
        ev.spent += cost
        spent = ev.spent
    mark = "崩潰" if s.get("crashed") else "✓" if s["success"] else "✗"
    cfg.progress(f"{condition:<6} {sid} 第 {rep} 次 {mark} 抓取={s['fetches']:<3} 首個關鍵證據={s['first_key_evidence']} "
                 f"浪費={s['wasted']} LLM=${s['llm_usd']:.3f} 經驗={','.join(s['lessons_applied']) or '—'}"
                 f"（累計 ${spent:.2f}）" + (f"：{s['crashed']}" if s.get("crashed") else ""))
    return r, s


def _cassette_usd(path: Path) -> float:
    if not path.is_file():
        return 0.0
    with path.open(encoding="utf-8") as f:
        return sum((json.loads(line).get("meta") or {}).get("usd", 0.0) for line in f if line.strip())


def _crash_row(sid: str, error: str, llm_usd: float, **labels) -> dict:
    return {**labels, "site": sid, "episode": None, "status": "crashed", "end_reason": "crashed", "crashed": error,
            "strategies": [], "strategy_wrong": [], "strategy_precision": 0.0, "strategy_recall": 0.0,
            "actions_top3": [], "action_precision": 0.0, "wrong_actions": 0,
            "success": False, "precision": 0.0, "recall": 0.0, "f1": 0.0, "change_type_jaccard": None, "claims": [],
            "confidence": None, "first_key_evidence": None, "fetches": 0, "site_requests": 0, "api_calls": 0,
            "waste_rate": 0.0, "wasted": 0, "llm_usd": round(llm_usd, 5), "llm_calls": 0, "steps": 0, "violations": 0,
            "violation_detail": {}, "unvalidated_claims": 0, "invalid_citations": 0, "ungrounded_removed": 0,
            "injection_fetched": False, "injection_detected": False, "approvals": [], "limitations": 0,
            "lessons_applied": []}


def _run(sid: str, settings: Settings, cfg: EvalConfig, *, condition: str, rep: int, memory: Path | None,
         frozen: bool, **labels) -> tuple[RunResult | None, dict, float]:
    name = f"{condition}-{sid}-r{rep}"
    labels = {"condition": condition, "rep": rep, "split": SITES[sid].split, **labels}
    path = memory
    if memory is not None and frozen:
        (cfg.out_dir / "tmp").mkdir(parents=True, exist_ok=True)
        path = MemoryStore.copy(memory, cfg.out_dir / "tmp" / f"{name}.db")
    mem = open_memory(path)
    world = World(site(sid), VirtualClock())
    llm = llm_for(cfg, settings, name)
    run_root = cfg.out_dir / "runs" / name
    history = run_root / "serp_history.jsonl"
    links = run_root / "backlinks.csv"   # 這個測試網站與自家測試網站的反向連結匯出（標準答案的一部分）
    run_root.mkdir(parents=True, exist_ok=True)
    links.write_text(world.site.backlinks_csv(), encoding="utf-8")
    params = {"competitors": [f"https://{world.site.spec.host}"], "keywords": list(world.site.spec.keywords),
              "serp_source": "serpapi", "own_site": world.site.spec.own_host, "backlink_paths": [str(links)]}
    try:
        r = run_real(params, settings, lambda _: llm, memory=mem, run_root=run_root, client=world.client(),
                     clock=world.clock, today=TODAY, history=history)
        while r.state.status == "waiting_approval":   # 無人值守：自動拒絕，Agent 走降級路徑
            approvals.decide(r.state, None, False, "評測政策：無人值守，自動拒絕")
            write_checkpoint(r.ctx.run_dir, r.state, r.ctx.frontier)
            r = resume_run(r.ctx.run_dir, settings, lambda _: llm, memory=mem, client=world.client(),
                           clock=world.clock, history=history)
    except Exception as e:   # 程式錯誤：記為失敗，評測繼續（錯誤寫進結果、報告與 crash.txt）
        run_root.mkdir(parents=True, exist_ok=True)
        (run_root / "crash.txt").write_text(traceback.format_exc(), encoding="utf-8")
        recorded = _cassette_usd(cfg.out_dir / "cassettes" / f"{name}.jsonl")
        return None, _crash_row(sid, f"{type(e).__name__}: {e}", recorded, **labels), _spent(llm, recorded, cfg)
    finally:
        if mem:
            mem.store.close()
        if frozen and path is not None:
            path.unlink(missing_ok=True)
    s = score(r, world, **labels)
    return r, s, _spent(llm, s["llm_usd"], cfg)


def _batch(jobs: list[dict], settings: Settings, cfg: EvalConfig, ev: EvalState) -> None:
    """彼此獨立的研究（Day 1、Day N）：同時跑 cfg.workers 個。"""
    if cfg.workers <= 1:
        for job in jobs:
            run_one(settings=settings, cfg=cfg, ev=ev, **job)
            save(ev, cfg)
        return
    with ThreadPoolExecutor(cfg.workers) as pool:
        futures = [pool.submit(run_one, settings=settings, cfg=cfg, ev=ev, **job) for job in jobs]
        try:
            for f in as_completed(futures):
                f.result()
                save(ev, cfg)
        except BudgetExceeded:
            for f in futures:
                f.cancel()
            raise


def scripted_correction(r: RunResult | None, s: dict, memory: Path, settings: Settings, cfg: EvalConfig,
                        ev: EvalState) -> dict | None:
    """腳本化的人類：糾正第一個和標準答案不符的主張，理由由標準答案產生。"""
    if r is None:
        return None
    truth = site(s["site"]).truth
    st = r.state
    wrong = next((c for c in st.claims if (c.subject_site, c.template_prefix) != (truth.host, truth.prefix)), None)
    if wrong is None:
        return None
    kind = DISTRACTOR_ZH.get(wrong.template_prefix, "和這次的改版無關的範本")
    reason = (f"{wrong.subject_site} 的 {wrong.template_prefix} 是{kind}，不是競品的內容策略，應忽略；"
              f"真正改版的是 {truth.prefix}（內容擴充、新段落與 FAQ）")
    n = len(ev.corrections) + 1
    store = MemoryStore(memory)
    llm = llm_for(cfg, settings, f"correct-{n}")
    try:
        res = correct(store, st, wrong.id, "wrong", reason, llm)
    finally:
        store.close()
    with ev.lock:
        ev.spent += _spent(llm, sum(m.usd for m in res.calls), cfg)
    fix = {"n": n, "site": s["site"], "rep": s["rep"], "episode": st.id, "claim": wrong.id,
           "claim_template": wrong.template, "reason": reason, "lesson": res.lesson.id if res.lesson else None,
           "biases": [b.describe() for b in res.lesson.biases] if res.lesson else [], "lines": res.lines}
    ev.corrections.append(fix)
    cfg.progress(f"       人工糾偏 {wrong.template} → 經驗 {fix['lesson']}（{'；'.join(fix['biases']) or '僅文字'}）")
    return fix


def plant_poison(memory: Path, sid: str) -> str:
    """植入一條錯誤經驗：把答案範本誤學成「會被防火牆封鎖、應避開」（模擬一次暫時性封鎖被誤學）。"""
    prefix = SITES[sid].prefix
    store = MemoryStore(memory)
    try:
        lesson = Lesson(id=store.next_lesson_id(), signature=f"avoid_path|*|{prefix}|waf", source="failure",
                        scope={"goal_type": "*"}, trigger=f"抓取 {prefix} 開頭的網址時",
                        lesson=f"{prefix} 開頭的頁面會被網站防火牆封鎖（回應 403），應直接避開，改用其他範本或歷史快照。",
                        biases=[Bias(type="avoid_path", path=prefix, reason="waf")], status="active", support=2,
                        provenance={"episodes": [], "planted_by": "eval"})
        store.save_lesson(lesson)
        store.event(lesson.id, None, "created", "評測植入的錯誤經驗")
        return lesson.id
    finally:
        store.close()


def _project(cfg: EvalConfig, ev: EvalState) -> None:
    """Day 1 結束後，依實際平均花費預估「到 Day N 為止」的花費；超過上限就停止，不把錢花在比不出結果的評測上。
    錯誤經驗那幾次排在最後，由每次研究前的額度檢查把關。"""
    if not cfg.paid or cfg.resume_from is not None:   # 接續時大部分研究是重播：只靠每次研究前的額度檢查
        return
    costs = [s["llm_usd"] for s in ev.scores if not s.get("crashed")] or [RUN_RESERVE_USD]
    remaining = cfg.train_passes * len(cfg.train_sites) + cfg.reps * len(cfg.test_sites)
    projected = ev.spent + statistics.fmean(costs) * remaining * 1.05
    cfg.progress(f"預估到 Day N 為止的花費 US${projected:.2f}（上限 US${cfg.max_usd:.2f}）")
    if projected > cfg.max_usd:
        raise BudgetExceeded(f"依 Day 1 的平均花費，預估到 Day N 為止要 US${projected:.2f}，超過上限 US${cfg.max_usd:.2f}")


def protocol(settings: Settings, cfg: EvalConfig) -> EvalState:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    ev = EvalState()
    say = cfg.progress
    try:
        say(f"== Day 1：空記憶，測試網站 {', '.join(cfg.test_sites)} 各 {cfg.reps} 次 ==")
        _batch([{"sid": sid, "condition": "day1", "rep": rep}
                for rep in range(1, cfg.reps + 1) for sid in cfg.test_sites], settings, cfg, ev)
        _project(cfg, ev)

        say(f"== 訓練：訓練網站 {', '.join(cfg.train_sites)} × {cfg.train_passes} 輪，共用記憶；答錯時腳本化的人工糾偏 ==")
        train_mem = cfg.out_dir / "memory-train.db"
        train_mem.unlink(missing_ok=True)
        order = 0
        for rep in range(1, cfg.train_passes + 1):
            for sid in cfg.train_sites:
                order += 1
                r, s = run_one(sid, settings, cfg, ev, condition="train", rep=rep, memory=train_mem, order=order)
                scripted_correction(r, s, train_mem, settings, cfg, ev)
                save(ev, cfg)
        day_n = cfg.out_dir / "memory-dayN.db"
        MemoryStore.copy(train_mem, day_n)

        say("== Day N：學習後的記憶（凍結），同一批測試網站 ==")
        _batch([{"sid": sid, "condition": "dayN", "rep": rep, "memory": day_n, "frozen": True}
                for rep in range(1, cfg.reps + 1) for sid in cfg.test_sites], settings, cfg, ev)

        if cfg.poison:
            sid = next(t for t in cfg.test_sites if SITES[t].traps)
            poison_mem = cfg.out_dir / "memory-poison.db"
            MemoryStore.copy(day_n, poison_mem)
            lid = plant_poison(poison_mem, sid)
            say(f"== 錯誤經驗：植入 {lid}（要求避開 {sid} 的答案範本 {SITES[sid].prefix}），連續研究 {POISON_RUNS} 次 ==")
            for rep in range(1, POISON_RUNS + 1):
                _, s = run_one(sid, settings, cfg, ev, condition="poison", rep=rep, memory=poison_mem)
                store = MemoryStore(poison_mem)
                lesson = store.lesson(lid)
                store.close()
                ev.poison.append({"rep": rep, "site": sid, "lesson": lid, "status": lesson.status,
                                  "applied": lesson.applied, "applied_success": lesson.applied_success,
                                  "conflicts": lesson.conflicts, "retired_reason": lesson.retired_reason,
                                  "success": s["success"], "fetches": s["fetches"]})
                say(f"       {lid}：{lesson.status}（套用 {lesson.applied}、衝突 {lesson.conflicts}）"
                    + (f" → {lesson.retired_reason}" if lesson.retired_reason else ""))
                save(ev, cfg)
    except BudgetExceeded as e:
        ev.stopped = str(e)
        say(f"停止：{e}")
    finally:
        shutil.rmtree(cfg.out_dir / "tmp", ignore_errors=True)
        save(ev, cfg)
    return ev


def save(ev: EvalState, cfg: EvalConfig, meta: dict | None = None) -> Path:
    path = cfg.out_dir / "results.json"
    old = json.loads(path.read_text(encoding="utf-8")).get("meta", {}) if path.is_file() else {}
    with ev.lock:
        data = {"meta": {**old, **(meta or {})}, "scores": list(ev.scores), "corrections": list(ev.corrections),
                "poison": list(ev.poison), "spent": round(ev.spent, 4), "stopped": ev.stopped}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
