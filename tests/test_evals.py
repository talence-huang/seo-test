"""評測工具本身的測試（不呼叫 LLM、不花錢）：測試網站的標準答案、評分器、評測流程與重播。

真實評測（python -m evals run）會呼叫 Opus；這裡用「每次呼叫都失敗」的替身跑完整流程，
驗證流程、記憶隔離、錯誤經驗淘汰與重播的機制（研究會以「沒有可檢驗的假設」結束）。
"""

import json

from evals import sites
from evals.protocol import EvalConfig, VirtualClock, protocol
from evals.report import render
from seo_intel.llm.types import LLMFatalError
from seo_intel.tools.diff import diff_features
from seo_intel.tools.extract import extract


class _NoLLM:
    def call(self, role, *, system, user, schema):
        raise LLMFatalError("評測測試：不呼叫 LLM")


class _Boom:
    """接續時不該被呼叫的 LLM：錄製檔涵蓋所有提示時，研究完全不需要呼叫它（被呼叫就會崩潰）。"""

    def call(self, role, *, system, user, schema):
        raise RuntimeError("接續時錄製檔應該涵蓋所有提示")


def test_sites_have_known_answers():
    for sid, spec in sites.SITES.items():
        s = sites.site(sid)
        truth = s.truth
        assert len(truth.key_urls) >= 10 and all(u.startswith(f"https://{spec.host}{spec.prefix}") for u in truth.key_urls)
        assert not any("answer" in f for f in s.sitemap_files())          # sitemap 檔名不洩漏答案
        serp = [r["link"] for r in s.serp(spec.keywords[0])]
        assert sum(u in truth.key_urls for u in serp) == 4                 # SERP 上排名的是答案範本
        assert ("/private/" in s.robots()) and (("Sitemap:" in s.robots()) == spec.declared)
        assert any(p.kind == "compare" for p in s.pages.values()) == spec.traps


def test_pages_carry_the_intended_signals():
    s = sites.site("s1")
    key = next(p for p in s.groups["answer"] if p.key and p.before is not None)
    url = s.url(key)
    after, before = extract(s.html(key), url), extract(s.html(key, key.before), url)
    d = diff_features(before, after)
    assert set(d["change_types"]) == set(s.truth.change_types)
    assert before.modified_date() < sites.WINDOW_START <= after.modified_date()
    tag = next(p for p in s.groups["tag"] if p.modified >= sites.WINDOW_START)
    assert not diff_features(extract(s.html(tag, tag.before), s.url(tag)), extract(s.html(tag), s.url(tag)))["significant"]
    app = next(p for p in s.groups["page"] if p.kind == "app")
    assert extract(s.html(app), s.url(app)).js_shell
    inj = next(p for p in s.groups["post"] if p.injection)
    assert extract(s.html(inj), s.url(inj)).suspicious_instructions


def test_world_logs_and_flags_requests():
    s = sites.site("s5")
    world = sites.World(s, VirtualClock())
    c = world.client()
    cmp_page = next(p for p in s.groups["page"] if p.kind == "compare")
    assert c.get(s.url(cmp_page)).status_code == 403
    assert c.get(f"https://{s.spec.host}/private/x/").status_code == 404
    assert c.get("https://elsewhere.test/").status_code == 404
    key = next(p for p in s.groups["answer"] if p.key and p.before is not None)
    cdx = c.get("https://web.archive.org/cdx/search/cdx", params={"url": s.url(key), "output": "json"}).json()
    snap = c.get(f"https://web.archive.org/web/{cdx[1][0]}id_/{s.url(key)}").text
    assert extract(snap, s.url(key)).word_count < extract(s.html(key), s.url(key)).word_count
    flags = [x.flags for x in world.log]
    assert ("waf",) in flags and ("off_target",) in flags
    assert ("not_found", "robots_disallowed") in [tuple(sorted(f)) for f in flags]


def test_protocol_isolates_memory_and_replays(tmp_path, settings, monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "eval-mock-key")
    common = {"reps": 1, "train_passes": 1, "train_sites": ("s1", "s2"), "test_sites": ("s6", "s8"),
              "progress": lambda _: None}
    out = tmp_path / "run"
    ev = protocol(settings, EvalConfig(out_dir=out, live=_NoLLM, **common))
    assert ev.stopped is None and len(ev.scores) == 2 + 2 + 2 + 3
    assert all(r["violations"] == 0 for r in ev.scores)
    day1 = {r["site"]: r for r in ev.scores if r["condition"] == "day1"}
    dayn = {r["site"]: r for r in ev.scores if r["condition"] == "dayN"}
    assert day1["s6"]["wasted"] > 0 and dayn["s6"]["wasted"] == 0          # 兩個訓練網站都遇到 → 經驗生效、避開
    assert dayn["s6"]["lessons_applied"] and not day1["s6"]["lessons_applied"]
    # 沒有 LLM 時每次研究都失敗（對照組成功率 0%）：不能把失敗怪到植入的經驗上，所以不淘汰（淘汰規則見 test_units）
    assert [p["status"] for p in ev.poison] == ["active"] * 3 and ev.poison[-1]["applied"] == 3
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    report = render({**results, "meta": {"test_sites": ["s6", "s8"], "train_sites": ["s1", "s2"]}}, out)
    assert "## 6. 錯誤經驗淘汰" in report and "Day N 的記憶" in report

    replay = protocol(settings, EvalConfig(out_dir=tmp_path / "replay", replay_from=out, **common))
    resumed = protocol(settings, EvalConfig(out_dir=tmp_path / "resume", resume_from=out, live=_Boom, **common))
    keys = ("condition", "site", "rep", "success", "fetches", "first_key_evidence", "wasted", "steps", "lessons_applied")
    rows = lambda e: sorted(({k: r[k] for k in keys} for r in e.scores), key=lambda r: (r["condition"], r["site"], r["rep"]))  # noqa: E731
    assert rows(replay) == rows(ev) == rows(resumed)
    assert not any(r.get("crashed") for e in (ev, replay, resumed) for r in e.scores)
