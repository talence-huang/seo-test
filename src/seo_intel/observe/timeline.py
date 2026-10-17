"""產生單一檔案的 timeline.html：事後在瀏覽器中檢視整個 episode 的決策過程（無外部依賴）。"""

from __future__ import annotations

import html
import json
from typing import Any

from seo_intel.core.actions import COMPONENT_LABELS
from seo_intel.observe.console import llm_line
from seo_intel.observe.record import StepRecord

_PHASE_COLOR = {"discovery": "#7F77DD", "sampling": "#1D9E75", "targeted": "#D85A30", "verification": "#888780"}
_STATUS = {"resolved": ("已解答", "ok"), "exhausted": ("停止追查", "warn"), "open": ("未解答", "muted")}

_CSS = """
:root{--bg:#fafaf8;--fg:#1f1f1d;--muted:#6b6a65;--card:#fff;--line:#e3e1da;--ok:#0F6E56;--warn:#854F0B;--accent:#534AB7}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1a;--fg:#ecebe6;--muted:#a3a29b;--card:#262624;--line:#3a3a37;--ok:#5DCAA5;--warn:#EF9F27;--accent:#AFA9EC}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Segoe UI","Noto Sans TC",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px}
.meta{color:var(--muted);font-size:13px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.card b{display:block;font-size:18px;overflow-wrap:anywhere}
.card span{color:var(--muted);font-size:12px}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:13px}th{color:var(--muted);font-weight:500}
.ok{color:var(--ok)}.warn{color:var(--warn)}.muted{color:var(--muted)}code,pre{font-family:ui-monospace,Consolas,monospace;font-size:12px}
pre{white-space:pre-wrap;word-break:break-all;background:var(--bg);padding:8px;border-radius:6px;margin:6px 0}
details summary{cursor:pointer}.pill{display:inline-block;padding:0 8px;border-radius:999px;font-size:11px;color:#fff}
.step{font-weight:600}.wrap{overflow-x:auto}svg text{fill:var(--muted);font-size:11px}
"""


def _e(x: Any) -> str:
    return html.escape(str(x))


