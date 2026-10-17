"""把評測結果整理成 Markdown 報告：各條件的彙總、Day 1 → Day N 的配對差異（bootstrap 95% 信賴區間）、
消融、緊縮預算、訓練過程與學到的記憶。"""

from __future__ import annotations

import random
import statistics
from pathlib import Path

from seo_intel.memory.store import MemoryStore

MAIN = ("day1", "dayN", "dayN-noM1", "dayN-noM2")
LABEL = {"day1": "Day 1（無記憶）", "dayN": "Day N（學習後）", "dayN-noM1": "Day N 關閉 M1", "dayN-noM2": "Day N 關閉 M2"}


def bootstrap(values: list[float], n: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """平均值與 95% bootstrap 信賴區間（樣本很小時區間會很寬，照實呈現）。"""
    if not values:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(n))
    return statistics.fmean(values), means[int(0.025 * n)], means[int(0.975 * n) - 1]


def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else float("nan")


def summarize(rows: list[dict]) -> dict:
    fke = [r["first_key_evidence"] for r in rows if r["first_key_evidence"] is not None]
    return {
        "n": len(rows), "success": _mean([float(r["success"]) for r in rows]), "f1": _mean([r["f1"] for r in rows]),
        "fetches": _mean([r["fetches"] for r in rows]), "fke": statistics.median(fke) if fke else float("nan"),
        "fke_missing": len(rows) - len(fke), "waste": _mean([r["waste_rate"] for r in rows]),
        "llm_usd": _mean([r["llm_usd"] for r in rows]), "steps": _mean([r["steps"] for r in rows]),
        "violations": sum(r["violations"] for r in rows),
        "approvals": sum(len(r["approvals"]) for r in rows), "unvalidated": sum(r["unvalidated_claims"] for r in rows),
    }


def _pairs(scores: list[dict], a: str, b: str) -> list[tuple[dict, dict]]:
    idx = {(r["scenario"], r["seed"]): r for r in scores if r.get("condition") == a}
    return [(idx[(r["scenario"], r["seed"])], r) for r in scores
            if r.get("condition") == b and (r["scenario"], r["seed"]) in idx]


def _fmt(x: float, d: int = 2) -> str:
    return "—" if x != x else f"{x:.{d}f}"


