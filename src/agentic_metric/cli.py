"""Typer CLI for agentic-metric."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from importlib.metadata import version as _pkg_version
from pathlib import Path

import typer
from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

app = typer.Typer(
    name="agentic-metric",
    help="Monitor token usage and costs across Codex and Claude Code sessions.",
    invoke_without_command=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
pricing_app = typer.Typer(
    help="View and manage model pricing.",
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(pricing_app, name="pricing")


console = Console()

_COMPACT_TOP_SESSIONS_LIMIT = 5


# ANSI named colors — inherit the terminal's own palette / theme.
# No hard-coded hex, so output adapts to light/dark terminals equally well.
C_TEXT     = "bright_white"
C_SUBTEXT  = "bright_white"
C_MUTED    = "white"
C_RED      = "bright_red"
C_PEACH    = "bright_yellow"
C_YELLOW   = "bright_yellow"
C_GREEN    = "bright_green"
C_TEAL     = "bright_cyan"
C_SKY      = "bright_blue"
C_BLUE     = "bright_blue"
C_MAUVE    = "bright_magenta"
C_SURFACE1 = "white"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"agentic-metric {_pkg_version('agentic-metric')}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _default(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-v", callback=_version_callback,
        is_eager=True, help="Show version and exit.",
    ),
) -> None:
    """Launch TUI by default when no command is given."""
    if ctx.invoked_subcommand is None:
        _run_tui()
        raise typer.Exit()


@pricing_app.callback(invoke_without_command=True)
def _pricing_default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command()
def tui() -> None:
    """Launch the interactive TUI dashboard."""
    _run_tui()


def _run_tui() -> None:
    """Launch the interactive TUI dashboard."""
    from .tui.app import AgenticMetricApp
    AgenticMetricApp().run()


@app.command()
def sync() -> None:
    """Force sync all collectors to the database."""
    from .collectors import create_default_registry
    from .store.database import Database

    db = Database()
    registry = create_default_registry()

    console.print(f"[{C_SUBTEXT}]Syncing all collectors…[/]")
    registry.sync_all(db)
    db.close()

    console.print(f"[bold {C_GREEN}]✓ Sync complete[/]")
    for c in registry.get_all():
        console.print(f"  [{C_MUTED}]•[/] [{C_MAUVE}]{c.agent_type}[/]")


# ── report ─────────────────────────────────────────────────────────


@app.command()
def report(
    today_: bool = typer.Option(False, "--today", help="Show today's usage."),
    week: bool = typer.Option(False, "--week", help="Show this week's usage (Mon–today)."),
    month: bool = typer.Option(False, "--month", help="Show this month's usage."),
    range_: str = typer.Option(
        None, "--range",
        help="Custom date range FROM:TO, e.g. 2026-04-01:2026-04-23.",
    ),
    no_sync: bool = typer.Option(
        False, "--no-sync", help="Skip syncing collectors before querying."
    ),
    full: bool = typer.Option(
        False, "--full", help="Show the full drill-down with extra model/time tables."
    ),
    limit: int = typer.Option(
        8, "--limit", "-n", min=1, max=25,
        help="Rows to show in driver tables.",
    ),
) -> None:
    """Show a usage report for a time range."""
    from .collectors import create_default_registry
    from .store.database import Database
    from .store import aggregator

    flags = [today_, week, month, range_ is not None]
    if sum(1 for f in flags if f) > 1:
        console.print(f"[{C_RED}]Pick only one of --today / --week / --month / --range.[/]")
        raise typer.Exit(1)

    if range_:
        try:
            frm, to = range_.split(":", 1)
            frm, to = frm.strip(), to.strip()
            if len(frm) != 10 or len(to) != 10:
                raise ValueError
            label = f"{frm} → {to}"
        except ValueError:
            console.print(f"[{C_RED}]--range must look like 2026-04-01:2026-04-23.[/]")
            raise typer.Exit(1)
    else:
        if week:
            label, frm, to = aggregator.resolve_range("week")
        elif month:
            label, frm, to = aggregator.resolve_range("month")
        else:
            label, frm, to = aggregator.resolve_range("today")

    db = Database()
    if not no_sync:
        registry = create_default_registry()
        registry.sync_all(db)

    totals = aggregator.get_range_totals(db, frm, to)
    by_agent = aggregator.get_range_by_agent(db, frm, to)
    by_agent_model = aggregator.get_range_by_agent_model(db, frm, to)
    by_project = aggregator.get_range_by_project(db, frm, to, limit=10)
    by_time_model = aggregator.get_range_by_time_model(db, frm, to, limit=limit)
    top_sessions = aggregator.get_range_top_sessions(
        db,
        frm,
        to,
        limit=limit if full else min(limit, _COMPACT_TOP_SESSIONS_LIMIT),
    )

    # Periodic breakdown (hourly/daily/weekly) — only when the range
    # corresponds to a named focus.
    focus_kind = None
    if not range_:
        focus_kind = "week" if week else ("month" if month else "today")
    periodic = aggregator.get_heatmap(db, focus_kind) if focus_kind else []

    # Previous period totals for delta comparison.
    prev_totals = None
    if focus_kind:
        _, p_frm, p_to = aggregator.resolve_range(focus_kind, offset=1)
        prev_totals = aggregator.get_range_totals(db, p_frm, p_to)

    db.close()

    _print_report(
        label, frm, to, totals, by_agent, by_agent_model, by_project,
        by_time_model, top_sessions, periodic, focus_kind, prev_totals, full=full,
    )


@app.command("today")
def today_cmd(
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip syncing collectors before querying."),
    full: bool = typer.Option(False, "--full", help="Show the full drill-down with extra model/time tables."),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=25, help="Rows to show in driver tables."),
) -> None:
    """Shortcut for ``report --today``."""
    report(today_=True, week=False, month=False, range_=None, no_sync=no_sync, full=full, limit=limit)


@app.command("week")
def week_cmd(
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip syncing collectors before querying."),
    full: bool = typer.Option(False, "--full", help="Show the full drill-down with extra model/time tables."),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=25, help="Rows to show in driver tables."),
) -> None:
    """Shortcut for ``report --week``."""
    report(today_=False, week=True, month=False, range_=None, no_sync=no_sync, full=full, limit=limit)


@app.command("month")
def month_cmd(
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip syncing collectors before querying."),
    full: bool = typer.Option(False, "--full", help="Show the full drill-down with extra model/time tables."),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=25, help="Rows to show in driver tables."),
) -> None:
    """Shortcut for ``report --month``."""
    report(today_=False, week=False, month=True, range_=None, no_sync=no_sync, full=full, limit=limit)


@app.command("history")
def history_cmd(
    days: int = typer.Option(14, "--days", "-d", min=1, max=365, help="Number of days to include."),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip syncing collectors before querying."),
    full: bool = typer.Option(False, "--full", help="Show the full drill-down with extra model/time tables."),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=25, help="Rows to show in driver tables."),
) -> None:
    """Show a recent multi-day usage report."""
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    report(
        today_=False,
        week=False,
        month=False,
        range_=f"{start.strftime('%Y-%m-%d')}:{today.strftime('%Y-%m-%d')}",
        no_sync=no_sync,
        full=full,
        limit=limit,
    )


def _print_report(
    label: str, frm: str, to: str,
    totals: dict, by_agent: list[dict],
    by_agent_model: list[dict], by_project: list[dict],
    by_time_model: list[dict], top_sessions: list[dict],
    periodic: list[dict], focus_kind: str | None,
    prev_totals: dict | None = None,
    *,
    full: bool = False,
) -> None:
    tot_tokens = _sum_tokens(totals)
    tot_cost = totals.get("estimated_cost_usd") or 0.0
    tot_cost_unknown = _has_unknown_cost(totals)
    tot_sess = totals.get("session_count") or 0
    tot_turns = totals.get("user_turns") or 0
    cache_pct = _cache_hit_rate(totals)

    # ─── Header panel (label + stats + auto summary line) ───
    header_text = Text()
    header_text.append(label, style=f"bold {C_PEACH}")
    header_text.append(f"   {frm} → {to}", style=C_MUTED)

    delta_line = _delta_line(tot_cost, prev_totals, current_unknown=tot_cost_unknown)
    cost_cell = Group(
        Text("COST", style=f"{C_MUTED}"),
        Text(_fmt_cost(tot_cost, unknown=tot_cost_unknown), style=f"bold {C_YELLOW}"),
        delta_line if delta_line else Text(""),
    )
    stats = Table.grid(padding=(0, 4))
    for _ in range(5):
        stats.add_column(justify="left")
    stats.add_row(
        cost_cell,
        _stat("Sessions", f"{tot_sess:,}", C_MAUVE),
        _stat("Turns", f"{tot_turns:,}", C_SKY),
        _stat("Tokens", _fmt_tokens(tot_tokens), C_TEAL),
        _stat("Cache hit", f"{cache_pct:.0f}%" if cache_pct >= 0 else "—", C_GREEN),
    )

    summary_line = _auto_summary_line(
        focus_kind, totals, periodic, prev_totals, cache_pct,
    )
    header_children = [header_text]
    if summary_line:
        header_children.append(summary_line)
    header_children.extend([Text(""), stats])
    token_split = _token_split_line(totals)
    if token_split:
        header_children.extend([Text(""), token_split])

    header_panel = Panel(
        Group(*header_children),
        box=box.ROUNDED,
        border_style=C_SURFACE1,
        padding=(1, 2),
    )

    # ─── Heatmap strip (today/week/month scope) ───
    heatmap_renderable = None
    if periodic and focus_kind:
        heatmap_renderable = _build_heatmap_panel(periodic, focus_kind)
    drivers_renderable = _build_cost_drivers_panel(
        totals, by_time_model, top_sessions, by_project, detailed=full,
    )

    # ─── Table renderables ───
    agent_tbl = _build_by_agent_table(by_agent)
    session_tbl = _build_top_sessions_table(top_sessions, tot_cost, total_unknown=tot_cost_unknown)
    project_tbl = _build_top_projects_table(by_project)
    model_tbl = _build_by_agent_model_table(by_agent_model) if full else None
    periodic_tbl = _build_periodic_table(periodic, focus_kind) if full else None

    # ─── Render ───
    console.print()
    console.print(header_panel)
    if heatmap_renderable is not None:
        console.print(heatmap_renderable)
    if drivers_renderable is not None:
        console.print(drivers_renderable)

    try:
        term_width = console.size.width
    except Exception:
        term_width = 0

    if term_width >= 160 and agent_tbl is not None and project_tbl is not None:
        console.print(Columns([agent_tbl, project_tbl], expand=True, equal=False, padding=(0, 2)))
    else:
        if agent_tbl is not None:
            console.print(agent_tbl)
        if project_tbl is not None:
            console.print(project_tbl)

    if session_tbl is not None:
        console.print(session_tbl)

    detail_tables = [t for t in (model_tbl, periodic_tbl) if t is not None]
    if detail_tables:
        if term_width >= 160 and len(detail_tables) == 2:
            console.print(Columns(detail_tables, expand=True, equal=False, padding=(0, 2)))
        else:
            for t in detail_tables:
                console.print(t)

    console.print()


def _auto_summary_line(
    focus_kind: str | None,
    totals: dict,
    periodic: list[dict],
    prev_totals: dict | None,
    cache_pct: float,
) -> Text | None:
    """Build a one-liner under the header: peak · cache · delta."""
    parts: list[tuple[str, str]] = []

    # Peak bucket within the periodic breakdown.
    if periodic:
        known_peak = max(periodic, key=lambda b: b.get("cost") or 0)
        unknown_peak = next((b for b in periodic if _has_unknown_cost(b)), None)
        peak = known_peak if (known_peak.get("cost") or 0) > 0 else unknown_peak
        if peak is not None and ((peak.get("cost") or 0) > 0 or _has_unknown_cost(peak)):
            peak_label = peak["label"]
            if focus_kind == "today":
                peak_label = f"{peak_label}:00"
            parts.append((C_YELLOW, f"peak {peak_label} {_fmt_cost(peak.get('cost'), unknown=_has_unknown_cost(peak))}"))

    if cache_pct >= 0 and cache_pct >= 50:
        parts.append((C_GREEN, f"cache {cache_pct:.0f}%"))

    if prev_totals is not None and not _has_unknown_cost(totals) and not _has_unknown_cost(prev_totals):
        prev = prev_totals.get("estimated_cost_usd") or 0.0
        cur = totals.get("estimated_cost_usd") or 0.0
        if prev > 0 and cur > 0:
            ratio = cur / prev
            if ratio >= 10:
                parts.append((C_RED, "▲ ≫10× vs last"))
            elif ratio > 1.01:
                parts.append((C_RED, f"▲ +{(ratio - 1) * 100:.0f}% vs last"))
            elif ratio < 0.99:
                parts.append((C_GREEN, f"▼ -{(1 - ratio) * 100:.0f}% vs last"))

    if not parts:
        return None

    line = Text()
    for i, (color, text) in enumerate(parts):
        if i > 0:
            line.append("  ·  ", style=C_MUTED)
        line.append(text, style=color)
    return line


def _build_heatmap_panel(buckets: list[dict], focus_kind: str) -> Panel:
    """Render the activity heatmap as a CLI panel."""
    blocks = [" ", "·", "░", "▒", "▓", "█", "█"]
    colors = [
        "default",
        C_BLUE,
        C_GREEN,
        "bright_green",
        C_YELLOW,
        C_RED,
        "bright_red",
    ]
    levels = len(blocks)
    max_v = max((b.get("cost") or 0) for b in buckets) or 1.0

    n = len(buckets)
    if n >= 20:
        cell_w, label_every = 4, 3
    elif n >= 10:
        cell_w, label_every = 6, 1
    elif n >= 6:
        cell_w, label_every = 8, 1
    else:
        cell_w, label_every = 12, 1
    try:
        available = max(24, console.size.width - 8)
        cell_w = min(cell_w, max(2, available // max(n, 1)))
    except Exception:
        pass

    import datetime as _dt
    now = _dt.datetime.now()
    highlight = None
    if focus_kind == "today":
        highlight = now.hour
    elif focus_kind == "week":
        highlight = now.weekday()
    elif focus_kind == "month":
        highlight = n - 1

    row_blocks = Text(" ")
    row_labels = Text(" ")
    for i, b in enumerate(buckets):
        ratio = (b.get("cost") or 0) / max_v
        lvl = min(levels - 1, int(round(ratio * (levels - 1))))
        style = colors[lvl]
        if i == highlight:
            style = f"bold {style} reverse"
        row_blocks.append(blocks[lvl] * cell_w, style=style)
        if i % label_every == 0:
            row_labels.append(b["label"][:cell_w].center(cell_w), style=C_MUTED)
        else:
            row_labels.append(" " * cell_w, style="default")

    # Summary below the strip
    known_peak = max(buckets, key=lambda bb: bb.get("cost") or 0)
    unknown_peak = next((bb for bb in buckets if _has_unknown_cost(bb)), None)
    peak = known_peak if (known_peak.get("cost") or 0) > 0 else (unknown_peak or known_peak)
    peak_unknown = _has_unknown_cost(peak)
    total_cost = sum((bb.get("cost") or 0) for bb in buckets)
    total_unknown = any(_has_unknown_cost(bb) for bb in buckets)
    total_tokens = sum((bb.get("tokens") or 0) for bb in buckets)
    summary = Text(" ")
    if (peak.get("cost") or 0) > 0 or peak_unknown:
        summary.append("peak ", style=C_MUTED)
        summary.append(peak["label"], style="bold")
        summary.append(f"  {_fmt_cost(peak.get('cost'), unknown=peak_unknown)}", style=C_YELLOW)
        summary.append(f"  {_fmt_tokens(peak.get('tokens') or 0)}", style=C_TEAL)
        summary.append("    ")
    summary.append("total ", style=C_MUTED)
    summary.append(_fmt_cost(total_cost, unknown=total_unknown), style=f"bold {C_YELLOW}")
    summary.append(f"  {_fmt_tokens(total_tokens)} tokens", style=C_TEAL)

    titles = {"today": "Today by hour",
              "week":  "This week by day",
              "month": "This month by week"}
    return Panel(
        Group(row_blocks, row_labels, Text(""), summary),
        title=titles.get(focus_kind, "Heatmap"),
        title_align="left",
        box=box.ROUNDED,
        border_style=C_SURFACE1,
        padding=(0, 1),
    )


def _build_cost_drivers_panel(
    totals: dict,
    by_time_model: list[dict],
    top_sessions: list[dict],
    by_project: list[dict],
    *,
    detailed: bool = False,
) -> Panel | None:
    """Render the report explanation panel: peak buckets and expensive sessions."""
    if not by_time_model and not top_sessions and not by_project:
        return None

    total_cost = totals.get("estimated_cost_usd") or 0.0
    summary = _driver_summary_line(
        total_cost,
        by_time_model,
        top_sessions,
        by_project,
        total_unknown=_has_unknown_cost(totals),
    )

    body: list[object] = []
    if summary:
        body.append(summary)
    if detailed:
        time_table = _build_time_model_table(by_time_model, total_cost, total_unknown=_has_unknown_cost(totals))
        if time_table is not None:
            if body:
                body.append(Text(""))
            body.append(time_table)

    if not body:
        return None

    return Panel(
        Group(*body),
        title="Cost drivers",
        title_align="left",
        box=box.ROUNDED,
        border_style=C_SURFACE1,
        padding=(1, 2),
    )


def _driver_summary_line(
    total_cost: float,
    by_time_model: list[dict],
    top_sessions: list[dict],
    by_project: list[dict],
    *,
    total_unknown: bool = False,
) -> Text | None:
    total_unknown = total_unknown or any(_has_unknown_cost(r) for r in [*by_time_model, *top_sessions, *by_project])
    line = Text()
    wrote = False
    if by_time_model:
        peak = by_time_model[0]
        peak_unknown = _has_unknown_cost(peak)
        line.append("Peak bucket  ", style=C_MUTED)
        line.append(_time_bucket_label(peak), style=f"bold {C_BLUE}")
        line.append(" · ", style=C_MUTED)
        line.append(f"{peak['agent_type']} / {peak['model']}", style=C_SKY)
        line.append(f" · {_fmt_cost(peak['estimated_cost_usd'], unknown=peak_unknown)}", style=f"bold {C_YELLOW}")
        line.append(_share_suffix(peak["estimated_cost_usd"], total_cost, unknown=peak_unknown, total_unknown=total_unknown), style=C_MUTED)
        wrote = True
    if top_sessions:
        if wrote:
            line.append("\n")
        sess = top_sessions[0]
        sess_unknown = _has_unknown_cost(sess)
        line.append("Top session  ", style=C_MUTED)
        line.append(_short_session_id(sess["session_id"]), style=f"bold {C_MAUVE}")
        line.append(f" · {_fmt_cost(sess['estimated_cost_usd'], unknown=sess_unknown)}", style=f"bold {C_YELLOW}")
        line.append(_share_suffix(sess["estimated_cost_usd"], total_cost, unknown=sess_unknown, total_unknown=total_unknown), style=C_MUTED)
        wrote = True
    if by_project:
        if wrote:
            line.append("\n")
        project = by_project[0]
        project_unknown = _has_unknown_cost(project)
        line.append("Top project  ", style=C_MUTED)
        line.append(_short_path(project["project_path"], max_len=40), style=C_BLUE)
        line.append(f" · {_fmt_cost(project['estimated_cost_usd'], unknown=project_unknown)}", style=f"bold {C_YELLOW}")
        line.append(_share_suffix(project["estimated_cost_usd"], total_cost, unknown=project_unknown, total_unknown=total_unknown), style=C_MUTED)
        wrote = True
    return line if wrote else None


def _top_project_line(by_project: list[dict], total_cost: float) -> Text | None:
    if not by_project:
        return None
    parts = []
    for row in by_project[:3]:
        cost = row.get("estimated_cost_usd") or 0.0
        if cost <= 0 and not _has_unknown_cost(row):
            continue
        parts.append((row["project_path"], cost, _has_unknown_cost(row)))
    if not parts:
        return None

    line = Text()
    line.append("Projects: ", style=C_MUTED)
    total_unknown = any(unknown for _, _, unknown in parts)
    for i, (path, cost, unknown) in enumerate(parts):
        if i:
            line.append("  ·  ", style=C_MUTED)
        line.append(_short_path(path, max_len=34), style=C_BLUE)
        line.append(f" {_fmt_cost(cost, unknown=unknown)}", style=C_YELLOW)
        line.append(_share_suffix(cost, total_cost, unknown=unknown, total_unknown=total_unknown), style=C_MUTED)
    return line


def _build_time_model_table(rows: list[dict], total_cost: float, *, total_unknown: bool = False) -> Table | None:
    rows = [r for r in rows if _has_cost_signal(r)]
    if not rows:
        return None
    wide = console.size.width >= 120
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title="Peak time × model",
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    tbl.add_column("When", style=C_BLUE, no_wrap=True)
    tbl.add_column("Driver", style=C_SKY)
    if wide:
        tbl.add_column("Sessions", justify="right", style=C_TEXT)
    tbl.add_column("Input", justify="right", style=C_TEAL)
    tbl.add_column("Output", justify="right", style=C_TEAL)
    tbl.add_column("Cache", justify="right", style=C_GREEN)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}")
    if wide:
        tbl.add_column("Share", justify="right", style=C_MUTED)
    for row in rows[:8]:
        cost = row["estimated_cost_usd"] or 0.0
        unknown = _has_unknown_cost(row)
        cells = [
            _time_bucket_label(row) if wide else _time_bucket_label_short(row),
            _clip(f"{row['agent_type']}/{row['model']}", 28 if wide else 18),
        ]
        if wide:
            cells.append(f"{row['session_count']:,}")
        cells.extend([
            _fmt_tokens(row.get("input_tokens") or 0),
            _fmt_tokens(row.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(row)),
            _fmt_cost(cost, unknown=unknown),
        ])
        if wide:
            cells.append(_share_pct(cost, total_cost, unknown=unknown, total_unknown=total_unknown))
        tbl.add_row(*cells)
    return tbl


def _build_top_sessions_table(rows: list[dict], total_cost: float, *, total_unknown: bool = False) -> Table | None:
    rows = [r for r in rows if _has_cost_signal(r)]
    if not rows:
        return None
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title="Top sessions",
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    wide = console.size.width >= 120
    tbl.add_column("Session", style=C_MAUVE, no_wrap=True)
    tbl.add_column("Agent / model", style=C_SKY)
    if wide:
        tbl.add_column("Prompt / project", style=C_TEXT, overflow="fold", max_width=42)
    tbl.add_column("Input", justify="right", style=C_TEAL)
    tbl.add_column("Output", justify="right", style=C_TEAL)
    tbl.add_column("Cache", justify="right", style=C_GREEN)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}")
    tbl.add_column("Share", justify="right", style=C_MUTED)
    for row in rows[:8]:
        cost = row["estimated_cost_usd"] or 0.0
        unknown = _has_unknown_cost(row)
        models = _clip((row.get("models") or row.get("model") or "(unknown)").replace(",", ", "), 28)
        prompt = (row.get("first_prompt") or "").strip()
        prompt_or_project = prompt if prompt else _short_path(row.get("project_path") or "")
        cells = [
            _short_session_id(row["session_id"]),
            f"{row['agent_type']} / {models}",
        ]
        if wide:
            cells.append(_clip(prompt_or_project, 88))
        cells.extend([
            _fmt_tokens(row.get("input_tokens") or 0),
            _fmt_tokens(row.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(row)),
            _fmt_cost(cost, unknown=unknown),
            _share_pct(cost, total_cost, unknown=unknown, total_unknown=total_unknown),
        ])
        tbl.add_row(*cells)
    return tbl


def _build_by_agent_table(by_agent: list[dict]) -> Table | None:
    if not by_agent:
        return None
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title="By agent",
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    tbl.add_column("Agent", style=C_MAUVE)
    tbl.add_column("Sessions", justify="right", style=C_TEXT)
    tbl.add_column("Turns", justify="right", style=C_TEXT)
    tbl.add_column("Input", justify="right", style=C_TEAL)
    tbl.add_column("Output", justify="right", style=C_TEAL)
    tbl.add_column("Cache", justify="right", style=C_GREEN)
    tbl.add_column("Cache %", justify="right", style=C_GREEN)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}")
    for r in by_agent:
        cp = _cache_hit_rate(r)
        tbl.add_row(
            r["agent_type"],
            f"{r['session_count']:,}",
            f"{r['user_turns']:,}",
            _fmt_tokens(r.get("input_tokens") or 0),
            _fmt_tokens(r.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(r)),
            f"{cp:.0f}%" if cp >= 0 else "—",
            _fmt_cost(r.get("estimated_cost_usd"), unknown=_has_unknown_cost(r)),
        )
    return tbl


def _build_by_agent_model_table(rows: list[dict]) -> Table | None:
    nonzero = [r for r in rows if _has_cost_signal(r)]
    if not nonzero:
        return None
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title="By agent × model",
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    tbl.add_column("Agent", style=C_MAUVE)
    tbl.add_column("Model", style=C_SKY)
    tbl.add_column("Sessions", justify="right", style=C_TEXT)
    tbl.add_column("Input", justify="right", style=C_TEAL)
    tbl.add_column("Output", justify="right", style=C_TEAL)
    tbl.add_column("Cache", justify="right", style=C_GREEN)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}")
    current_agent = None
    for r in nonzero:
        shown_agent = r["agent_type"] if r["agent_type"] != current_agent else ""
        current_agent = r["agent_type"]
        tbl.add_row(
            shown_agent,
            r["model"],
            f"{r['session_count']:,}",
            _fmt_tokens(r.get("input_tokens") or 0),
            _fmt_tokens(r.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(r)),
            _fmt_cost(r.get("estimated_cost_usd"), unknown=_has_unknown_cost(r)),
        )
    return tbl


def _build_top_projects_table(rows: list[dict]) -> Table | None:
    nonzero = [r for r in rows if _has_cost_signal(r)]
    if not nonzero:
        return None
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title="Top projects",
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    tbl.add_column("Project", style=C_BLUE, overflow="fold", max_width=48)
    tbl.add_column("Sessions", justify="right", style=C_TEXT)
    tbl.add_column("Input", justify="right", style=C_TEAL)
    tbl.add_column("Output", justify="right", style=C_TEAL)
    tbl.add_column("Cache", justify="right", style=C_GREEN)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}")
    for r in nonzero:
        path = _shorten_home(r["project_path"] or "(unspecified)")
        tbl.add_row(
            path,
            f"{r['session_count']:,}",
            _fmt_tokens(r.get("input_tokens") or 0),
            _fmt_tokens(r.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(r)),
            _fmt_cost(r.get("estimated_cost_usd"), unknown=_has_unknown_cost(r)),
        )
    return tbl


def _build_periodic_table(periodic: list[dict], focus_kind: str | None) -> Table | None:
    if not periodic:
        return None
    nonzero = [b for b in periodic if _has_cost_signal(b, cost_key="cost")]
    if not nonzero:
        return None
    if focus_kind == "today":
        periodic_title, bucket_col = "By hour", "Hour"
    elif focus_kind == "week":
        periodic_title, bucket_col = "By day", "Day"
    else:
        periodic_title, bucket_col = "By week", "Week"

    max_cost = max((b.get("cost") or 0) for b in nonzero) or 1e-9
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title=periodic_title,
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    tbl.add_column(bucket_col, style=C_BLUE)
    tbl.add_column("Sessions", justify="right", style=C_TEXT)
    tbl.add_column("Tokens", justify="right", style=C_TEAL)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}")
    tbl.add_column("", justify="left", no_wrap=True)
    for b in nonzero:
        cost = b["cost"] or 0.0
        unknown = _has_unknown_cost(b)
        ratio = cost / max_cost
        bar_width = 14
        fill = int(round(ratio * bar_width))
        bar = Text()
        bar.append("█" * fill, style=C_PEACH)
        bar.append("░" * (bar_width - fill), style=C_SURFACE1)
        label_col = b["label"]
        if b.get("sublabel"):
            label_col = f"{label_col}  [{C_MUTED}]{b['sublabel']}[/{C_MUTED}]"
        tbl.add_row(
            label_col,
            f"{b['session_count']:,}",
            _fmt_tokens(b.get("tokens") or 0),
            _fmt_cost(cost, unknown=unknown),
            bar,
        )
    return tbl


def _stat(label: str, value: str, color: str, big: bool = False) -> Group:
    label_text = Text(label.upper(), style=f"{C_MUTED}")
    value_style = f"bold {color}"
    if big:
        value_style = f"bold {color}"
    return Group(label_text, Text(value, style=value_style))


# ── helpers ────────────────────────────────────────────────────────


def _token_split_line(totals: dict) -> Text | None:
    if not totals:
        return None
    line = Text()
    line.append("Token split", style=C_MUTED)
    line.append("   input ", style=C_MUTED)
    line.append(_fmt_tokens(totals.get("input_tokens") or 0), style=C_TEAL)
    line.append("  ·  output ", style=C_MUTED)
    line.append(_fmt_tokens(totals.get("output_tokens") or 0), style=C_TEAL)
    line.append("  ·  cache read ", style=C_MUTED)
    line.append(_fmt_tokens(totals.get("cache_read_tokens") or 0), style=C_GREEN)
    cache_write = totals.get("cache_creation_tokens") or 0
    if cache_write:
        line.append("  ·  cache write ", style=C_MUTED)
        line.append(_fmt_tokens(cache_write), style=C_GREEN)
    return line


def _time_bucket_label(row: dict) -> str:
    date_s = row.get("usage_date") or ""
    hour = int(row.get("usage_hour") or 0)
    return f"{date_s} {hour:02d}:00" if date_s else f"{hour:02d}:00"


def _time_bucket_label_short(row: dict) -> str:
    date_s = row.get("usage_date") or ""
    hour = int(row.get("usage_hour") or 0)
    if len(date_s) == 10:
        date_s = date_s[5:]
    return f"{date_s} {hour:02d}" if date_s else f"{hour:02d}"


def _has_unknown_cost(row: dict | None) -> bool:
    return bool(row and (row.get("unknown_cost_count") or 0) > 0)


def _has_cost_signal(row: dict, *, cost_key: str = "estimated_cost_usd") -> bool:
    return (row.get(cost_key) or 0) > 0 or _has_unknown_cost(row)


def _fmt_cost(cost: float | None, *, unknown: bool = False) -> str:
    if unknown or cost is None:
        return "?"
    if cost >= 1.0:
        return f"${cost:,.2f}"
    return f"${cost:.3f}"


def _share_pct(
    cost: float,
    total_cost: float,
    *,
    unknown: bool = False,
    total_unknown: bool = False,
) -> str:
    if unknown or total_unknown or total_cost <= 0:
        return "—"
    return f"{(100.0 * cost / total_cost):.1f}%"


def _share_suffix(
    cost: float,
    total_cost: float,
    *,
    unknown: bool = False,
    total_unknown: bool = False,
) -> str:
    pct = _share_pct(cost, total_cost, unknown=unknown, total_unknown=total_unknown)
    return "" if pct == "—" else f" ({pct})"


def _clip(value: str, max_len: int) -> str:
    value = value or ""
    if len(value) <= max_len:
        return value
    return value[: max(0, max_len - 1)] + "…"


def _short_session_id(session_id: str) -> str:
    if not session_id:
        return "(unknown)"
    if ":" in session_id:
        head, tail = session_id.split(":", 1)
        return f"{head[:8]}:{tail[:10]}"
    return session_id[:8]


def _short_path(path: str, max_len: int = 42) -> str:
    if not path:
        return "(unspecified)"
    path = _shorten_home(path)
    return _clip(path, max_len)


def _shorten_home(path: str) -> str:
    if not path:
        return path
    try:
        home = os.path.normpath(str(Path.home()))
        candidate = os.path.normpath(os.path.expanduser(path))
        home_key = os.path.normcase(home)
        candidate_key = os.path.normcase(candidate)
        if os.path.commonpath([home_key, candidate_key]) == home_key:
            rel = os.path.relpath(candidate, home)
            return "~" if rel == "." else str(Path("~") / rel)
    except (OSError, ValueError):
        pass
    return path


def _fmt_tokens(n: int) -> str:
    """Compact token count: 1234 -> 1.2K, 1234567 -> 1.2M, 1.2B."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _sum_tokens(r: dict) -> int:
    return (
        (r.get("input_tokens") or 0)
        + (r.get("output_tokens") or 0)
        + (r.get("cache_read_tokens") or 0)
        + (r.get("cache_creation_tokens") or 0)
    )