def _budget_svg(records: list[StepRecord], fetch_cap: int) -> str:
    pts = [(r.seq, r.budget.get("fetches", 0), r.phase) for r in records if r.budget]
    if not pts:
        return ""
    w, h, pad = 1100, 220, 34
    max_seq = max(p[0] for p in pts) or 1
    x = lambda s: pad + (w - 2 * pad) * s / max_seq  # noqa: E731
    y = lambda v: h - pad - (h - 2 * pad) * v / fetch_cap  # noqa: E731
    bands, start = [], pts[0]
    for prev, cur in zip(pts, pts[1:] + [(max_seq + 1, 0, None)]):
        if cur[2] != start[2]:
            bands.append(f'<rect x="{x(start[0]):.1f}" y="{pad}" width="{max(1, x(prev[0]) - x(start[0])):.1f}" '
                         f'height="{h - 2 * pad}" fill="{_PHASE_COLOR.get(start[2], "#888")}" opacity="0.12"/>'
                         f'<text x="{x(start[0]) + 4:.1f}" y="{pad + 12}">{_e(start[2])}</text>')
            start = cur
    line = " ".join(f"{x(s):.1f},{y(v):.1f}" for s, v, _ in pts)
    grid = "".join(f'<line x1="{pad}" x2="{w - pad}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="currentColor" opacity="0.1"/>'
                   f'<text x="2" y="{y(v) + 4:.1f}">{v}</text>' for v in range(0, fetch_cap + 1, max(1, fetch_cap // 5)))
    return (f'<div class="wrap"><svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="抓取次數隨步驟累積">'
            f'{grid}{"".join(bands)}<polyline points="{line}" fill="none" stroke="var(--accent)" stroke-width="2"/>'
            f'<text x="{w - pad - 60}" y="{h - 8}">步驟 →</text></svg></div>')


def _breakdown(parts: dict[str, float]) -> str:
    shown = [f"{COMPONENT_LABELS.get(k, k)} {v:+.2f}" for k, v in parts.items() if abs(v) >= 0.005]
    return "　".join(shown) or "—"


_HYP_COLORS = ("#534AB7", "#0F6E56", "#D85A30", "#993556", "#185FA5", "#854F0B", "#5F5E5A", "#3B6D11")
_STATUS_ZH = {"proposed": "提出", "testing": "測試中", "supported": "成立", "refuted": "推翻",
              "inconclusive": "無結論", "rejected": "拒收"}


def _belief_svg(history: list[dict], hypotheses: list[dict]) -> str:
    """每個假設的信心隨證據累積的變化（橫軸＝第幾筆證據）；虛線為「成立」(0.8) 與「推翻」(0.2) 門檻。"""
    if not history:
        return ""
    w, h, pad = 1100, 260, 34
    n = len(history)
    x = lambda i: pad + (w - 2 * pad) * i / max(1, n - 1)  # noqa: E731
    y = lambda p: h - pad - (h - 2 * pad) * p  # noqa: E731
    lines, legend = [], []
    for k, hyp in enumerate(hy for hy in hypotheses if hy["status"] != "rejected"):
        pts = [(i, p) for i, p in enumerate(history) if p["hypothesis_id"] == hyp["id"]]
        if not pts:
            continue
        color = _HYP_COLORS[k % len(_HYP_COLORS)]
        steps = []   # 階梯線：信心只在套用證據時改變
        for j, (i, p) in enumerate(pts):
            if j:
                steps.append(f"{x(i):.1f},{y(pts[j - 1][1]['posterior']):.1f}")
            steps.append(f"{x(i):.1f},{y(p['posterior']):.1f}")
        dots = "".join(f'<circle cx="{x(i):.1f}" cy="{y(p["posterior"]):.1f}" r="3" fill="{color}">'
                       f'<title>{_e(hyp["id"])} {_e(p.get("evidence_id") or "先驗")}：{p["posterior"]:.2f}</title></circle>'
                       for i, p in pts)
        lines.append(f'<polyline points="{" ".join(steps)}" fill="none" stroke="{color}" stroke-width="2"/>{dots}')
        legend.append(f'<span style="color:{color}">■ {_e(hyp["id"])} {_e(hyp.get("template") or "全站因素")}'
                      f'（{_STATUS_ZH[hyp["status"]]} {hyp["posterior"]:.2f}）</span>')
    guides = "".join(f'<line x1="{pad}" x2="{w - pad}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="currentColor" '
                     f'stroke-dasharray="4 4" opacity="0.35"/><text x="2" y="{y(v) + 4:.1f}">{v}</text>'
                     for v in (0.2, 0.8))
    return (f'<div class="meta">{" ".join(legend)}</div><div class="wrap"><svg viewBox="0 0 {w} {h}" width="100%" '
            f'role="img" aria-label="假設信心隨步驟變化">{guides}{"".join(lines)}'
            f'<text x="2" y="{y(1) + 4:.1f}">1.0</text><text x="2" y="{y(0) + 4:.1f}">0</text></svg></div>')


def _research_sections(state: dict) -> str:
    claims = state.get("claims", [])
    hyps = state.get("hypotheses", [])
    evidence = state.get("evidence", [])
    if not hyps:
        return ""
    report = state.get("report") or {}
    claim_rows = "".join(
        f"<tr><td>{_e(c['id'])}</td><td>{_e(c['subject_site'])}</td><td><code>{_e(c['template_prefix'])}</code></td>"
        f"<td>{_e(', '.join(c['change_types']))}</td><td>{_e(' ～ '.join(c['date_window'] or []))}</td>"
        f"<td>{c['confidence']:.2f}</td><td>{_e(', '.join(c['evidence_ids']))}</td>"
        f"<td class='{'ok' if c['validated'] else 'warn'}'>{'✓' if c['validated'] else '✗'}</td></tr>" for c in claims)
    claims_html = (f"<table><tr><th>主張</th><th>網站</th><th>範本</th><th>變動類型</th><th>時間窗</th><th>信心</th>"
                   f"<th>證據</th><th>驗證</th></tr>{claim_rows}</table>") if claims else "<p class='muted'>沒有假設達到成立門檻。</p>"
    hyp_rows = "".join(
        f"<tr><td>{_e(h['id'])}</td><td><code>{_e(h['category'])}</code></td><td>{_e(h.get('template') or '—')}</td>"
        f"<td>{_e(h['statement'])}<div class='muted'>預測：{_e(h['prediction'])}<br>反證：{_e(h['falsification'])}</div></td>"
        f"<td>{h['prior']:.2f} → <b>{h['posterior']:.2f}</b></td><td>{_STATUS_ZH[h['status']]}"
        f"{'（已紅隊驗證）' if h.get('verified') else ''}{'<br><span class=warn>' + _e(h['note']) + '</span>' if h.get('note') else ''}</td></tr>"
        for h in hyps)
    ev_rows = "".join(
        f"<tr><td>{_e(e['id'])}</td><td>{_e(e['hypothesis_id'])}</td><td><code>{_e(e['probe'])}</code></td>"
        f"<td class='{'ok' if e['direction'] == 'for' else 'warn'}'>{'支持' if e['direction'] == 'for' else '反對'}</td>"
        f"<td>{_e(e['strength'])}</td><td>{_e(', '.join(e['observation_ids']))}</td><td>{_e(e['note'])}</td></tr>"
        for e in evidence)
    cit = report.get("citations", {})
    head = (f"<p><b>{_e(report.get('headline', ''))}</b></p><p>{_e(report.get('summary', ''))}</p>"
            + (f"<p class='muted'>混淆因子：{_e(report['confounder'])}</p>" if report.get("confounder") else "")
            + (f"<p class='muted'>引用驗證：有效 {cit.get('valid', 0)}、移除無效引用 {cit.get('invalid_removed', 0)}</p>" if cit else ""))
    return (f"<h2>結論（程式由證據推導的結構化主張）</h2>{head}<div class='wrap'>{claims_html}</div>"
            f"<h2>假設信心隨證據累積的變化（橫軸＝第幾筆證據；滑鼠移到點上看證據 ID；虛線：成立 0.8／推翻 0.2）</h2>"
            f"{_belief_svg(state.get('belief_history', []), hyps)}"
            f"<h2>假設</h2><div class='wrap'><table><tr><th>ID</th><th>類別</th><th>範本</th><th>主張、預測與反證</th>"
            f"<th>先驗 → 信心</th><th>狀態</th></tr>{hyp_rows}</table></div>"
            f"<h2>證據（{len(evidence)}）</h2><div class='wrap'><table><tr><th>ID</th><th>假設</th><th>探針</th><th>方向</th>"
            f"<th>強度</th><th>觀察</th><th>判定依據</th></tr>{ev_rows}</table></div>")


def _step_row(r: StepRecord) -> str:
    color = _PHASE_COLOR.get(r.phase, "#888")
    inner = []
    if r.details:
        inner.append("<ul>" + "".join(f"<li>{_e(d)}</li>" for d in r.details) + "</ul>")
    if r.candidates:
        rows = "".join(f"<tr><td>{_e(c['action'])}</td><td>{c['score']:.2f}</td>"
                       f"<td>{_e(_breakdown(c['breakdown']))}</td><td>{_e(c['reason'])}</td></tr>"
                       for c in r.candidates)
        inner.append(f"<b>候選行動（前 5 名）</b><table><tr><th>行動</th><th>分數</th><th>分數明細</th><th>理由</th></tr>{rows}</table>")
    for rec in r.llm:
        inner.append(f"<b>{_e(llm_line(rec))}</b><pre>{_e(json.dumps(rec.get('output'), ensure_ascii=False, indent=2))}</pre>")
    if r.tool_calls:
        inner.append("<b>HTTP 請求</b><pre>" + _e("\n".join(
            f"{c['status'] or c['error']}  {c['url']}  (第 {c['attempt']} 次，等待 {c['waited_s']}s)" for c in r.tool_calls)) + "</pre>")
    body = "".join(inner)
    summary = f'<span class="step">{_e(r.step)}</span> {_e(r.summary)}'
    cell = f"<details><summary>{summary}</summary>{body}</details>" if body else summary
    return (f'<tr><td>{r.seq}</td><td><span class="pill" style="background:{color}">{_e(r.phase)}</span></td>'
            f'<td>{r.t:.0f}s</td><td>{cell}</td><td>{r.budget.get("fetches", "")}</td></tr>')


def render_timeline(records: list[StepRecord], state: dict, frontier: dict | None,
                    environment: dict | None = None) -> str:
    task, goal = state["task"], state.get("goal") or {}
    b = state["budget"]
    cap = records[-1].budget.get("fetch_cap", 300) if records else 300
    cards = [("狀態", f"{state['status']}"), ("結束原因", state.get("end_reason") or "—"),
             ("抓取", f"{b['fetches']} / {cap}"), ("API 呼叫", b["api_calls"]), ("LLM 花費", f"US${b['llm_usd']:.4f}"),
             ("行動 / 步驟", f"{b['actions']} / {state['seq']}"), ("環境時間", f"{b['elapsed_s'] / 60:.1f} 分")]
    needs = "".join(
        f"<tr><td>{_e(n['id'])}</td><td><code>{_e(n['kind'])}</code></td><td>{n['priority']}</td>"
        f"<td>{_e(', '.join(n['target_domains']))}</td><td>{_e(n['question'])}</td>"
        f"<td class='{_STATUS[n['status']][1]}'>{_STATUS[n['status']][0]}</td><td>{_e(n.get('resolution') or '—')}</td></tr>"
        for n in state.get("info_needs", []))
    tpl_rows = ""
    if frontier:
        stats = sorted(frontier["templates"], key=lambda s: (-s["n_fetched"], -s["n_urls"]))[:40]
        tpl_rows = "".join(
            f"<tr><td>{_e(s['key'])}</td><td>{s['n_urls']:,}</td><td>{s['n_recent']:,}</td><td>{s['n_fetched']}</td>"
            f"<td>{s['n_evidence']}</td><td>{s['alpha'] / (s['alpha'] + s['beta']):.2f}</td><td>{s['n_blocked']}</td>"
            f"<td>{_e(s['trap'] or '')}</td></tr>" for s in stats)
    obs = "".join(f"<tr><td>{_e(o['id'])}</td><td><code>{_e(o['kind'])}</code></td><td>{_e(o['summary'])}</td></tr>"
                  for o in state.get("observations", []))
    env = ""
    if environment:
        env = (f"<h2>環境判定（SimWeb 伺服器紀錄，非 Agent 自報）</h2><pre>"
               f"{_e(json.dumps(environment, ensure_ascii=False, indent=2))}</pre>")
    excluded = f"（排除：{_e(json.dumps(frontier['excluded'], ensure_ascii=False))}）" if frontier else ""
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>研究時間軸 {_e(state['id'])}</title>
<style>{_CSS}</style></head><body><main>
<h1>{_e(goal.get('restated_question') or task['question'])}</h1>
<div class="meta">Episode {_e(state['id'])} · 情境 {_e(task.get('scenario'))} · 自家站 {_e(task['own_site'])} ·
競品 {_e(', '.join(task['competitors']))} · 研究期間 {_e(goal.get('window_start'))} ～ {_e(goal.get('window_end'))} ·
關鍵字 {_e(', '.join(goal.get('topic_keywords', [])))}</div>
<div class="cards">{''.join(f'<div class="card"><span>{_e(k)}</span><b>{_e(v)}</b></div>' for k, v in cards)}</div>
{_research_sections(state)}
<h2>資訊需求</h2><div class="wrap"><table><tr><th>ID</th><th>類型</th><th>優先</th><th>目標網域</th><th>問題</th><th>狀態</th><th>解答</th></tr>{needs}</table></div>
<h2>抓取預算隨步驟累積（底色＝階段）</h2>{_budget_svg(records, cap)}
<h2>範本統計（Frontier）{excluded}</h2><div class="wrap"><table><tr><th>範本</th><th>URL 數</th><th>期間內更新</th><th>已抓取</th><th>證據</th><th>證據率</th><th>封鎖</th><th>陷阱</th></tr>{tpl_rows}</table></div>
<h2>觀察（{len(state.get('observations', []))}）</h2><div class="wrap"><table><tr><th>ID</th><th>類型</th><th>內容</th></tr>{obs}</table></div>
{env}
<h2>步驟時間軸（點選展開：理由、候選與分數明細、LLM 輸出、HTTP 請求）</h2>
<div class="wrap"><table><tr><th>#</th><th>階段</th><th>時間</th><th>步驟</th><th>抓取</th></tr>{''.join(_step_row(r) for r in records)}</table></div>
</main></body></html>"""
