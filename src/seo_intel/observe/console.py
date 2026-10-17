"""即時主控台：每一步印出決策、理由與預算。trace show 也用同一套渲染重播。"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from seo_intel.core.models import EpisodeState
from seo_intel.observe.record import StepRecord

_COLOR = {
    "DEFINE_GOAL": "magenta", "PLAN_INFO_NEEDS": "magenta", "SELECT_ACTION": "cyan", "EXECUTE_TOOL": "blue",
    "PARSE_RESULT": "green", "DECIDE_NEXT": "yellow", "REPORT": "magenta", "HUMAN_DECISION": "bright_red",
}


def _bar(used: int, cap: int, phase_cap: int, width: int = 24) -> str:
    filled = min(width, round(width * used / cap)) if cap else 0
    mark = min(width - 1, round(width * phase_cap / cap)) if cap else 0
    cells = ["█" if i < filled else "░" for i in range(width)]
    if filled <= mark < width:
        cells[mark] = "│"
    return "".join(cells)


def budget_line(b: dict) -> str:
    minutes, seconds = divmod(int(b.get("elapsed_s", 0)), 60)
    return (f"預算：抓取 {_bar(b['fetches'], b['fetch_cap'], b['phase_cap'])} {b['fetches']}/{b['fetch_cap']}"
            f"（階段上限 {b['phase_cap']}）· API {b['api_calls']}/{b['api_cap']} · LLM ${b['llm_usd']:.3f}/${b['llm_cap']:.2f}"
            f" · 行動 {b['actions']} · 環境時間 {minutes}m{seconds:02d}s · 階段 {b['phase']}")


def llm_line(rec: dict) -> str:
    if rec.get("error") and not rec.get("model"):
        return f"LLM 錯誤：{rec['error']}"
    u = rec.get("usage", {})
    fallback = " · fallback" if rec.get("served_by_fallback") else ""
    err = f" · 錯誤 {rec['error']}" if rec.get("error") else ""
    return (f"LLM {rec.get('role')} {rec.get('model')} · 輸入 {u.get('input_tokens', 0):,}"
            f"（快取讀 {u.get('cache_read_tokens', 0):,}）/ 輸出 {u.get('output_tokens', 0):,} token"
            f" · US${rec.get('usd', 0):.4f} · {rec.get('latency_ms', 0)}ms{fallback}{err}")


class ConsoleObserver:
    def __init__(self, console: Console | None = None, verbose: bool = False) -> None:
        self.console = console or Console()
        self.verbose = verbose

    def render(self, r: StepRecord) -> None:
        quiet = r.step == "DECIDE_NEXT" and r.decision.get("decision") == "continue" and not r.details
        if quiet and not self.verbose:
            return
        color = _COLOR.get(r.step, "white")
        self.console.print(f"[dim][{r.seq:>4}][/] [bold {color}]{r.step:<15}[/] {escape(r.summary)}")
        for d in r.details:
            self.console.print(f"       {escape(d)}", highlight=False)
        if r.lessons_applied and r.step not in ("DEFINE_GOAL", "REFLECT"):
            self.console.print(f"       [cyan]套用經驗 {escape('、'.join(r.lessons_applied))}[/]", highlight=False)
        for rec in r.llm:
            self.console.print(f"       [magenta]{escape(llm_line(rec))}[/]", highlight=False)
        if r.step == "SELECT_ACTION" and r.budget:
            self.console.print(f"       [dim]{escape(budget_line(r.budget))}[/]", highlight=False)

    def emit(self, record: StepRecord) -> None:
        self.render(record)

    def finish(self, state: EpisodeState) -> None:
        pass
