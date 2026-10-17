"""評測入口：python -m evals <指令>

  run      完整評測：Day 1 → 訓練 → Day N → 錯誤經驗淘汰（真實 Opus，會花錢；有花費上限）
           --resume-from <目錄>：先重播那份評測錄下的回應，提示不同時才呼叫 Opus（程式修正後接續用）
  one      開發驗證：用空記憶研究一個測試網站一次（真實 Opus）
  replay   用錄製的 LLM 回應重跑一份評測，確認結果相同（不需要金鑰、不花錢）
  report   從 results.json 重新產生 report.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPORTS = Path(__file__).resolve().parent / "reports"
COMPARE = ("success", "claims", "fetches", "first_key_evidence", "wasted", "steps", "llm_usd", "violations")


def _deterministic() -> None:
    """固定字串雜湊的種子：集合的走訪順序在錄製與重播之間必須一致，提示才會相同。"""
    if os.environ.get("PYTHONHASHSEED") != "0":
        env = {**os.environ, "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
        sys.exit(subprocess.call([sys.executable, "-m", "evals", *sys.argv[1:]], env=env))


def _settings():
    from seo_intel.config import load_settings
    from seo_intel.env import has_anthropic_credentials, load_env_file

    load_env_file()
    os.environ["SERPAPI_API_KEY"] = "eval-mock-key"   # SerpApi 由模擬傳輸回應，不會送出
    return load_settings(), has_anthropic_credentials()


def _meta(settings, cfg, name: str, **extra) -> dict:
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"name": name, "model": settings.models.roles["planner"].model, "reps": cfg.reps,
            "train_passes": cfg.train_passes, "max_usd": cfg.max_usd, "poison": cfg.poison,
            "train_sites": list(cfg.train_sites), "test_sites": list(cfg.test_sites), "commit": commit,
            "started": datetime.now().isoformat(timespec="seconds"), **extra}


def _write_report(out: Path) -> Path:
    from evals.report import render

    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    path = out / "report.md"
    path.write_text(render(results, out), encoding="utf-8")
    return path


def cmd_run(a) -> int:
    from evals.protocol import EvalConfig, protocol, save

    settings, has_key = _settings()
    if not has_key:
        print("需要 Anthropic 金鑰：在 .env 填入 ANTHROPIC_API_KEY")
        return 1
    name = a.name or datetime.now().strftime("%Y%m%d-%H%M")
    out = REPORTS / name
    if out.exists() and any(out.iterdir()):
        print(f"{out} 已存在；請換一個 --name（錄製檔不能混用）")
        return 1
    resume = Path(a.resume_from).resolve() if a.resume_from else None
    if resume is not None and not (resume / "cassettes").is_dir():
        print(f"{resume} 沒有錄製檔（cassettes/）")
        return 1
    cfg = EvalConfig(out_dir=out, reps=a.reps, train_passes=a.passes, max_usd=a.max_usd, poison=not a.no_poison,
                     resume_from=resume)
    print(f"評測 {name}：預計 {cfg.planned_runs} 次研究，花費上限 US${cfg.max_usd:.2f}；輸出 {out}"
          + (f"；接續 {resume.name}（有錄製的回應不重新呼叫 Opus）" if resume else ""))
    out.mkdir(parents=True, exist_ok=True)
    meta = _meta(settings, cfg, name, **({"resume_from": resume.name} if resume else {}))
    ev = protocol_with_meta(protocol, save, settings, cfg, meta)
    print(f"完成：{len(ev.scores)} 次研究，花費 US${ev.spent:.2f}；報告 {_write_report(out)}")
    return 0


def protocol_with_meta(protocol, save, settings, cfg, meta):
    from evals.protocol import EvalState

    save(EvalState(), cfg, meta)
    ev = protocol(settings, cfg)
    save(ev, cfg, {"finished": datetime.now().isoformat(timespec="seconds"), "spent": round(ev.spent, 4)})
    return ev


def cmd_one(a) -> int:
    from evals.protocol import EvalConfig, EvalState, run_one

    settings, has_key = _settings()
    if not has_key:
        print("需要 Anthropic 金鑰：在 .env 填入 ANTHROPIC_API_KEY")
        return 1
    out = REPORTS / f"dev-{a.site}-{datetime.now():%Y%m%d-%H%M%S}"
    cfg = EvalConfig(out_dir=out, max_usd=a.max_usd)
    ev = EvalState()
    _, s = run_one(a.site, settings, cfg, ev, condition="dev", rep=1)
    (out / "score.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: s[k] for k in ("success", "end_reason", "strategies", "strategy_wrong", "strategy_recall",
                                        "strategy_precision", "actions_top3", "action_precision", "wrong_actions",
                                        "fetches", "wasted", "llm_usd", "violation_detail", "injection_detected")},
                     ensure_ascii=False))
    print(f"研究紀錄：{out / 'runs'}")
    return 0


def cmd_replay(a) -> int:
    from evals.protocol import EvalConfig, protocol, save

    src = Path(a.dir).resolve()
    original = json.loads((src / "results.json").read_text(encoding="utf-8"))
    m = original["meta"]
    settings, _ = _settings()
    out = src / "replay"
    if out.exists():
        import shutil
        shutil.rmtree(out)
    cfg = EvalConfig(out_dir=out, reps=m["reps"], train_passes=m["train_passes"], max_usd=m["max_usd"],
                     poison=m["poison"], train_sites=tuple(m["train_sites"]), test_sites=tuple(m["test_sites"]),
                     replay_from=src)
    out.mkdir(parents=True)
    protocol_with_meta(protocol, save, settings, cfg, {**m, "replay": True, "name": f"{m['name']}（重播）"})
    replayed = json.loads((out / "results.json").read_text(encoding="utf-8"))
    key = lambda r: (r["condition"], r["site"], r["rep"])  # noqa: E731
    new = {key(r): r for r in replayed["scores"]}
    diffs = []
    for r in original["scores"]:
        other = new.get(key(r))
        if other is None:
            diffs.append(f"{key(r)}：重播沒有這次研究")
            continue
        for k in COMPARE:
            if r[k] != other[k]:
                diffs.append(f"{key(r)} {k}：{r[k]} → {other[k]}")
    print(f"重播 {len(replayed['scores'])} / {len(original['scores'])} 次研究；報告 {_write_report(out)}")
    print("結果完全相同" if not diffs else f"有 {len(diffs)} 項不同：\n" + "\n".join(diffs[:40]))
    return 0 if not diffs else 2


def cmd_report(a) -> int:
    print(_write_report(Path(a.dir)))
    return 0


def main() -> int:
    _deterministic()
    p = argparse.ArgumentParser(prog="python -m evals", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="完整評測（真實 Opus）")
    r.add_argument("--name", help="評測名稱（輸出到 evals/reports/<名稱>）")
    r.add_argument("--reps", type=int, default=3, help="每個測試網站研究幾次")
    r.add_argument("--passes", type=int, default=2, help="訓練網站跑幾輪")
    r.add_argument("--max-usd", type=float, default=13.5, help="LLM 花費上限（含預估，超過即停止）")
    r.add_argument("--no-poison", action="store_true", help="不做錯誤經驗淘汰")
    r.add_argument("--resume-from", help="接續這份評測（目錄）：有錄製的回應直接重播，不重新花錢")
    o = sub.add_parser("one", help="開發驗證：研究一個測試網站一次（真實 Opus）")
    o.add_argument("site", choices=sorted(__import__("evals.sites", fromlist=["SITES"]).SITES))
    o.add_argument("--max-usd", type=float, default=2.0)
    for name, help_ in (("replay", "用錄製的回應重跑並比對結果"), ("report", "重新產生 report.md")):
        x = sub.add_parser(name, help=help_)
        x.add_argument("dir", help="評測目錄，例如 evals/reports/20261017-1500")
    a = p.parse_args()
    return {"run": cmd_run, "one": cmd_one, "replay": cmd_replay, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
