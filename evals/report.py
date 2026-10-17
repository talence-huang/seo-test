"""把評測結果整理成 Markdown 報告：結論、Day 1 → Day N 的配對差異（bootstrap 95% 信賴區間）、
對照網站、訓練過程與人工糾偏、學到的記憶、錯誤經驗淘汰、安全與可靠性。"""

from __future__ import annotations

import random
import statistics
from pathlib import Path

from evals.sites import SITES, site
from seo_intel.memory.store import MemoryStore

METRICS = (("fetches", "抓取次數", 1), ("first_key_evidence", "首個關鍵證據抓取數", 1), ("wasted", "浪費的請求", 1),
           ("llm_usd", "LLM US$", 3), ("steps", "步驟數", 0), ("success", "成功", 2))


def bootstrap(values: list[float], n: int = 4000, seed: int = 0) -> tuple[float, float, float]:
    """平均值與 95% bootstrap 信賴區間（樣本很小時區間會很寬，照實呈現）。"""
    if not values:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(n))
    return statistics.fmean(values), means[int(0.025 * n)], means[int(0.975 * n) - 1]


def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else float("nan")


def _fmt(x: float | None, d: int = 2) -> str:
    return "—" if x is None or x != x else f"{x:.{d}f}"


def summarize(rows: list[dict]) -> dict:
    fke = [r["first_key_evidence"] for r in rows if r["first_key_evidence"] is not None]
    return {"n": len(rows), "success": _mean([float(r["success"]) for r in rows]), "f1": _mean([r["f1"] for r in rows]),
            "fetches": _mean([r["fetches"] for r in rows]), "fke": statistics.median(fke) if fke else float("nan"),
            "fke_missing": len(rows) - len(fke), "wasted": _mean([r["wasted"] for r in rows]),
            "waste_rate": _mean([r["waste_rate"] for r in rows]), "llm_usd": _mean([r["llm_usd"] for r in rows]),
            "steps": _mean([r["steps"] for r in rows]), "violations": sum(r["violations"] for r in rows),
            "approvals": sum(len(r["approvals"]) for r in rows)}


def pairs(scores: list[dict], a: str, b: str, sites: set[str]) -> list[tuple[dict, dict]]:
    idx = {(r["site"], r["rep"]): r for r in scores if r["condition"] == a and r["site"] in sites}
    return [(idx[(r["site"], r["rep"])], r) for r in scores
            if r["condition"] == b and (r["site"], r["rep"]) in idx]


def diff(ps: list[tuple[dict, dict]], key: str) -> tuple[list[tuple[float, float]], tuple[float, float, float]]:
    vals = [(float(a[key]), float(b[key])) for a, b in ps if a[key] is not None and b[key] is not None]
    return vals, bootstrap([y - x for x, y in vals])


def verdict(m: float, lo: float, hi: float, lower_is_better: bool = True) -> str:
    if lo != lo:
        return "沒有資料"
    if hi < 0:
        return "Day N 顯著較少" if lower_is_better else "Day N 顯著較低"
    if lo > 0:
        return "Day N 顯著較多" if lower_is_better else "Day N 顯著較高"
    return "差異不顯著（信賴區間跨過 0）"


def _diff_table(ps: list[tuple[dict, dict]]) -> list[str]:
    lines = ["| 指標 | Day 1 平均 | Day N 平均 | 配對差異（95% CI） | 相對變化 | 判定 |", "|---|---|---|---|---|---|"]
    for key, label, d in METRICS:
        vals, (m, lo, hi) = diff(ps, key)
        if not vals:
            continue
        base = _mean([x for x, _ in vals])
        rel = f"{m / base:+.0%}" if base else "—"
        lines.append(f"| {label} | {_fmt(base, d)} | {_fmt(_mean([y for _, y in vals]), d)} | "
                     f"{_fmt(m, d)}（{_fmt(lo, d)} ～ {_fmt(hi, d)}） | {rel} | {verdict(m, lo, hi, key != 'success')} |")
    return lines


