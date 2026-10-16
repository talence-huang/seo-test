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
app.add_typer(db_app, name="db")
app.add_typer(config_app, name="config")
app.add_typer(llm_app, name="llm")

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


if __name__ == "__main__":
    app()
