"""seo-intel 指令列介面。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from seo_intel import __version__
from seo_intel.config import PROJECT_ROOT, get_settings
from seo_intel.env import ENV_FILE, has_anthropic_credentials, load_env_file
from seo_intel.store import SCHEMA_VERSION, Database

DEFAULT_DB = PROJECT_ROOT / "data" / "intel.db"

app = typer.Typer(help="可觀察的自主 SEO 競爭情報研究 Agent", no_args_is_help=True)
db_app = typer.Typer(help="資料庫", no_args_is_help=True)
config_app = typer.Typer(help="設定", no_args_is_help=True)
llm_app = typer.Typer(help="LLM 連線", no_args_is_help=True)
simweb_app = typer.Typer(help="SimWeb 研究環境", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(config_app, name="config")
app.add_typer(llm_app, name="llm")
app.add_typer(simweb_app, name="simweb")

console = Console()
DbPath = Annotated[Path, typer.Option("--path", help="SQLite 檔案路徑")]


@app.callback()
def _startup() -> None:
    load_env_file()


@app.command()
def version() -> None:
    """顯示版本。"""
    console.print(f"seo-intel {__version__}（schema v{SCHEMA_VERSION}）")


@db_app.command("init")
def db_init(path: DbPath = DEFAULT_DB) -> None:
    """建立或升級資料庫。"""
    db = Database(path)
    console.print(f"[green]✓[/] 資料庫就緒：{path}（{len(db.tables())} 張資料表，schema v{SCHEMA_VERSION}）")
    db.close()


@db_app.command("info")
def db_info(path: DbPath = DEFAULT_DB) -> None:
    """列出資料表與筆數。"""
    if not path.exists():
        console.print(f"[red]找不到資料庫：{path}[/]，請先執行 seo-intel db init")
        raise typer.Exit(1)
    db = Database(path)
    table = Table("資料表", "筆數")
    for name in db.tables():
        table.add_row(name, str(db.query(f"SELECT COUNT(*) AS n FROM {name}")[0]["n"]))
    console.print(table)
    db.close()


@config_app.command("show")
def config_show() -> None:
    """顯示目前生效的設定。"""
    s = get_settings()
    roles = Table("角色", "模型", "effort", "max_tokens", "fallback", title="LLM 角色")
    for name, r in s.models.roles.items():
        roles.add_row(name, r.model, r.effort, str(r.max_tokens), "是" if r.fallback else "否")
    console.print(roles)

    prices = Table("模型", "輸入", "輸出", "快取讀", "快取寫", title="價格（US$ / 百萬 token）")
    for name, p in s.models.pricing.items():
        prices.add_row(name, f"{p.input:.2f}", f"{p.output:.2f}", f"{p.cache_read:.3f}", f"{p.cache_write:.3f}")
    console.print(prices)

    b = s.budget
    console.print(
        f"[bold]預算[/]：抓取 {b.max_fetches} 頁（每網域 {b.max_fetches_per_domain}）· "
        f"LLM US${b.max_llm_usd:.2f} · {b.max_wall_time_s}s · {b.max_steps} 步"
    )
    ph = b.phases
    console.print(
        f"[bold]階段預留[/]：discovery {ph.discovery:.0%} · sampling {ph.sampling:.0%} · "
        f"targeted {ph.targeted:.0%} · verification {ph.verification:.0%}"
    )
    console.print(
        f"[bold]自治邊界[/]：需核准 {len(s.policy.require_approval)} 項 · 禁止 {len(s.policy.forbidden)} 項"
    )
    if has_anthropic_credentials():
        console.print("[bold]Anthropic 憑證[/]：[green]已設定[/]")
    else:
        console.print(f"[bold]Anthropic 憑證[/]：[yellow]未設定[/]（請在 {ENV_FILE} 填入 ANTHROPIC_API_KEY）")


class Ping(BaseModel):
    ok: bool
    echo: str


@llm_app.command("ping")
def llm_ping(role: Annotated[str, typer.Option(help="使用哪個角色的設定")] = "extractor") -> None:
    """做一次真實 API 呼叫，確認連線、結構化輸出與成本計量。"""
    from seo_intel.llm import AnthropicLLM, LLMError

    settings = get_settings()
    llm = AnthropicLLM(settings.models)
    try:
        result = llm.call(
            role,
            system="You are a connectivity check for a research agent. Answer only in the requested JSON format.",
            user="Set ok to true and echo the single word: pong",
            schema=Ping,
        )
    except LLMError as e:
        console.print(f"[red]✗ 呼叫失敗[/]：{type(e).__name__}：{e}")
        raise typer.Exit(1) from e

    m = result.meta
    t = Table("欄位", "值", title=f"LLM ping（角色：{role}）")
    t.add_row("結構化輸出", result.parsed.model_dump_json())
    t.add_row("實際模型", m.model)
    t.add_row("輸入 / 輸出 token", f"{m.usage.input_tokens} / {m.usage.output_tokens}")
    t.add_row("快取讀 / 寫 token", f"{m.usage.cache_read_tokens} / {m.usage.cache_write_tokens}")
    t.add_row("成本", f"US${m.usd:.6f}")
    t.add_row("stop_reason", str(m.stop_reason))
    t.add_row("由 fallback 回應", "是" if m.served_by_fallback else "否")
    t.add_row("延遲", f"{m.latency_ms} ms")
    t.add_row("request_id", str(m.request_id))
    console.print(t)


ScenarioOpt = Annotated[str, typer.Option("--scenario", "-s", help="情境 ID，例如 s1")]


@simweb_app.command("list")
def simweb_list() -> None:
    """列出所有情境。"""
    from simweb import build, list_scenarios

    t = Table("情境", "集合", "網站類型", "目標", "網站與規模（sitemap URL 數）", title="SimWeb 情境")
    for spec in list_scenarios():
        world = build(spec.id).world
        sizes = " · ".join(f"{d} {world.sitemap_size(d):,}" for d in world.sites)
        t.add_row(spec.id, spec.split, spec.archetype, spec.goal.type, sizes)
    console.print(t)


@simweb_app.command("inspect")
def simweb_inspect(scenario: ScenarioOpt) -> None:
    """檢視情境：網站、植入事件、標準答案，以及植入效應是否可在排名資料中觀察到。"""
    from datetime import timedelta
    from statistics import mean

    from simweb import build

    sim = build(scenario)
    spec, w, truth = sim.spec, sim.world, sim.truth
    g = spec.goal
    console.print(f"[bold]情境 {spec.id}[/]（{spec.split}，{spec.archetype}，種子 {spec.seed}，現在 {spec.now}）")
    console.print(f"[bold]研究問題[/]：{g.question}")
    console.print(f"自家站 {g.own_site} · 競品 {', '.join(g.competitors)} · 主題 {g.cluster} · 期間 {g.window[0]}～{g.window[1]}")

    sites = Table("網站", "角色", "sitemap URL", "crawl-delay", "WAF 路徑", "不穩定率", "損壞 HTML", "注入頁", title="網站與反爬設定")
    for d, s in w.sites.items():
        inj = w.injection_page(d)
        sites.add_row(d, s.spec.role, f"{w.sitemap_size(d):,}", f"{s.spec.crawl_delay:g}s",
                      ", ".join(s.spec.waf_prefixes) or "—", f"{s.spec.flaky_rate:.0%}", f"{s.spec.malformed_rate:.0%}",
                      inj.path if inj else "—")
    console.print(sites)

    tpl = Table("網站", "範本", "種類", "頁數", title="範本分布")
    for d, s in w.sites.items():
        for t in s.templates.values():
            tpl.add_row(d, t.prefix, t.kind, f"{t.count:,}")
    console.print(tpl)

    caused = {fx.cause: fx for fx in spec.effects}
    ev_t = Table("事件", "網站", "範本", "類型", "主題", "頁數", "日期", "變動", "角色", title="植入事件")
    for e in spec.events:
        t = w.sites[e.site].templates[e.template]
        role = f"[green]因果（{caused[e.id].id}）[/]" if e.id in caused else "[yellow]干擾項[/]"
        ev_t.add_row(e.id, e.site, t.prefix, e.kind, e.cluster or "—", str(len(w.event_pages[e.id])),
                     f"{e.date}～{e.date + timedelta(days=e.spread_days)}", ", ".join(e.change_types(t)), role)
    console.print(ev_t)
    for cf in spec.confounders:
        console.print(f"[bold]混淆因子[/] {cf.id}：{cf.date} 起自家站全站排名變差 {cf.own_shift:g} 名")

    ans = Table("類型", "網站", "範本", "主題", "變動類型", "時間窗", "方向", title="標準答案（由事件自動推導）")
    for c in truth.key_claims:
        ans.add_row(c.claim_type, c.subject_site, c.template_prefix, c.cluster, ", ".join(c.change_types),
                    f"{c.date_window[0]}～{c.date_window[1]}", c.direction)
    console.print(ans)
    if truth.distractors:
        console.print("[bold]不應被當成原因[/]：" + "；".join(f"{x.subject_site}{x.template_prefix}" for x in truth.distractors))

    rank = sim.server.rank
    first = min(spec.event(c.event_id).date for c in truth.key_claims)
    before = (first - timedelta(days=35), first - timedelta(days=5))
    after = (spec.now - timedelta(days=16), rank.latest_date)
    control = next(c for c in w.archetype.clusters if c != g.cluster)

    def avg(site: str, cluster: str, span) -> float:
        qs = [q for q, c in rank.queries() if c == cluster]
        days = [span[0] + timedelta(days=i) for i in range((span[1] - span[0]).days + 1)]
        return mean(rank.position(site, q, d) for q in qs for d in days)

    obs = Table("網站", "主題", f"事件前 {before[0]}～{before[1]}", f"近期 {after[0]}～{after[1]}", "變化（正值＝變差）",
                title="植入效應可觀察性檢查（平均排名）")
    focus = truth.key_claims[0].subject_site
    for site, cluster, label in [(g.own_site, g.cluster, "目標主題"), (g.own_site, control, "對照主題"),
                                 (focus, g.cluster, "目標主題")]:
        b, a = avg(site, cluster, before), avg(site, cluster, after)
        obs.add_row(site, f"{label}：{cluster}", f"{b:.1f}", f"{a:.1f}", f"{a - b:+.1f}")
    console.print(obs)


@simweb_app.command("fetch")
def simweb_fetch(
    scenario: ScenarioOpt,
    url: Annotated[str, typer.Argument(help="要抓取的完整網址")],
    lines: Annotated[int, typer.Option(help="顯示前幾行內容")] = 15,
) -> None:
    """在情境中抓取一個網址（手動瀏覽模擬世界用）。"""
    import httpx

    from simweb import build

    sim = build(scenario)
    try:
        r = sim.client().get(url)
    except httpx.HTTPError as e:
        console.print(f"[red]{type(e).__name__}[/]：{e}")
        raise typer.Exit(1) from e
    console.print(f"[bold]{r.status_code}[/] {url}（{r.headers.get('content-type')}，{len(r.content):,} bytes）")
    text = r.text.replace("><", ">\n<")
    console.print("\n".join(text.splitlines()[:lines]), markup=False, highlight=False)
    flags = sim.server.log.records[-1].flags if sim.server.log.records else ()
    if flags:
        console.print(f"[yellow]環境紀錄的旗標[/]：{', '.join(flags)}")


if __name__ == "__main__":
    app()
