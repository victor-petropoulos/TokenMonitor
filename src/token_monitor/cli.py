"""TokenMonitor CLI."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from token_monitor import __version__
from token_monitor.hook_script import HOOK_SCRIPT_BODY, HOOK_SCRIPT_NAME
from token_monitor.paths import (
    DEFAULT_CONTEXT_WINDOW,
    baselines_dir,
    cursor_dir,
    data_dir,
    ensure_data_dir,
    hook_failures_path,
    hooks_json_path,
    usage_db_path,
)
from token_monitor.scanner import (
    diff_scans,
    load_scan,
    run_scan,
    save_scan,
    simulate_disable,
)
from token_monitor.store import get_history, get_last_event_age_hours, get_latest

app = typer.Typer(
    name="token-monitor",
    help="Monitor Cursor agent token usage and estimate static config overhead.",
    no_args_is_help=True,
)
console = Console()

HOOK_COMMAND = "./hooks/record-token-usage.sh"


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
) -> None:
    """Scan global Cursor config and estimate token overhead."""
    result = run_scan(encoding=encoding, workspace=workspace)
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
) -> None:
    """Show latest recorded agent turn usage (from hooks)."""
    event = get_latest()
    if not event:
        console.print(
            "[yellow]No usage events recorded.[/yellow]\n"
            "Run [bold]token-monitor install-hook[/bold], reload Cursor, then send an agent message."
        )
        raise typer.Exit(1)

    console.print("[bold]Live usage (last turn, exact)[/bold]")
    console.print(f"  Time:           {event.ts}")
    console.print(f"  Model:          {event.model or '—'}")
    console.print(f"  Input:          {_format_tokens(event.input_tokens)} tokens")
    console.print(f"  Headroom:       {_headroom_label(event.input_tokens, window)}")
    console.print(f"  Output:         {_format_tokens(event.output_tokens)} tokens")
    console.print(f"  Cache read:     {_format_tokens(event.cache_read_tokens)}")
    console.print(f"  Cache write:    {_format_tokens(event.cache_write_tokens)}")
    if event.new_tokens is not None:
        console.print(f"  New tokens:     {_format_tokens(event.new_tokens)} (input − cache_read)")
    turn_type = "cold" if event.is_cold_turn else "warm"
    console.print(f"  Turn type:      {turn_type}")
    workspace: Path | None = None
    if event.workspace_roots:
        root = event.workspace_roots[0]
        console.print(f"  Workspace:      {root}")
        candidate = Path(root)
        if candidate.is_dir():
            workspace = candidate

    static = run_scan(workspace=workspace)
    overhead = static.session_overhead_tokens()
    if event.input_tokens is not None:
        gap = event.input_tokens - overhead
        console.print(f"\n  Session overhead (est.): {_format_tokens(overhead)}")
        console.print(
            f"  Unattributed:            ~{_format_tokens(max(0, gap))} "
            "(system + conversation + dynamic)"
        )
        if gap < 0:
            console.print(
                "[dim]  Note: scan can exceed live input when disabled MCPs or "
                "on-demand skill bodies are counted separately.[/dim]"
            )


@app.command()
def history(
    last: int = typer.Option(20, "--last", "-n"),
    window: int = typer.Option(DEFAULT_CONTEXT_WINDOW, "--window"),
) -> None:
    """Show recent usage events."""
    events = get_history(limit=last)
    if not events:
        console.print("[yellow]No usage events recorded.[/yellow]")
        raise typer.Exit(1)

    table = Table(title=f"Last {len(events)} agent turns")
    table.add_column("Time")
    table.add_column("Input", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Cache read", justify="right")
    table.add_column("Turn")
    table.add_column("Headroom")

    for ev in events:
        table.add_row(
            ev.ts[:19],
            _format_tokens(ev.input_tokens),
            _format_tokens(ev.new_tokens),
            _format_tokens(ev.cache_read_tokens),
            "cold" if ev.is_cold_turn else "warm",
            _headroom_label(ev.input_tokens, window),
        )
    console.print(table)


@app.command("top-offenders")
def top_offenders(
    encoding: str = typer.Option("cl100k_base", "--encoding", "-e"),
) -> None:
    """Rank global config by estimated token cost."""
    result = run_scan(encoding=encoding)
    rows: list[tuple[str, str, int, str]] = []

    for rule in sorted(result.rules, key=lambda r: r.listed_tokens, reverse=True)[:10]:
        rows.append(("rule", Path(rule.path).name, rule.listed_tokens, rule.path))

    for skill in sorted(result.user_skills, key=lambda s: s.listed_tokens, reverse=True)[:10]:
        rows.append(("user_skill", Path(skill.path).parent.name, skill.listed_tokens, skill.path))

    for skill in sorted(result.plugin_skills, key=lambda s: s.listed_tokens, reverse=True)[:15]:
        name = Path(skill.path).parent.parent.name
        rows.append(("plugin_skill", name, skill.listed_tokens, skill.path))

    for srv in result.mcp_servers:
        if srv.enabled:
            label = srv.config_name or srv.server_id
            rows.append(("mcp", label, srv.tokens, srv.server_id))

    rows.sort(key=lambda r: r[2], reverse=True)

    table = Table(title="Top offenders (estimated tokens)")
    table.add_column("Type")
    table.add_column("Name")
    table.add_column("Est.Tokens", justify="right")
    table.add_column("Detail", overflow="fold")
    for kind, name, tokens, detail in rows[:30]:
        table.add_row(kind, name, _format_tokens(tokens), detail)
    console.print(table)

    latest = get_latest()
    if latest and latest.input_tokens is not None:
        overhead = result.session_overhead_tokens()
        gap = latest.input_tokens - overhead
        console.print(
            f"\nLive input: {_format_tokens(latest.input_tokens)} | "
            f"Session overhead (est.): {_format_tokens(overhead)} | "
            f"Unattributed: ~{_format_tokens(max(0, gap))}"
        )


@app.command()
def simulate(
    disable: str = typer.Option(..., "--disable", "-d", help="Comma-separated MCP names to exclude"),
    encoding: str = typer.Option("cl100k_base", "--encoding", "-e"),
) -> None:
    """What-if: estimate tokens if MCP servers were disabled (static only)."""
    names = [n.strip() for n in disable.split(",") if n.strip()]
    result = run_scan(encoding=encoding)
    before = result.total_listed_tokens()
    after_result = simulate_disable(result, names)
    after = after_result.total_listed_tokens()
    savings = before - after

    console.print(f"[bold]Simulate disable:[/bold] {', '.join(names)}")
    console.print(f"  Before:   {_format_tokens(before)} est. tokens")
    console.print(f"  After:    {_format_tokens(after)} est. tokens")
    console.print(f"  Savings:  {_format_tokens(savings)} est. tokens (static MCP schemas only)")
    console.print(
        "\n[dim]Note: live input_tokens may differ; run a new agent session after "
        "disabling MCPs to measure real headroom.[/dim]"
    )


@app.command()
def diff(
    baseline: Path = typer.Argument(..., help="Baseline scan JSON"),
    encoding: str = typer.Option("cl100k_base", "--encoding", "-e"),
) -> None:
    """Compare a saved scan snapshot to current global config."""
    base_path = baseline if baseline.is_absolute() else baselines_dir() / baseline
    if not base_path.is_file():
        console.print(f"[red]Baseline not found: {base_path}[/red]")
        raise typer.Exit(1)

    old = load_scan(base_path)
    current = run_scan(encoding=encoding)
    deltas = diff_scans(old, current)

    table = Table(title=f"Diff vs {base_path.name}")
    table.add_column("Category")
    table.add_column("Baseline", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Savings", justify="right")

    old_cats = {c.name: c.total_tokens for c in old.categories()}
    cur_cats = {c.name: c.total_tokens for c in current.categories()}
    for name in sorted(deltas):
        table.add_row(
            name,
            _format_tokens(old_cats.get(name, 0)),
            _format_tokens(cur_cats.get(name, 0)),
            _format_tokens(deltas[name]),
        )

    old_total = old.total_listed_tokens()
    cur_total = current.total_listed_tokens()
    table.add_row("", "", "", "")
    table.add_row(
        "TOTAL",
        _format_tokens(old_total),
        _format_tokens(cur_total),
        _format_tokens(old_total - cur_total),
        style="bold",
    )
    console.print(table)


@app.command()
def doctor() -> None:
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

    for label, ok, detail in checks:
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"{icon} {label}: {detail}")

    if not all(ok for _, ok, _ in checks):
        raise typer.Exit(1)


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
        console.print(f"token-monitor {__version__}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