def _cache_tokens(r: dict) -> int:
    return (r.get("cache_read_tokens") or 0) + (r.get("cache_creation_tokens") or 0)


def _cache_hit_rate(r: dict) -> float:
    """Return cache-read share of prompt-side tokens in %, or -1 if N/A."""
    cache_read = r.get("cache_read_tokens") or 0
    input_tok = r.get("input_tokens") or 0
    cache_create = r.get("cache_creation_tokens") or 0
    prompt_side = cache_read + input_tok + cache_create
    if prompt_side <= 0:
        return -1.0
    return 100.0 * cache_read / prompt_side


def _delta_line(
    current: float,
    prev_totals: dict | None,
    *,
    current_unknown: bool = False,
) -> Text | None:
    """Build a colored '▲ +23% vs $X' line, or None if no comparison."""
    if prev_totals is None:
        return None
    if current_unknown or _has_unknown_cost(prev_totals):
        return None
    prev = prev_totals.get("estimated_cost_usd") or 0.0
    line = Text()
    if prev <= 0 and current <= 0:
        return None
    if prev <= 0:
        line.append("▲ new", style=C_PEACH)
        return line
    ratio = current / prev
    if abs(current - prev) < 0.01 or abs(ratio - 1.0) < 0.01:
        line.append("≈ same as last", style=C_MUTED)
        return line
    if current > prev:
        # Anything above 10x is shown as ≫10× rather than a huge number
        if ratio >= 10:
            line.append("▲ ≫10× ", style=C_RED)
        else:
            pct = (ratio - 1) * 100
            line.append(f"▲ +{pct:.0f}% ", style=C_RED)
    else:
        pct = (1 - ratio) * 100
        line.append(f"▼ -{pct:.0f}% ", style=C_GREEN)
    line.append(f"vs ${prev:,.2f}", style=C_MUTED)
    return line