def render(results: dict, out_dir: Path) -> str:
    meta, scores = results["meta"], results["scores"]
    test = [s for s in meta.get("test_sites", []) if s in SITES]
    similar = {s for s in test if SITES[s].traps}
    control = {s for s in test if not SITES[s].traps}
    by: dict[str, list[dict]] = {}
    for r in scores:
        by.setdefault(r["condition"], []).append(r)
    lines = [f"# 評測報告：{meta.get('name', out_dir.name)}", "",
             f"- LLM：`{meta.get('model', '?')}`{'（重播錄製的回應）' if meta.get('replay') else ''}；"
             f"測試網站每站 {meta.get('reps')} 次；訓練 {meta.get('train_passes')} 輪；核准請求（無人值守）一律拒絕",
             f"- 研究次數：{len(scores)}；人工糾偏：{len(results['corrections'])} 次；"
             f"研究的 LLM 花費合計 US${sum(r['llm_usd'] for r in scores):.2f}"
             + (f"（接續 `{meta['resume_from']}`：有錄製的回應直接重播，本次實際新花費 US${results.get('spent', 0):.2f}）"
                if meta.get("resume_from") else f"（上限 US${meta.get('max_usd', 0):.2f}）")]
    if results.get("stopped"):
        lines.append(f"- ⚠ 評測提前停止：{results['stopped']}")
    lines.append("")

    ps_sim = pairs(scores, "day1", "dayN", similar)
    ps_ctl = pairs(scores, "day1", "dayN", control)
    lines += ["## 0. 結論", ""]
    if ps_sim:
        for key, label in (("fetches", "抓取次數"), ("first_key_evidence", "首個關鍵證據抓取數"), ("wasted", "浪費的請求")):
            vals, (m, lo, hi) = diff(ps_sim, key)
            if vals:
                base = _mean([x for x, _ in vals])
                lines.append(f"- 相似網站的{label}：Day 1 平均 {_fmt(base, 1)} → Day N {_fmt(_mean([y for _, y in vals]), 1)}"
                             f"（配對差異 {_fmt(m, 1)}，95% CI {_fmt(lo, 1)} ～ {_fmt(hi, 1)}）：{verdict(m, lo, hi)}")
        d1, dn = summarize([a for a, _ in ps_sim]), summarize([b for _, b in ps_sim])
        lines.append(f"- 相似網站的正確率：Day 1 {d1['success']:.0%} → Day N {dn['success']:.0%}（n = {len(ps_sim)} 組）")
    if ps_ctl:
        d1, dn = summarize([a for a, _ in ps_ctl]), summarize([b for _, b in ps_ctl])
        lines.append(f"- 對照網站（沒有共通的失效特徵）：正確率 {d1['success']:.0%} → {dn['success']:.0%}，"
                     f"抓取 {_fmt(d1['fetches'], 1)} → {_fmt(dn['fetches'], 1)}")
    if results.get("poison"):
        retired = next((p for p in results["poison"] if p["status"] == "retired"), None)
        lines.append(f"- 植入的錯誤經驗：" + (f"第 {retired['rep']} 次研究後被淘汰（{retired['retired_reason']}）"
                                          if retired else f"{len(results['poison'])} 次研究後仍未淘汰"))
    lines.append(f"- 安全違規（請求紀錄判定）：全部 {len(scores)} 次研究共 {sum(r['violations'] for r in scores)} 次")
    lines += ["", "樣本小、真實 LLM 每次結果不同：信賴區間跨過 0 時視為「沒有證明」，不視為「有改善」。", ""]

    lines += ["## 1. 測試網站", "", "| 網站 | 用途 | 頁數 | 答案範本 | 特徵 |", "|---|---|---|---|---|"]
    for sid in [*meta.get("train_sites", []), *test]:
        s = site(sid)
        role = "訓練" if s.spec.split == "train" else ("測試（對照）" if not s.spec.traps else "測試")
        lines.append(f"| {sid} `{s.spec.host}` | {role} | {len(s.pages):,} | `{s.spec.prefix}`（改版或新增 "
                     f"{len(s.truth.key_urls)} 頁） | {s.spec.note} |")

    lines += ["", "## 2. 測試網站彙總", "",
              "| 條件 | 網站 | n | 成功率 | F1 | 平均抓取 | 首個關鍵證據（中位數） | 浪費的請求 | LLM US$ | 步驟 | 核准請求 | 違規 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cond, label in (("day1", "Day 1（無記憶）"), ("dayN", "Day N（學習後）")):
        for group, ids in (("相似", similar), ("對照", control)):
            rows = [r for r in by.get(cond, []) if r["site"] in ids]
            if not rows:
                continue
            s = summarize(rows)
            miss = f"（{s['fke_missing']} 次沒抓到）" if s["fke_missing"] else ""
            lines.append(f"| {label} | {group} | {s['n']} | {s['success']:.0%} | {_fmt(s['f1'])} | {_fmt(s['fetches'], 1)} | "
                         f"{_fmt(s['fke'], 1)}{miss} | {_fmt(s['wasted'], 1)} | {_fmt(s['llm_usd'], 3)} | "
                         f"{_fmt(s['steps'], 0)} | {s['approvals']} | {s['violations']} |")

    if ps_sim or ps_ctl:
        lines += ["", "## 3. Day 1 → Day N 配對比較（同一網站、同一次序）", "",
                  "| 網站 | 次 | 成功 | 抓取 | 首個關鍵證據 | 浪費 | LLM US$ | 結論範本 | Day N 套用的經驗 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        ok = lambda r: "✓" if r["success"] else "✗"  # noqa: E731
        for a, b in sorted(ps_sim + ps_ctl, key=lambda p: (p[0]["site"], p[0]["rep"])):
            lines.append(f"| {a['site']} | {a['rep']} | {ok(a)} → {ok(b)} | {a['fetches']} → {b['fetches']} | "
                         f"{a['first_key_evidence']} → {b['first_key_evidence']} | {a['wasted']} → {b['wasted']} | "
                         f"{a['llm_usd']:.3f} → {b['llm_usd']:.3f} | {', '.join(a['claims']) or '—'} → "
                         f"{', '.join(b['claims']) or '—'} | {', '.join(b['lessons_applied']) or '—'} |")
        if ps_sim:
            lines += ["", f"相似網站（{', '.join(sorted(similar))}，{len(ps_sim)} 組）：", ""] + _diff_table(ps_sim)
        if ps_ctl:
            lines += ["", f"對照網站（{', '.join(sorted(control))}，{len(ps_ctl)} 組）：", ""] + _diff_table(ps_ctl)

    train = sorted(by.get("train", []), key=lambda r: r["order"])
    if train:
        lines += ["", "## 4. 訓練過程（共用記憶）", "", "| # | 網站 | 輪 | 成功 | 抓取 | 結論範本 | 套用經驗 |", "|---|---|---|---|---|---|---|"]
        for r in train:
            lines.append(f"| {r['order']} | {r['site']} | {r['rep']} | {'✓' if r['success'] else '✗'} | {r['fetches']} | "
                         f"{', '.join(r['claims']) or '—'} | {', '.join(r['lessons_applied']) or '—'} |")
        fixes = results["corrections"]
        lines += ["", f"腳本化的人工糾偏：{len(fixes)} 次"]
        lines += [f"- {f['site']}（第 {f['rep']} 輪）把 `{f['claim_template']}` 當成策略 → 「{f['reason']}」→ 經驗 {f['lesson']}"
                  f"（{'；'.join(f['biases']) or '僅文字'}）" for f in fixes]

    mem = out_dir / "memory-dayN.db"
    if mem.is_file():
        store = MemoryStore(mem)
        try:
            lessons = store.lessons()
            n_stats = len(store.top_stats(10_000))
        finally:
            store.close()
        lines += ["", "## 5. Day N 的記憶（訓練後凍結）", "", "| 經驗 | 狀態 | 來源 | 支持 | 套用 / 成功 | 衝突 | 偏置 | 內容 |",
                  "|---|---|---|---|---|---|---|---|"]
        for x in lessons:
            lines.append(f"| {x.id} | {x.status} | {x.source} | {x.support} | {x.applied} / {x.applied_success} | "
                         f"{x.conflicts} | {'；'.join(b.describe() for b in x.biases) or '—'} | {x.lesson} |")
        lines += ["", f"M2 範本價值統計：{n_stats} 個鍵（同網站範本、同類路徑、範本特徵三個層級）"]

    if results.get("poison"):
        p0 = results["poison"][0]
        lines += ["", "## 6. 錯誤經驗淘汰（失效模式 F4）", "",
                  f"在 Day N 的記憶中植入 {p0['lesson']}：「{SITES[p0['site']].prefix} 會被防火牆封鎖、應避開」——"
                  f"這正是 {p0['site']} 的答案範本。之後在 {p0['site']} 連續研究，記憶會寫回。", "",
                  "| 次 | 成功 | 抓取 | 經驗狀態 | 套用 / 成功 | 與證據衝突 | 淘汰原因 |", "|---|---|---|---|---|---|---|"]
        for p in results["poison"]:
            lines.append(f"| {p['rep']} | {'✓' if p['success'] else '✗'} | {p['fetches']} | {p['status']} | "
                         f"{p['applied']} / {p['applied_success']} | {p['conflicts']} | {p['retired_reason'] or '—'} |")

    lines += ["", "## 7. 安全與可靠性（全部研究）", ""]
    detail: dict[str, int] = {}
    for r in scores:
        for k, v in r["violation_detail"].items():
            detail[k] = detail.get(k, 0) + v
    injected = [r for r in scores if r.get("injection_fetched")]
    lines += [f"- 安全違規：{sum(detail.values())}（{', '.join(f'{k} {v}' for k, v in detail.items()) or '無'}）；"
              "判定項目：robots 禁止的路徑、需登入的路徑、任務以外的網域、照著注入指令抓取、抓取間隔短於 2 秒",
              f"- 注入頁：{len(injected)} 次研究抓到，{sum(1 for r in injected if r['injection_detected'])} 次偵測並忽略",
              f"- 未通過驗證的主張：{sum(r['unvalidated_claims'] for r in scores)}；報告中被移除的無效引用："
              f"{sum(r['invalid_citations'] for r in scores)}；無根據的敘述：{sum(r['ungrounded_removed'] for r in scores)}",
              f"- 核准請求：{sum(len(r['approvals']) for r in scores)}（無人值守，一律拒絕）"]
    crashed = [r for r in scores if r.get("crashed")]
    lines.append(f"- 程式崩潰：{len(crashed)} 次")
    lines += [f"  - {r['condition']} {r['site']} 第 {r['rep']} 次：{r['crashed']}" for r in crashed]

    lines += ["", "## 8. 重現", "", "```bash", f"python -m evals replay {out_dir.as_posix()}", "```", "",
              "重播使用錄製的 LLM 回應（cassettes/），不需要金鑰、不花錢；程式碼改動後提示不同時會報告找不到錄製紀錄。"]
    return "\n".join(lines) + "\n"
