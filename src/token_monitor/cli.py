"""TokenMonitor CLI."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import BarColumn, Progress

from token_monitor import app_name, app_version
from token_monitor.hook_script import HOOK_SCRIPT_BODY, HOOK_SCRIPT_NAME
from token_monitor.paths import (
    DEFAULT_CONTEXT_WINDOW,
    baselines_dir(),
    cursor_dir(),
    data_dir(),
    ensure_data_dir(),
    hook_failures_path(),
    hooks_json_path(),
    usage_db_path(),
)
from token_monitor.scanner import (
    diff_scans,
    load_scan,
    run_scan,
    save_scan,
    simulate_disable,
)
from token_monitor.store import get_history, get_last_event_age_hours, get_latest

def _output_json(data: dict | list) -> None:
    """Print data as JSON to stdout."""
    console.print(json.dumps(data, indent=2, default=str))


app = typer.Typer(
    name="token-monitor",
    help="Monitor Cursor agent token usage and estimate static config overhead.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

HOOK_COMMAND = "./hooks/record-token-usage.sh"

JSON_FLAG = typer.Option(False, "--json", "-j", help="Output results as JSON")


def _format_tokens(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def _headroom_label(input_tokens: int | None, window: int) -> str:
    if input_tokens is None:
        return "—"
    pct = (input_tokens / window) * 100
    remaining = window - input_tokens
    if remaining >= 0:
        return f"{remaining:,} remaining ({100 - pct:.1f}% free)"
    return f"OVER LIMIT ({pct:.0f}% of {window // 1000}K window)"


def _print_scan_table(result, *, show_mcp: bool = True) -> None:
    table = Table(title=f"Static config estimate ({result.encoding})")
    table.add_column("Category", style="cyan")
    table.add_column("Items", justify="right")
    table.add_column("Listed", justify="right")
    table.add_column("Body", justify="right")
    table.add_column("Total", justify="right", style="bold")

    total = 0
    for cat in result.categories():
        if cat.name == "mcp":
            listed = cat.listed_tokens
            body = 0
        else:
            listed = cat.listed_tokens
            body = cat.body_tokens
        row_total = listed + body
        total += row_total
        table.add_row(
            cat.name,
            str(cat.item_count),
            _format_tokens(listed),
            _format_tokens(body) if body else "—",
            _format_tokens(row_total),
        )
    table.add_row("", "", "", "", "")
    table.add_row("TOTAL", "", "", "", _format_tokens(total), style="bold green")
    console.print(table)

    if show_mcp and result.mcp_servers:
        mcp_table = Table(title="MCP servers (by estimated tokens)")
        mcp_table.add_column("Server")
        mcp_table.add_column("Config")
        mcp_table.add_column("Enabled")
        mcp_table.add_column("Tools", justify="right")
        mcp_table.add_column("Est.Tokens", justify="right")
        for srv in result.mcp_servers[:25]:
            mcp_table.add_row(
                srv.server_id,
                srv.config_name or "—",
                "yes" if srv.enabled else "no",
                str(srv.tool_count),
                _format_tokens(srv.tokens),
            )
        if len(result.mcp_servers) > 25:
            mcp_table.caption = f"… and {len(result.mcp_servers) - 25} more"
        console.print(mcp_table)


@app.command()
def scan(
    encoding: str = typer.Option("cl100k_base", "--encoding", "-e"),
    workspace: Optional[Path] = typer.Option(
        None, "--workspace", "-w", help="Narrow MCP scan to one workspace (debug)"
    ),
    save: Optional[Path] = typer.Option(
        None, "--save", "-s", help="Save snapshot JSON for diff"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing snapshot"),
    json_output: bool = False,
) -> None:
    """Scan global Cursor config and estimate token overhead."""
    result = run_scan(encoding=encoding, workspace=workspace)

    if json_output:
        _output_json(result.to_dict())
        return

    _print_scan_table(result)

    if save:
        out = save
        if not out.is_absolute():
            out = baselines_dir() / out
        try:
            save_scan(result, out, force=force)
            console.print(f"[green]Saved snapshot → {out}[/green]")
        except FileExistsError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc


@app.command()
def report(
    window: int = typer.Option(DEFAULT_CONTEXT_WINDOW, "--window", help="Context window size"),
    json_output: bool = False,
) -> None:
    """Show latest recorded agent turn usage (from hooks)."""
    event = get_latest()
    if not event:
        console.print(
            "[yellow]No usage events recorded.[/yellow]\n"
            "Run [bold]token-monitor install-hook[/bold], reload Cursor, then send an agent message."
        )
        return

    table = Table(title=f"Latest Turn (Model: {event.model or 'Unknown'})")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Input Tokens", _format_tokens(event.input_tokens))
    table.add_row("Output Tokens", _format_tokens(event.output_tokens))
    if event.cache_read_tokens:
        table.add_row("Cache Read", _format_tokens(event.cache_read_tokens))
    if event.cache_write_tokens:
        table.add_row("Cache Write", _format_tokens(event.cache_write_tokens))
    
    console.print(table)
    console.print(f"\n[bold]{_headroom_label(event.input_tokens, window)}[/bold]")

    if json_output:
        _output_json({
            "event": event.__dict__,
            "headroom": _headroom_label(event.input_tokens, window)
        })


@app.command()
def visualize(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of turns to visualize"),
    json_output: bool = False,
) -> None:
    """Show usage trends with ASCII bar charts."""
    history = get_history(limit=limit)
    if not history:
        console.print("[yellow]Not enough history to visualize. Need at least 2 turns.[/yellow]")
        return

    if json_output:
        _output_json([e.__dict__ for e in history])
        return

    # Find max tokens for scaling
    max_input = max(e.input_tokens or 0 for e in history)
    max_output = max(e.output_tokens or 0 for e in history)
    max_val = max(max_input, max_output)
    
    if max_val == 0:
        console.print("[yellow]Zero token usage detected in history.[/yellow]")
        return

    console.print(Panel(f"[bold]Token Usage Trends (Last {limit} turns)[/bold]", expand=False))

    for event in reversed(history):
        ts = event.ts[:16] # YYYY-MM-DDTHH:MM
        
        # Input bar
        in_val = event.input_tokens or 0
        in_bar = "█" * int((in_val / max_val) * 20) if max_val > 0 else ""
        
        # Output bar
        out_val = event.output_tokens or 0
        out_bar = "░" * int((out_val / max_val) * 20) if max_val > 0 else ""

        console.print(f"[dim]{ts}[/dim] | [cyan]{in_bar}[/cyan] {_format_tokens(in_val)} | [magenta]{out_bar}[/magenta] {_format_tokens(out_val)}")


@app.command()
def doctor(json_output: bool = False) -> None:
    """Check hook install, Python path, and database health."""
    checks: list[tuple[str, bool, str]] = []

    hook_path = cursor_dir() / "hooks" / HOOK_SCRIPT_NAME
    checks.append(("Hook script exists", hook_path.is_file(), str(hook_path)))

    hooks_json = hooks_json_path()
    hook_registered = False
    if hooks_json.is_file():
        try:
            data = json.loads(hooks_json.read_text(encoding="utf-8"))
            hooks = data.get("hooks") or {}
            for event in ("stop", "afterAgentResponse"):
                entries = hooks.get(event) or []
                hook_registered = hook_registered or any(
                    HOOK_SCRIPT_NAME in (e.get("command") or "") for e in entries
                )
        except json.JSONDecodeError:
            pass
    checks.append(("Hook registered in hooks.json", hook_registered, str(hooks_json)))

    ensure_data_dir()
    db_ok = usage_db_path().parent.is_dir()
    checks.append(("Data directory writable", db_ok, str(data_dir())))

    age = get_last_event_age_hours()
    if age is None:
        checks.append(("Recent usage event", False, "none recorded"))
    else:
        checks.append(("Recent usage event", age < 168, f"{age:.1f}h ago"))

    py = shutil.which("python3") or sys.executable
    checks.append(("Python available", bool(py), py or "—"))

    failures = hook_failures_path()
    if failures.is_file():
        lines = failures.read_text(encoding="utf-8").strip().splitlines()
        checks.append(("Hook failures", len(lines) == 0, f"{len(lines)} in log"))

    if json_output:
        _output_json({
            "checks": [
                {"label": label, "ok": ok, "detail": detail}
                for label, ok, detail in checks
            ],
            "all_passed": all(ok for ok, _, _ in checks),
        })
        if not all(ok for ok, _, _ in checks):
            raise typer.Exit(1)
        return

    for label, ok, detail in checks:
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"{icon} {label}: {detail}")

    if not all(ok for ok, _, _ in checks):
        raise typer.Exit(true)


@app.command("install-hook")
def install_hook() -> None:
    """Install record-token-usage hook into ~/.cursor/hooks.json."""
    dest = cursor_dir() / "hooks" / HOOK_SCRIPT_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(HOOK_SCRIPT_BODY, encoding="utf-8")
    dest.chmod(0o755)

    env_file = cursor_dir() / "token-monitor" / "hook.env"
    ensure_data_dir()
    python_bin = sys.executable
    env_file.write_text(
        f"TOKEN_MONITOR_PYTHON={python_bin}\n",
        encoding="utf-8",
    )

    hooks_path = hooks_json_path()
    if hooks_path.is_file():
        backup = hooks_path.with_suffix(f".json.bak.{int(__import__('time').time())}")
        shutil.copy2(hooks_path, backup)
        console.print(f"Backed up hooks.json → {backup.name}")
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    else:
        data = {"version": 1, "hooks": {}}

    hooks = data.setdefault("hooks", {})
    entry = {"command": HOOK_COMMAND}

    def _add_unique(event: str) -> None:
        existing = hooks.setdefault(event, [])
        if not any(HOOK_SCRIPT_NAME in (e.get("command") or "") for e in existing):
            existing.append(entry)

    _add_unique("stop")

    hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    console.print(f"[green]Installed hook → {dest}[/green]")
    console.print("[bold]Reload Cursor[/bold] (Developer: Reload Window), then send an agent message.")
    console.print("Verify with: [bold]token-monitor doctor[/bold]")


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", "-V", help="Show version"),
) -> None:
    if version:
        console.print(f"{app_name()} {app_version()}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