def render(results: dict, out_dir: Path) -> str:
    meta, scores, fixes = results["meta"], results["scores"], results["corrections"]
    by = {}
    for r in scores:
        by.setdefault(r.get("condition"), []).append(r)
    lines = [f"# 評測報告：{meta.get('name', out_dir.name)}", "",
             f"- LLM：`{meta['llm']}`；種子數：{meta['seeds']}；核准政策（無人值守）：{meta['on_approval']}",
             f"- 測試集：{', '.join(meta['test'])}；訓練集：{', '.join(meta['train'])}",
             f"- episode 數：{len(scores)}；LLM 總花費：US${sum(r['llm_usd'] for r in scores):.2f}"
             + ("（FakeLLM 的花費是依 token 估算的模擬值）" if meta["llm"] == "fake" else ""),
             f"- 全部 episode 的安全違規（伺服器判定）：{sum(r['violations'] for r in scores)}", ""]

    lines += ["## 1. 測試集彙總", "",
              "| 條件 | n | 成功率 | F1 | 平均抓取 | 首個關鍵證據（中位數） | 浪費率 | LLM US$ | 步驟 | 違規 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for c in MAIN:
        if c in by:
            s = summarize(by[c])
            miss = f"（{s['fke_missing']} 次沒抓到）" if s["fke_missing"] else ""
            lines.append(f"| {LABEL[c]} | {s['n']} | {s['success']:.0%} | {_fmt(s['f1'])} | {_fmt(s['fetches'], 1)} | "
                         f"{_fmt(s['fke'], 1)}{miss} | {s['waste']:.1%} | {_fmt(s['llm_usd'], 4)} | {_fmt(s['steps'], 0)} | "
                         f"{s['violations']} |")

    pairs = _pairs(scores, "day1", "dayN")
    if pairs:
        lines += ["", "## 2. Day 1 → Day N 配對比較（同一情境、同一種子）", "",
                  "| 情境 | 種子 | 成功 | 抓取 | 首個關鍵證據 | LLM US$ | 步驟 | 套用經驗 |", "|---|---|---|---|---|---|---|---|"]
        for a, b in pairs:
            ok = lambda r: "✓" if r["success"] else "✗"  # noqa: E731
            lines.append(f"| {a['scenario']} | {a['seed']} | {ok(a)} → {ok(b)} | {a['fetches']} → {b['fetches']} | "
                         f"{a['first_key_evidence']} → {b['first_key_evidence']} | {a['llm_usd']:.4f} → {b['llm_usd']:.4f} | "
                         f"{a['steps']} → {b['steps']} | {', '.join(b['lessons_applied']) or '—'} |")
        lines += ["", "| 指標 | Day 1 平均 | Day N 平均 | 配對差異（95% CI） | 相對變化 |", "|---|---|---|---|---|"]
        for key, label in (("fetches", "抓取次數"), ("first_key_evidence", "首個關鍵證據抓取數"), ("llm_usd", "LLM US$"),
                           ("steps", "步驟數"), ("success", "成功")):
            ps = [(float(a[key]), float(b[key])) for a, b in pairs if a[key] is not None and b[key] is not None]
            if not ps:
                continue
            m, lo, hi = bootstrap([y - x for x, y in ps])
            base = _mean([x for x, _ in ps])
            rel = f"{m / base:+.0%}" if base else "—"
            d = 4 if key == "llm_usd" else 2
            lines.append(f"| {label} | {_fmt(base, d)} | {_fmt(_mean([y for _, y in ps]), d)} | "
                         f"{_fmt(m, d)}（{_fmt(lo, d)} ～ {_fmt(hi, d)}） | {rel} |")

    caps = sorted({r["budget"] for r in scores if "budget" in r})
    if caps:
        lines += ["", "## 3. 緊縮預算（抓取上限；預算用盡時的核准請求依政策處理）", "",
                  "| 抓取上限 | Day 1 成功率 | Day N 成功率 | Day 1 核准請求 | Day N 核准請求 |", "|---|---|---|---|---|"]
        for cap in caps:
            d1, dn = summarize(by.get(f"day1-{cap}", [])), summarize(by.get(f"dayN-{cap}", []))
            lines.append(f"| {cap} | {d1['success']:.0%} | {dn['success']:.0%} | {d1['approvals']} | {dn['approvals']} |")

    train = sorted(by.get("train", []), key=lambda r: r["order"])
    if train:
        lines += ["", "## 4. 訓練過程（共用記憶）", "", "| # | 情境 | 種子 | 成功 | 抓取 | 套用經驗 |", "|---|---|---|---|---|---|"]
        for r in train:
            lines.append(f"| {r['order']} | {r['scenario']} | {r['seed']} | {'✓' if r['success'] else '✗'} | {r['fetches']} | "
                         f"{', '.join(r['lessons_applied']) or '—'} |")
        lines += ["", f"腳本化的人工糾偏：{len(fixes)} 次"]
        lines += [f"- {f['scenario']}（種子 {f['seed']}）把 `{f['claim_template']}` 判為原因 → 「{f['reason']}」→ 經驗 {f['lesson']}"
                  for f in fixes]

    mem = out_dir / "memory-dayN.db"
    if mem.is_file():
        store = MemoryStore(mem)
        lessons = store.lessons()
        lines += ["", "## 5. Day N 的記憶（凍結快照）", "", "| 經驗 | 狀態 | 來源 | 支持 | 套用 / 成功 | 衝突 | 內容 |",
                  "|---|---|---|---|---|---|---|"]
        for x in lessons:
            lines.append(f"| {x.id} | {x.status} | {x.source} | {x.support} | {x.applied} / {x.applied_success} | {x.conflicts} | "
                         f"{x.lesson} |")
        n_stats = len(store.top_stats(10_000))
        store.close()
        lines += ["", f"M2 範本價值統計：{n_stats} 個鍵（同網站範本、同類路徑、範本特徵三個層級）"]
    return "\n".join(lines) + "\n"
