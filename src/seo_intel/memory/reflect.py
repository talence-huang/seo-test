"""REFLECT：episode 結束時，以確定性規則從結果中學習，並治理既有經驗。

- M2：只從「成功」的 episode 學 URL 價值（結論不明時不知道哪個範本才是答案，學了只會學到雜訊）
- M1：從失敗（被封鎖的路徑、JS 空殼頁）與成功（答案範本具備哪些訊號、免費證據是否已足夠）萃取候選經驗
- 治理：候選經驗需 2 個 episode 支持才生效；套用後成功率偏低或與證據衝突的自動經驗會被淘汰；
  人工糾偏的經驗不會被 Agent 淘汰，只會標記「建議人工檢視」（淘汰人工經驗屬於需核准的動作）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from seo_intel.core import beliefs, probes
from seo_intel.core.actions import signal_templates
from seo_intel.core.context import EpisodeContext
from seo_intel.memory import priors
from seo_intel.memory.models import Bias, Lesson

ACTIVATE_AT = 2          # 需要幾個不同 episode 支持才生效
RETIRE_MIN_APPLIED = 3
RETIRE_BELOW = 0.5
RETIRE_CONFLICTS = 2


@dataclass
class Draft:
    source: str
    signature: str
    trigger: str
    lesson: str
    biases: list[Bias]
    observations: list[str] = field(default_factory=list)


def episode_success(ctx: EpisodeContext) -> bool:
    st = ctx.state
    return st.end_reason == "hypothesis_supported" and bool(st.claims) and all(c.validated for c in st.claims)


# ---------- M2 ----------

def update_url_values(ctx: EpisodeContext, store) -> list[str]:
    st, fr = ctx.state, ctx.frontier
    goal_type = st.goal.goal_type
    changed = {u for c in st.claims for u in st.template_changes.get(c.template, [])}
    per: dict[str, list[int]] = {}
    for url, info in st.fetched.items():
        if info.get("status") not in (200, 403) or url.split("/")[2] not in st.task.competitors:
            continue
        hit = int(url in changed)
        per.setdefault(info["template"], [0, 0])[0 if hit else 1] += 1
    lines = []
    for tkey, (hits, misses) in sorted(per.items(), key=lambda kv: -kv[1][0]):
        stat = fr.templates.get(tkey)
        n_urls = stat.n_urls if stat else 0
        burst = stat.burst if stat else None
        for scope, key in priors.keys(goal_type, tkey, n_urls, burst):
            store.add_reward(st.id, scope, key, tkey, hits, misses)
        if hits:
            lines.append(f"M2 {tkey}：命中 {hits} / 抓取 {hits + misses}")
    total_hits = sum(h for h, _ in per.values())
    lines.insert(0, f"M2：更新 {len(per)} 個範本的價值（命中 {total_hits} 頁、未命中 {sum(m for _, m in per.values())} 頁），"
                    f"同時寫入「同網站範本／同類路徑／範本特徵」三個層級")
    return lines


# ---------- M1：萃取候選經驗 ----------

def _first_segment(tkey: str) -> str:
    return "/" + tkey.split("/", 1)[1].split("/", 1)[0] + "/"


def derive(ctx: EpisodeContext, success: bool) -> list[Draft]:
    st, fr = ctx.state, ctx.frontier
    drafts: list[Draft] = []
    for stat in fr.templates.values():
        if stat.domain not in st.task.competitors or not stat.n_fetched:
            continue
        path = _first_segment(stat.key)
        if stat.n_blocked and stat.n_blocked == stat.n_fetched:
            drafts.append(Draft("failure", f"avoid_path|*|{path}|waf", f"抓取 {path} 開頭的網址時",
                                f"{path} 開頭的頁面會被網站防火牆封鎖（{stat.domain} 回應 403），應直接避開，改用其他範本或歷史快照。",
                                [Bias(type="avoid_path", path=path, reason="waf")]))
        shells = [u for u, i in st.fetched.items() if i.get("template") == stat.key and i.get("js_shell")]
        if shells and len(shells) == stat.n_fetched:
            drafts.append(Draft("failure", f"avoid_path|*|{path}|js_shell", f"抓取 {path} 開頭的網址時",
                                f"{path} 開頭的頁面是 JS 空殼頁（沒有可萃取的內容），抓取只浪費預算。",
                                [Bias(type="avoid_path", path=path, reason="js_shell")]))
    if not success:
        return drafts
    signals = signal_templates(ctx)
    if all(c.template in signals for c in st.claims):
        drafts.append(Draft("success", "sampling_focus", "需要在競品網站抽樣範本時",
                            "答案範本在探勘階段就有訊號（出現在 SERP 排名網址，或期間內更新比例遠高於全站平常）；"
                            "抽樣時先處理有訊號的範本，其餘範本延後。",
                            [Bias(type="sampling_focus")]))
    free = set(probes.FREE)
    costly_used = [p for p in st.probes if p.kind in probes.COSTLY and p.status == "evaluated"]
    if costly_used:
        same = True
        for h in st.hypotheses:
            if h.status in ("supported", "refuted"):
                p_free = beliefs.posterior(h.prior, [e for e in st.evidence_for(h.id) if e.probe in free])
                if (h.status == "supported" and p_free < 0.9) or (h.status == "refuted" and p_free > beliefs.REFUTED_AT):
                    same = False
        if same and any(h.status == "supported" for h in st.hypotheses):
            drafts.append(Draft("success", "skip_costly_probes|0.9", "免費證據已明確分出高下時",
                                "免費探針（對照組、SERP 排名網址、更新爆發度、時序）已讓主要假設信心 ≥ 0.9、其他 ≤ 0.2 時，"
                                "付費探針不會改變結論，可以省下抓取與 API 預算。",
                                [Bias(type="skip_costly_probes", threshold=0.9)]))
    return drafts


# ---------- 治理 ----------

def _upsert(store, d: Draft, episode_id: str, lines: list[str]) -> None:
    existing = store.by_signature(d.signature)
    if existing is None:
        lesson = Lesson(id=store.next_lesson_id(), signature=d.signature, source=d.source, scope={"goal_type": "*"},
                        trigger=d.trigger, lesson=d.lesson, biases=d.biases, status="candidate",
                        provenance={"episodes": [episode_id]})
        store.save_lesson(lesson)
        store.event(lesson.id, episode_id, "created", d.lesson)
        lines.append(f"新增候選經驗 {lesson.id}（{d.source}）：{d.lesson}")
        return
    episodes = existing.provenance.setdefault("episodes", [])
    if episode_id in episodes or existing.status == "retired":
        return
    episodes.append(episode_id)
    existing.support = len(episodes)
    store.event(existing.id, episode_id, "supported", f"支持數 {existing.support}")
    if existing.status == "candidate" and existing.support >= ACTIVATE_AT:
        existing.status = "active"
        store.event(existing.id, episode_id, "activated", f"{existing.support} 個 episode 支持")
        lines.append(f"經驗 {existing.id} 已有 {existing.support} 個 episode 支持 → 生效：{existing.lesson}")
    else:
        lines.append(f"經驗 {existing.id} 支持數 {existing.support}（{existing.status}）")
    store.save_lesson(existing)


def govern_applied(ctx: EpisodeContext, store, success: bool, lines: list[str]) -> None:
    st = ctx.state
    claim_templates = {c.template for c in st.claims}
    for lid, places in st.lessons_applied.items():
        lesson = store.lesson(lid)
        if lesson is None:
            continue
        lesson.applied += 1
        lesson.applied_success += int(success)
        store.event(lid, st.id, "applied", f"{'成功' if success else '未成功'}；套用於 {', '.join(places)}")
        for b in lesson.biases:
            if b.type == "avoid_path" and any(
                    b.domain in ("*", t.split("/", 1)[0]) and f"/{t.split('/', 1)[1]}".startswith(b.path)
                    for t in claim_templates):
                lesson.conflicts += 1
                store.event(lid, st.id, "conflict", f"成立的主張屬於被避開的路徑 {b.path}")
                lines.append(f"⚠ 經驗 {lid} 與證據衝突：成立的主張落在它要求避開的 {b.path}")
        maybe_retire(store, lesson, st.id, lines)
        store.save_lesson(lesson)


def maybe_retire(store, lesson: Lesson, episode_id: str, lines: list[str]) -> None:
    """淘汰規則：自動經驗與證據衝突 ≥ 2 次，或套用 ≥ 3 次且成功率 < 50%。人工經驗只標記、不淘汰。"""
    rate = lesson.success_rate
    if lesson.source == "human_correction":
        if lesson.conflicts:
            lines.append(f"⚠ 人工經驗 {lesson.id} 與證據衝突 {lesson.conflicts} 次：建議人工檢視（Agent 不會自行淘汰人工經驗）")
    elif lesson.status != "retired" and (
            lesson.conflicts >= RETIRE_CONFLICTS
            or (lesson.applied >= RETIRE_MIN_APPLIED and rate is not None and rate < RETIRE_BELOW)):
        lesson.status = "retired"
        lesson.retired_reason = (f"與證據衝突 {lesson.conflicts} 次" if lesson.conflicts >= RETIRE_CONFLICTS
                                 else f"套用 {lesson.applied} 次、成功率 {rate:.0%}")
        store.event(lesson.id, episode_id, "retired", lesson.retired_reason)
        lines.append(f"淘汰經驗 {lesson.id}：{lesson.retired_reason}")


def reflect(ctx: EpisodeContext) -> tuple[list[str], dict]:
    st, mem = ctx.state, ctx.memory
    store = mem.store
    success = episode_success(ctx)
    lines = [f"結果：{'成功' if success else '未成功'}（{st.end_reason}）"]
    if mem.use_m2 and success and st.goal is not None:
        lines += update_url_values(ctx, store)
    elif mem.use_m2:
        lines.append("M2：episode 未成功，不更新 URL 價值（避免從不確定的結論學到雜訊）")
    if mem.use_m1 and st.goal is not None:
        for d in derive(ctx, success):
            _upsert(store, d, st.id, lines)
        govern_applied(ctx, store, success, lines)
    store.record_outcome({"episode_id": st.id, "scenario": st.task.scenario,
                          "goal_type": st.goal.goal_type if st.goal else None, "success": success,
                          "end_reason": st.end_reason, "fetches": st.budget.fetches, "api_calls": st.budget.api_calls,
                          "llm_usd": st.budget.llm_usd, "lessons": sorted(st.lessons_applied)})
    summary = {"success": success, "lessons_applied": st.lessons_applied,
               "active_lessons": len(store.lessons("active")), "candidate_lessons": len(store.lessons("candidate"))}
    return lines, summary