# ── pricing subcommands ────────────────────────────────────────────


@pricing_app.command("list")
def pricing_list() -> None:
    """List all model pricing (builtin + user overrides)."""
    from .pricing import _BUILTIN_PRICING, _load_user_pricing

    user = _load_user_pricing()

    table = Table(
        title="Model Pricing (USD per 1M tokens)",
        title_style=f"bold {C_TEXT}",
        box=box.SIMPLE_HEAVY,
        border_style=C_SURFACE1,
        header_style=f"bold {C_SUBTEXT}",
        pad_edge=False,
    )
    table.add_column("Model", style=C_MAUVE)
    table.add_column("Input", justify="right", style=C_TEAL)
    table.add_column("Output", justify="right", style=C_TEAL)
    table.add_column("Cache Read", justify="right", style=C_SKY)
    table.add_column("Cache Write", justify="right", style=C_SKY)
    table.add_column("Source", style=C_MUTED)

    all_models = dict(_BUILTIN_PRICING)
    all_models.update(user)

    for model in sorted(all_models):
        p = all_models[model]
        if model in user and model in _BUILTIN_PRICING:
            # Don't scream "override" if the value equals builtin
            if tuple(p) == tuple(_BUILTIN_PRICING[model]):
                source = Text("builtin", style=C_MUTED)
            else:
                source = Text("override", style=C_PEACH)
        elif model in user:
            source = Text("custom", style=C_GREEN)
        else:
            source = Text("builtin", style=C_MUTED)
        table.add_row(
            model,
            f"${p[0]:.3f}",
            f"${p[1]:.3f}",
            f"${p[2]:.3f}",
            f"${p[3]:.3f}",
            source,
        )

    console.print(table)


@pricing_app.command("set", context_settings={"help_option_names": ["-h", "--help"]})
def pricing_set(
    ctx: typer.Context,
    model: str = typer.Argument(None, help="Model name (e.g. claude-opus-4-7)."),
    input_price: float = typer.Option(None, "--input", "-i", help="Input price per 1M tokens."),
    output_price: float = typer.Option(None, "--output", "-o", help="Output price per 1M tokens."),
    cache_read: float = typer.Option(0.0, "--cache-read", "-cr", help="Cache read price per 1M tokens."),
    cache_write: float = typer.Option(0.0, "--cache-write", "-cw", help="Cache write price per 1M tokens."),
) -> None:
    """Add or update pricing for a model (USD per 1M tokens)."""
    if model is None or input_price is None or output_price is None:
        console.print(ctx.get_help())
        console.print()
        console.print(f"[bold {C_TEXT}]Examples:[/]")
        console.print(f"  [{C_MUTED}]agentic-metric pricing set deepseek-r2 -i 0.5 -o 2.0[/]")
        console.print(f"  [{C_MUTED}]agentic-metric pricing set claude-opus-4-7 -i 4.0 -o 20.0 -cr 0.4 -cw 5.0[/]")
        raise typer.Exit()

    from .pricing import set_user_pricing

    set_user_pricing(model, input_price, output_price, cache_read, cache_write)
    console.print(
        f"[bold {C_GREEN}]✓[/] Set pricing for [bold {C_MAUVE}]{model}[/]: "
        f"input=[{C_TEAL}]${input_price:.3f}[/]  output=[{C_TEAL}]${output_price:.3f}[/]  "
        f"cache_read=[{C_SKY}]${cache_read:.3f}[/]  cache_write=[{C_SKY}]${cache_write:.3f}[/]"
    )


@pricing_app.command("reset")
def pricing_reset(
    model: str = typer.Argument(None, help="Model to reset. Omit to reset all."),
    all_models: bool = typer.Option(False, "--all", help="Reset all user overrides."),
) -> None:
    """Reset pricing to builtin defaults."""
    from .pricing import remove_user_pricing, reset_all_user_pricing

    if all_models:
        reset_all_user_pricing()
        console.print(f"[bold {C_GREEN}]✓[/] All user pricing overrides removed.")
    elif model:
        if remove_user_pricing(model):
            console.print(f"[bold {C_GREEN}]✓[/] Reset {model} to builtin default.")
        else:
            console.print(f"[{C_YELLOW}]{model} has no user override.[/]")
    else:
        console.print(f"[{C_RED}]Specify a model name or use --all.[/]")
        raise typer.Exit(1)
