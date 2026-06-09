"""Typer CLI for agentic-metric."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import time
from importlib.metadata import version as _pkg_version

import typer
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .formatting import (
    cache_hit_rate as _cache_hit_rate,
    cache_tokens as _cache_tokens,
    fmt_cost as _fmt_cost,
    fmt_tokens as _fmt_tokens,
    has_cost_signal as _has_cost_signal,
    has_unknown_cost as _has_unknown_cost,
    share_suffix as _share_suffix,
    shorten_home as _shorten_home,
    source_prefixed_path as _source_prefixed_path,
    source_root_label as _source_root_label,
)

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
long_context_app = typer.Typer(
    help="Manage request-size long-context pricing.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
cache_pricing_app = typer.Typer(
    help="Manage cache-duration pricing.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(pricing_app, name="pricing")
pricing_app.add_typer(long_context_app, name="long-context")
pricing_app.add_typer(cache_pricing_app, name="cache")


console = Console()


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

# Boxed panels (header + heatmap) share one width: capped, but adapting
# down to narrower terminals.
MAX_PANEL_WIDTH = 100


def _panel_width() -> int:
    try:
        return min(console.size.width, MAX_PANEL_WIDTH)
    except Exception:
        return MAX_PANEL_WIDTH


def _console_width(default: int = MAX_PANEL_WIDTH) -> int:
    try:
        return console.size.width
    except Exception:
        return default


def _scale_width(base: int, wide: int, *, threshold: int = 120) -> int:
    width = _console_width()
    if width <= threshold:
        return base
    return min(wide, base + (width - threshold) // 4)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"agentic-metric {_pkg_version('agentic-metric-x')}")
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
def sync(
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help="Delete the derived local database before syncing from source session logs.",
    ),
) -> None:
    """Force sync all collectors to the database."""
    from .collectors import create_default_registry
    from .config import DB_PATH
    from .store.database import Database

    if rebuild:
        for path in (
            DB_PATH,
            DB_PATH.with_name(DB_PATH.name + "-wal"),
            DB_PATH.with_name(DB_PATH.name + "-shm"),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    db = Database()
    registry = create_default_registry()

    action = "Rebuilding and syncing" if rebuild else "Syncing"
    console.print(f"[{C_SUBTEXT}]{action} all collectors…[/]")
    registry.sync_all(db)
    db.close()

    console.print(f"[bold {C_GREEN}]✓ Sync complete[/]")
    for c in registry.get_all():
        provider = getattr(c, "provider", "") or "—"
        data_root = _shorten_home(getattr(c, "data_root", "") or "") or "—"
        last_error = getattr(c, "last_error", "")
        console.print(
            f"  [{C_MUTED}]•[/] "
            f"[{C_MAUVE}]{c.agent_type}[/]  "
            f"[{C_SKY}]{provider}[/]  "
            f"[{C_MUTED}]{data_root}[/]"
        )
        if last_error:
            console.print(f"    [{C_YELLOW}]remote sync skipped: {last_error}[/]")


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
        False, "--full", help="Show the full drill-down with host, agent, provider, model, and time tables."
    ),
    limit: int = typer.Option(
        8, "--limit", "-n", min=1, max=25,
        help="Rows shown in Top projects and model drill-downs.",
    ),
    json_: bool = typer.Option(
        False, "--json", help="Output machine-readable JSON instead of tables."
    ),
    watch: float = typer.Option(
        0, "--watch", "-w", min=0,
        help="Refresh every N seconds (0 disables). Press Ctrl-C to stop.",
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
            datetime.strptime(frm, "%Y-%m-%d")
            datetime.strptime(to, "%Y-%m-%d")
            if frm > to:
                console.print(f"[{C_RED}]--range: start date must not be after end date.[/]")
                raise typer.Exit(1)
            label = "Range"
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

    def _once() -> None:
        db = Database()
        sync_errors: list[str] = []
        if db.pricing_changed and no_sync:
            console.print(f"[{C_YELLOW}]Pricing changed; syncing history to refresh event-level costs.[/]")
        if db.pricing_changed or not no_sync:
            registry = create_default_registry()
            registry.sync_all(db)
            sync_errors = registry.get_sync_errors()
            db.commit()

        totals = aggregator.get_range_totals(db, frm, to)
        by_host = aggregator.get_range_by_host(db, frm, to)
        by_agent_type = aggregator.get_range_by_agent_type(db, frm, to)
        by_provider = aggregator.get_range_by_provider(db, frm, to)
        by_model = aggregator.get_range_by_model(db, frm, to)
        by_agent = aggregator.get_range_by_agent(db, frm, to)
        by_agent_model = aggregator.get_range_by_agent_model(db, frm, to)
        by_provider_model = aggregator.get_range_by_provider_model(db, frm, to)
        by_agent_type_model = aggregator.get_range_by_agent_type_model(db, frm, to)
        by_project = aggregator.get_range_by_project(db, frm, to, limit=limit)
        by_project_agent = aggregator.get_range_by_project_agent(db, frm, to, limit=limit)
        by_project_model = aggregator.get_range_by_project_model(db, frm, to, limit=limit)
        # Periodic breakdown (hourly/daily/weekly) — only when the range
        # corresponds to a named focus.
        focus_kind = None
        if not range_:
            focus_kind = "week" if week else ("month" if month else "today")
        periodic = aggregator.get_heatmap(db, focus_kind) if focus_kind else []

        # Previous period totals for delta comparison (cost cell arrow).
        prev_totals = None
        if focus_kind:
            _, p_frm, p_to = aggregator.resolve_range(focus_kind, offset=1)
            prev_totals = aggregator.get_range_totals(db, p_frm, p_to)

        db.close()

        if json_:
            _emit_report_json(
                label, frm, to, totals,
                by_host, by_agent_type, by_provider, by_model,
                by_agent, by_agent_model, by_project, by_project_agent,
                sync_errors=sync_errors,
                by_provider_model=by_provider_model,
                by_agent_type_model=by_agent_type_model,
                by_project_model=by_project_model,
            )
        else:
            if sync_errors:
                for err in sync_errors:
                    console.print(f"[{C_YELLOW}]remote sync skipped: {err}[/]")
            _print_report(
                label, frm, to, totals,
                by_host, by_agent_type, by_provider, by_model,
                by_agent, by_agent_model, by_project, by_project_agent,
                periodic, focus_kind, prev_totals, full=full, limit=limit,
                by_provider_model=by_provider_model,
                by_agent_type_model=by_agent_type_model,
                by_project_model=by_project_model,
            )

    if watch and watch > 0 and not json_:
        try:
            while True:
                console.clear()
                _once()
                console.print(f"[{C_MUTED}]Refreshing every {watch:g}s — Ctrl-C to stop.[/]")
                time.sleep(watch)
        except KeyboardInterrupt:
            console.print(f"\n[{C_MUTED}]Stopped.[/]")
    else:
        _once()


def _emit_report_json(
    label: str, frm: str, to: str,
    totals: dict,
    by_host: list[dict], by_agent_type: list[dict],
    by_provider: list[dict], by_model: list[dict], by_agent: list[dict],
    by_agent_model: list[dict], by_project: list[dict], by_project_agent: list[dict],
    sync_errors: list[str] | None = None,
    by_provider_model: list[dict] | None = None,
    by_agent_type_model: list[dict] | None = None,
    by_project_model: list[dict] | None = None,
) -> None:
    """Print the report as machine-readable JSON (for scripts / pipes)."""
    payload = {
        "label": label,
        "from": frm,
        "to": to,
        "totals": dict(totals),
        "by_host": [dict(r) for r in by_host],
        "by_agent_type": [dict(r) for r in by_agent_type],
        "by_provider": [dict(r) for r in by_provider],
        "by_model": [dict(r) for r in by_model],
        "by_agent": [dict(r) for r in by_agent],
        "by_agent_model": [dict(r) for r in by_agent_model],
        "by_provider_model": [dict(r) for r in (by_provider_model or [])],
        "by_agent_type_model": [dict(r) for r in (by_agent_type_model or [])],
        "by_project": [dict(r) for r in by_project],
        "by_project_agent": [dict(r) for r in by_project_agent],
        "by_project_model": [dict(r) for r in (by_project_model or [])],
        "sync_errors": sync_errors or [],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@app.command("today")
def today_cmd(
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip syncing collectors before querying."),
    full: bool = typer.Option(False, "--full", help="Show the full drill-down with host, agent, provider, model, and time tables."),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=25, help="Rows shown in Top projects and model breakdown."),
    json_: bool = typer.Option(False, "--json", help="Output machine-readable JSON instead of tables."),
    watch: float = typer.Option(0, "--watch", "-w", min=0, help="Refresh every N seconds (0 disables)."),
) -> None:
    """Shortcut for ``report --today``."""
    report(today_=True, week=False, month=False, range_=None, no_sync=no_sync, full=full, limit=limit, json_=json_, watch=watch)


@app.command("week")
def week_cmd(
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip syncing collectors before querying."),
    full: bool = typer.Option(False, "--full", help="Show the full drill-down with host, agent, provider, model, and time tables."),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=25, help="Rows shown in Top projects and model breakdown."),
    json_: bool = typer.Option(False, "--json", help="Output machine-readable JSON instead of tables."),
    watch: float = typer.Option(0, "--watch", "-w", min=0, help="Refresh every N seconds (0 disables)."),
) -> None:
    """Shortcut for ``report --week``."""
    report(today_=False, week=True, month=False, range_=None, no_sync=no_sync, full=full, limit=limit, json_=json_, watch=watch)


@app.command("month")
def month_cmd(
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip syncing collectors before querying."),
    full: bool = typer.Option(False, "--full", help="Show the full drill-down with host, agent, provider, model, and time tables."),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=25, help="Rows shown in Top projects and model breakdown."),
    json_: bool = typer.Option(False, "--json", help="Output machine-readable JSON instead of tables."),
    watch: float = typer.Option(0, "--watch", "-w", min=0, help="Refresh every N seconds (0 disables)."),
) -> None:
    """Shortcut for ``report --month``."""
    report(today_=False, week=False, month=True, range_=None, no_sync=no_sync, full=full, limit=limit, json_=json_, watch=watch)


@app.command("history")
def history_cmd(
    days: int = typer.Option(14, "--days", "-d", min=1, max=365, help="Number of days to include."),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip syncing collectors before querying."),
    full: bool = typer.Option(False, "--full", help="Show the full drill-down with host, agent, provider, model, and time tables."),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=25, help="Rows shown in Top projects and model breakdown."),
    json_: bool = typer.Option(False, "--json", help="Output machine-readable JSON instead of tables."),
    watch: float = typer.Option(0, "--watch", "-w", min=0, help="Refresh every N seconds (0 disables)."),
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
        json_=json_,
        watch=watch,
    )


def _print_report(
    label: str, frm: str, to: str,
    totals: dict,
    by_host: list[dict], by_agent_type: list[dict],
    by_provider: list[dict], by_model: list[dict], by_agent: list[dict],
    by_agent_model: list[dict], by_project: list[dict], by_project_agent: list[dict],
    periodic: list[dict],
    focus_kind: str | None,
    prev_totals: dict | None = None,
    *,
    full: bool = False,
    limit: int = 8,
    by_provider_model: list[dict] | None = None,
    by_agent_type_model: list[dict] | None = None,
    by_project_model: list[dict] | None = None,
) -> None:
    tot_cost = totals.get("estimated_cost_usd") or 0.0
    tot_cost_unknown = _has_unknown_cost(totals)
    tot_sess = totals.get("session_count") or 0
    tot_turns = totals.get("user_turns") or 0
    tot_msgs = totals.get("message_count") or 0
    tot_requests = max(0, tot_msgs - tot_turns)
    cache_pct = _cache_hit_rate(totals)

    # ─── Header panel (label + stats) ───
    header_text = Text()
    header_text.append(label, style=f"bold {C_PEACH}")
    if frm == to:
        header_text.append(f"   {frm}", style=C_MUTED)
    else:
        header_text.append(f"   {frm} → {to}", style=C_MUTED)

    delta_line = _delta_line(tot_cost, prev_totals, current_unknown=tot_cost_unknown)
    cost_cell = Group(
        Text("COST", style=f"{C_MUTED}"),
        Text(_fmt_cost(tot_cost, unknown=tot_cost_unknown), style=f"bold {C_YELLOW}"),
        delta_line if delta_line else Text(""),
    )
    # Token split lives in the heatmap panel; cache share is elevated here too.
    stats = Table.grid(padding=(0, 3))
    for _ in range(5):
        stats.add_column(justify="left")
    stats.add_row(
        cost_cell,
        _stat("Cache %", f"{cache_pct:.0f}%" if cache_pct >= 0 else "—", C_GREEN),
        _stat("Sessions", f"{tot_sess:,}", C_MAUVE),
        _stat("Requests", f"{tot_requests:,}", C_SKY),
        _stat("Turns", f"{tot_turns:,}", C_SKY),
    )

    panel_width = _panel_width()
    header_panel = Panel(
        Group(header_text, Text(""), stats),
        box=box.ROUNDED,
        border_style=C_SURFACE1,
        padding=(1, 2),
        width=panel_width,
    )

    # ─── Heatmap strip (today/week/month scope) ───
    heatmap_renderable = None
    if periodic and focus_kind:
        heatmap_renderable = _build_heatmap_panel(
            periodic, focus_kind, totals, by_project, width=panel_width,
        )

    # ─── Table renderables ───
    breakdown_tbl = _build_by_agent_model_table(by_agent_model, limit=limit)
    project_tbl = _build_top_projects_table(by_project)
    project_agent_tbl = _build_project_agent_table(by_project_agent) if full else None
    host_tbl = _build_dimension_table("By host", "Host", by_host, "host", max_label_width=18) if full else None
    agent_type_tbl = _build_dimension_table("By agent", "Agent", by_agent_type, "agent_type", max_label_width=18) if full else None
    provider_tbl = _build_dimension_table("By provider", "Provider", by_provider, "provider", max_label_width=18) if full else None
    model_tbl = _build_dimension_table("By model", "Model", by_model, "model", max_label_width=32) if full else None
    periodic_tbl = _build_periodic_table(periodic, focus_kind) if full else None

    # A × model cross tables (full only). Each gives model one extra lens:
    # by billing channel, by agent, and by project.
    provider_model_tbl = agent_type_model_tbl = project_model_tbl = None
    if full:
        provider_model_tbl = _build_cross_table(
            "By provider × model", "Provider", by_provider_model or [],
            lambda r: r.get("provider") or "—", a_width=_scale_width(10, 16),
        )
        agent_type_model_tbl = _build_cross_table(
            "By agent × model", "Agent", by_agent_type_model or [],
            lambda r: r.get("agent_type") or "—", a_width=_scale_width(10, 16),
        )
        pm_width = max(28, min(120, _console_width() - 60))
        project_model_tbl = _build_cross_table(
            "By project × model", "Project", by_project_model or [],
            lambda r: _source_prefixed_path(
                r.get("project_path") or "(unspecified)",
                r.get("data_root") or "", max_len=pm_width,
            ),
            a_width=pm_width, model_width=_scale_width(14, 24),
        )

    # ─── Render ───
    # Non-full keeps the consolidated 4-D cross plus Top projects.
    # Full drills down coarse → fine: single-dim aggregates, the model
    # cross lenses, the project block, the full cross, and finally the
    # time breakdown.
    if full:
        ordered = (
            host_tbl,            # By host
            agent_type_tbl,      # By agent
            provider_tbl,        # By provider
            model_tbl,           # By model
            provider_model_tbl,  # By provider × model
            agent_type_model_tbl,# By agent × model
            project_tbl,         # Top projects
            project_agent_tbl,   # By project × agent
            project_model_tbl,   # By project × model
            breakdown_tbl,       # By source × agent × provider × model
            periodic_tbl,        # By hour / day / week
        )
    else:
        ordered = (breakdown_tbl, project_tbl)

    console.print()
    console.print(header_panel)
    if heatmap_renderable is not None:
        console.print(heatmap_renderable)

    for t in ordered:
        if t is not None:
            console.print(t)

    unknown_note = _build_unknown_models_note(by_agent_model)
    if unknown_note is not None:
        console.print(unknown_note)

    console.print()


def _build_unknown_models_note(by_agent_model: list[dict]) -> Group | None:
    """Hint the user how to price models whose cost is shown as '?'."""
    seen: list[str] = []
    for r in by_agent_model:
        if not _has_unknown_cost(r):
            continue
        name = (r.get("raw_model") or r.get("model") or "").strip()
        if name and name not in seen:
            seen.append(name)
    if not seen:
        return None

    lines: list[Text] = [
        Text.assemble(
            ("Unknown models", f"bold {C_YELLOW}"),
            (" — cost shown as ", C_MUTED),
            ("?", f"bold {C_YELLOW}"),
            (". Set pricing (USD per 1M tokens):", C_MUTED),
        )
    ]
    for name in seen:
        lines.append(Text.assemble(
            ("  agentic-metric pricing set ", C_MUTED),
            (name, C_MAUVE),
            (" -i <input> -o <output>", C_MUTED),
        ))
    return Group(*lines)


def _build_heatmap_panel(
    buckets: list[dict],
    focus_kind: str,
    totals: dict,
    by_project: list[dict],
    *,
    width: int | None = None,
) -> Panel:
    """Render the activity heatmap with token split + peak + top projects."""
    blocks = ["·", "•", "░", "▒", "▓", "█", "█"]
    styles = [
        "grey35",
        "dim green",
        "green",
        "green",
        "bright_green",
        "bright_green",
        "bold bright_green",
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
        outer = width if width is not None else console.size.width
        available = max(24, outer - 8)
        cell_w = min(cell_w, max(2, available // max(n, 1)))
    except Exception:
        pass

    now = datetime.now()
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
        style = styles[lvl]
        if i == highlight:
            style = f"{style} reverse"
        row_blocks.append(blocks[lvl] * cell_w, style=style)
        if i % label_every == 0:
            row_labels.append(b["label"][:cell_w].center(cell_w), style=C_MUTED)
        else:
            row_labels.append(" " * cell_w, style="default")

    # Peak summary below the strip (no total line — total already lives in header)
    known_peak = max(buckets, key=lambda bb: bb.get("cost") or 0)
    unknown_peak = next((bb for bb in buckets if _has_unknown_cost(bb)), None)
    peak = known_peak if (known_peak.get("cost") or 0) > 0 else (unknown_peak or known_peak)
    peak_unknown = _has_unknown_cost(peak)
    peak_line = Text(" ")
    if (peak.get("cost") or 0) > 0 or peak_unknown:
        peak_line.append("peak ", style=C_MUTED)
        peak_line.append(peak["label"], style="bold")
        peak_line.append(f"  {_fmt_cost(peak.get('cost'), unknown=peak_unknown)}", style=C_YELLOW)
        peak_line.append(f"  {_fmt_tokens(peak.get('tokens') or 0)}", style=C_TEAL)
    else:
        peak_line.append("peak —", style=C_MUTED)

    # Token summary (total + cache hit, then input/output/cache split) above the strip
    tsplit = _token_summary_block(totals)

    # Top projects (top 3) below the peak line
    projects_block = _top_projects_block(
        by_project, totals.get("estimated_cost_usd") or 0.0,
        total_unknown=_has_unknown_cost(totals),
    )

    body: list[object] = []
    if tsplit is not None:
        body.append(tsplit)
        body.append(Text(""))
    body.extend([row_blocks, row_labels, peak_line])
    if projects_block is not None:
        body.append(Text(""))
        body.append(projects_block)

    titles = {"today": "Today by hour",
              "week":  "This week by day",
              "month": "This month by week"}
    return Panel(
        Group(*body),
        title=titles.get(focus_kind, "Heatmap"),
        title_align="left",
        box=box.ROUNDED,
        border_style=C_SURFACE1,
        padding=(0, 1),
        width=width,
    )


def _top_projects_block(
    by_project: list[dict],
    total_cost: float,
    *,
    total_unknown: bool = False,
    limit: int = 3,
) -> Group | None:
    """Render up to ``limit`` projects as a vertical block for heatmap panel."""
    if not by_project:
        return None
    entries = [
        p for p in by_project
        if (p.get("estimated_cost_usd") or 0) > 0 or _has_unknown_cost(p)
    ][:limit]
    if not entries:
        return None

    any_unknown = total_unknown or any(_has_unknown_cost(p) for p in entries)
    label_width = len("Top projects")  # align subsequent rows' path column
    lines: list[Text] = []
    for i, p in enumerate(entries):
        unknown = _has_unknown_cost(p)
        label = "Top projects" if i == 0 else " " * label_width
        line = Text(" ")
        line.append(label, style=C_MUTED)
        line.append("  ")
        try:
            path_len = 22 if console.size.width < 100 else 44
        except Exception:
            path_len = 44
        line.append(
            _source_prefixed_path(
                p["project_path"],
                p.get("data_root") or "",
                max_len=path_len,
            ),
            style=C_BLUE,
        )
        line.append(
            f" · {_fmt_cost(p['estimated_cost_usd'], unknown=unknown)}",
            style=f"bold {C_YELLOW}" if i == 0 else C_YELLOW,
        )
        line.append(
            _share_suffix(
                p["estimated_cost_usd"], total_cost,
                unknown=unknown, total_unknown=any_unknown,
            ),
            style=C_MUTED,
        )
        lines.append(line)
    return Group(*lines)


def _build_dimension_table(
    title: str,
    label: str,
    rows: list[dict],
    label_key: str,
    *,
    max_label_width: int,
) -> Table | None:
    nonzero = [r for r in rows if _has_cost_signal(r)]
    if not nonzero:
        return None
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title=title,
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    tbl.add_column(label, style=C_SKY, overflow="ellipsis", no_wrap=True, max_width=max_label_width)
    tbl.add_column("Sess", justify="right", style=C_TEXT, no_wrap=True)
    tbl.add_column("Req", justify="right", style=C_TEXT, no_wrap=True)
    tbl.add_column("Turns", justify="right", style=C_TEXT, no_wrap=True)
    tbl.add_column("In", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Out", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Cache", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("C%", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}", no_wrap=True, min_width=6)

    for r in nonzero:
        display = r.get(label_key) or "—"
        if label_key == "model" and display == "Unknown" and r.get("raw_model"):
            display = f"Unknown: {r['raw_model']}"
        turns = r.get("user_turns") or 0
        messages = r.get("message_count") or 0
        cp = _cache_hit_rate(r)
        tbl.add_row(
            display,
            f"{r.get('session_count') or 0:,}",
            f"{max(0, messages - turns):,}",
            f"{turns:,}",
            _fmt_tokens(r.get("input_tokens") or 0),
            _fmt_tokens(r.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(r)),
            f"{cp:.0f}%" if cp >= 0 else "—",
            _fmt_cost(r.get("estimated_cost_usd"), unknown=_has_unknown_cost(r)),
        )
    return tbl


def _build_by_agent_model_table(rows: list[dict], *, limit: int = 8) -> Table | None:
    nonzero = [r for r in rows if _has_cost_signal(r)]
    if not nonzero:
        return None
    source_width = _scale_width(16, 34)
    agent_width = _scale_width(8, 14)
    provider_width = _scale_width(6, 12)
    model_width = _scale_width(14, 32)
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title="By source × agent × provider × model",
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    tbl.add_column("Source", style=C_MUTED, overflow="ellipsis", no_wrap=True, max_width=source_width)
    tbl.add_column("Ag", style=C_MAUVE, overflow="ellipsis", no_wrap=True, min_width=5, max_width=agent_width)
    tbl.add_column("Prov", style=C_SKY, overflow="ellipsis", no_wrap=True, min_width=4, max_width=provider_width)
    tbl.add_column("Model", style=C_SKY, overflow="ellipsis", no_wrap=True, max_width=model_width)
    tbl.add_column("In", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Out", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Cache", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("C%", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}", no_wrap=True, min_width=6)

    # Group consecutive rows by (agent, provider, root) and cap the model
    # rows per group at ``limit``; the tail rolls up into "+N more models".
    order: list[tuple] = []
    buckets: dict[tuple, list[dict]] = {}
    for r in nonzero:
        key = (r["agent_type"], r.get("provider") or "", r.get("data_root") or "")
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(r)

    def _model_row(r: dict, show_group: bool) -> None:
        model_display = r["model"]
        if model_display == "Unknown" and r.get("raw_model"):
            model_display = f"Unknown: {r['raw_model']}"
        data_root = r.get("data_root") or ""
        cp = _cache_hit_rate(r)
        tbl.add_row(
            _source_root_label(data_root, max_len=source_width) if show_group else "",
            r["agent_type"] if show_group else "",
            (r.get("provider") or "—") if show_group else "",
            model_display,
            _fmt_tokens(r.get("input_tokens") or 0),
            _fmt_tokens(r.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(r)),
            f"{cp:.0f}%" if cp >= 0 else "—",
            _fmt_cost(r.get("estimated_cost_usd"), unknown=_has_unknown_cost(r)),
        )

    for key in order:
        members = buckets[key]
        visible = members[:limit]
        hidden = members[limit:]
        for i, r in enumerate(visible):
            _model_row(r, show_group=(i == 0))
        if hidden:
            agg = {
                "input_tokens": sum(m.get("input_tokens") or 0 for m in hidden),
                "output_tokens": sum(m.get("output_tokens") or 0 for m in hidden),
                "cache_read_tokens": sum(m.get("cache_read_tokens") or 0 for m in hidden),
                "cache_creation_tokens": sum(m.get("cache_creation_tokens") or 0 for m in hidden),
                "estimated_cost_usd": sum(m.get("estimated_cost_usd") or 0 for m in hidden),
                "unknown_cost_count": sum(m.get("unknown_cost_count") or 0 for m in hidden),
            }
            agg_cp = _cache_hit_rate(agg)
            tbl.add_row(
                "", "", "",
                f"+{len(hidden)} more models",
                _fmt_tokens(agg["input_tokens"]),
                _fmt_tokens(agg["output_tokens"]),
                _fmt_tokens(_cache_tokens(agg)),
                f"{agg_cp:.0f}%" if agg_cp >= 0 else "—",
                _fmt_cost(agg["estimated_cost_usd"], unknown=_has_unknown_cost(agg)),
            )
    return tbl


def _build_cross_table(
    title: str,
    a_title: str,
    rows: list[dict],
    a_value,
    *,
    a_width: int,
    model_width: int | None = None,
) -> Table | None:
    """Render a two-key A × model breakdown (one row per A × model).

    ``a_value`` maps a row to its left-column display string; the model
    column handles the ``Unknown: <raw>`` relabel. Rows arrive pre-sorted
    by cost, so A repeats across its models instead of being grouped.
    """
    nonzero = [r for r in rows if _has_cost_signal(r)]
    if not nonzero:
        return None
    model_width = model_width or _scale_width(14, 32)
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title=title,
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    tbl.add_column(a_title, style=C_MAUVE, overflow="ellipsis", no_wrap=True, max_width=a_width)
    tbl.add_column("Model", style=C_SKY, overflow="ellipsis", no_wrap=True, max_width=model_width)
    tbl.add_column("Sess", justify="right", style=C_TEXT, no_wrap=True)
    tbl.add_column("In", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Out", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Cache", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("C%", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}", no_wrap=True, min_width=7)
    for r in nonzero:
        model_display = r.get("model") or "—"
        if model_display == "Unknown" and r.get("raw_model"):
            model_display = f"Unknown: {r['raw_model']}"
        cp = _cache_hit_rate(r)
        tbl.add_row(
            a_value(r),
            model_display,
            f"{r.get('session_count') or 0:,}",
            _fmt_tokens(r.get("input_tokens") or 0),
            _fmt_tokens(r.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(r)),
            f"{cp:.0f}%" if cp >= 0 else "—",
            _fmt_cost(r.get("estimated_cost_usd"), unknown=_has_unknown_cost(r)),
        )
    return tbl


def _build_top_projects_table(rows: list[dict]) -> Table | None:
    nonzero = [r for r in rows if _has_cost_signal(r)]
    if not nonzero:
        return None
    project_width = max(32, min(140, _console_width() - 44))
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
    tbl.add_column("Project", style=C_BLUE, overflow="ellipsis", no_wrap=True, max_width=project_width)
    tbl.add_column("Sessions", justify="right", style=C_TEXT, no_wrap=True)
    tbl.add_column("Input", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Output", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Cache", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("C%", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}", no_wrap=True, min_width=7)
    for r in nonzero:
        path = _source_prefixed_path(
            r["project_path"] or "(unspecified)",
            r.get("data_root") or "",
            max_len=project_width,
        )
        cp = _cache_hit_rate(r)
        tbl.add_row(
            path,
            f"{r['session_count']:,}",
            _fmt_tokens(r.get("input_tokens") or 0),
            _fmt_tokens(r.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(r)),
            f"{cp:.0f}%" if cp >= 0 else "—",
            _fmt_cost(r.get("estimated_cost_usd"), unknown=_has_unknown_cost(r)),
        )
    return tbl


def _build_project_agent_table(rows: list[dict]) -> Table | None:
    nonzero = [r for r in rows if _has_cost_signal(r)]
    if not nonzero:
        return None
    project_width = max(28, min(120, _console_width() - 56))
    agent_width = _scale_width(8, 14)
    tbl = Table(
        show_header=True,
        header_style=f"bold {C_SUBTEXT}",
        box=box.SIMPLE_HEAVY,
        pad_edge=False,
        border_style=C_SURFACE1,
        title="By project × agent",
        title_style=f"bold {C_TEXT}",
        title_justify="left",
    )
    tbl.add_column("Project", style=C_BLUE, overflow="ellipsis", no_wrap=True, max_width=project_width)
    tbl.add_column("Agent", style=C_MAUVE, overflow="ellipsis", no_wrap=True, min_width=5, max_width=agent_width)
    tbl.add_column("Sess", justify="right", style=C_TEXT, no_wrap=True)
    tbl.add_column("Req", justify="right", style=C_TEXT, no_wrap=True)
    tbl.add_column("Turns", justify="right", style=C_TEXT, no_wrap=True)
    tbl.add_column("In", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Out", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Cache", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("C%", justify="right", style=C_GREEN, no_wrap=True)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}", no_wrap=True, min_width=6)
    for r in nonzero:
        turns = r.get("user_turns") or 0
        messages = r.get("message_count") or 0
        path = _source_prefixed_path(
            r["project_path"] or "(unspecified)",
            r.get("data_root") or "",
            max_len=project_width,
        )
        cp = _cache_hit_rate(r)
        tbl.add_row(
            path,
            r.get("agent_type") or "—",
            f"{r.get('session_count') or 0:,}",
            f"{max(0, messages - turns):,}",
            f"{turns:,}",
            _fmt_tokens(r.get("input_tokens") or 0),
            _fmt_tokens(r.get("output_tokens") or 0),
            _fmt_tokens(_cache_tokens(r)),
            f"{cp:.0f}%" if cp >= 0 else "—",
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
    tbl.add_column(bucket_col, style=C_BLUE, no_wrap=True)
    tbl.add_column("Sessions", justify="right", style=C_TEXT, no_wrap=True)
    tbl.add_column("Cache %", justify="right", style=f"bold {C_GREEN}", no_wrap=True, min_width=7)
    tbl.add_column("Tokens", justify="right", style=C_TEAL, no_wrap=True)
    tbl.add_column("Cost", justify="right", style=f"bold {C_YELLOW}", no_wrap=True)
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
        cp = _cache_hit_rate(b)
        tbl.add_row(
            label_col,
            f"{b['session_count']:,}",
            f"{cp:.0f}%" if cp >= 0 else "—",
            _fmt_tokens(b.get("tokens") or 0),
            _fmt_cost(cost, unknown=unknown),
            bar,
        )
    return tbl


def _stat(label: str, value: str, color: str) -> Group:
    label_text = Text(label.upper(), style=f"{C_MUTED}")
    return Group(label_text, Text(value, style=f"bold {color}"))


# ── helpers ────────────────────────────────────────────────────────
# Pure formatting helpers are in cli/formatting.py.


def _token_summary_block(totals: dict) -> Group | None:
    """Two-line token block for the heatmap panel (CLI side).

    Line 1: ``Token total N · Cache % P%``
    Line 2: ``Token input X · output Y · cache read Z · cache write W``
    """
    if not totals:
        return None
    input_t = totals.get("input_tokens") or 0
    output_t = totals.get("output_tokens") or 0
    cache_r = totals.get("cache_read_tokens") or 0
    cache_w = totals.get("cache_creation_tokens") or 0
    total_t = input_t + output_t + cache_r + cache_w
    if total_t == 0:
        return None

    cache_pct = _cache_hit_rate(totals)

    line_total = Text()
    line_total.append("Token total ", style=C_MUTED)
    line_total.append(_fmt_tokens(total_t), style=C_TEAL)
    if cache_pct >= 0:
        line_total.append("  ·  Cache % ", style=C_MUTED)
        line_total.append(f"{cache_pct:.0f}%", style=C_GREEN)

    line_split = Text()
    line_split.append("Token ", style=C_MUTED)
    line_split.append("input ", style=C_MUTED)
    line_split.append(_fmt_tokens(input_t), style=C_TEAL)
    line_split.append("  ·  output ", style=C_MUTED)
    line_split.append(_fmt_tokens(output_t), style=C_TEAL)
    line_split.append("  ·  cache read ", style=C_MUTED)
    line_split.append(_fmt_tokens(cache_r), style=C_GREEN)
    if cache_w:
        line_split.append("  ·  cache write ", style=C_MUTED)
        line_split.append(_fmt_tokens(cache_w), style=C_GREEN)

    return Group(line_total, line_split)


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


def _refresh_history_after_pricing_change() -> None:
    """Re-read local history so event-level pricing reflects the new rules."""
    from .collectors import create_default_registry
    from .store.database import Database

    db = Database()
    try:
        if db.pricing_changed:
            registry = create_default_registry()
            registry.sync_all(db)
            db.commit()
            console.print(f"[bold {C_GREEN}]✓[/] Repriced history from local event data.")
    finally:
        db.close()


@pricing_app.command("list")
def pricing_list(
    json_: bool = typer.Option(False, "--json", help="Output pricing as machine-readable JSON."),
) -> None:
    """List model pricing plus long-context and cache-duration rules."""
    from .pricing import (
        _BUILTIN_PRICING,
        _load_user_pricing,
        get_all_pricing,
        get_long_context_rules,
        get_user_cache_pricing,
    )

    user = _load_user_pricing()

    if json_:
        merged = get_all_pricing()
        payload = {
            "models": {
                m: {
                    "input": p[0], "output": p[1],
                    "cache_read": p[2], "cache_write": p[3],
                    "source": (
                        "custom" if m in user and m not in _BUILTIN_PRICING
                        else "override" if m in user and tuple(p) != tuple(_BUILTIN_PRICING.get(m, ()))
                        else "builtin"
                    ),
                }
                for m, p in sorted(merged.items())
            },
            "long_context": [
                {
                    "prefixes": [str(x) for x in r["prefixes"]],
                    "threshold": int(r["threshold"]),
                    "prices": [float(v) for v in r["prices"]],
                    "source": str(r.get("source") or "builtin"),
                }
                for r in get_long_context_rules(include_disabled=True)
            ],
            "cache": get_user_cache_pricing(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

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

    lc_rows = get_long_context_rules(include_disabled=True)
    if lc_rows:
        lc_table = Table(
            title="Long Context Pricing (USD per 1M tokens)",
            title_style=f"bold {C_TEXT}",
            box=box.SIMPLE_HEAVY,
            border_style=C_SURFACE1,
            header_style=f"bold {C_SUBTEXT}",
            pad_edge=False,
        )
        lc_table.add_column("Model Prefix", style=C_MAUVE)
        lc_table.add_column("Threshold", justify="right", style=C_TEAL)
        lc_table.add_column("Input", justify="right", style=C_TEAL)
        lc_table.add_column("Output", justify="right", style=C_TEAL)
        lc_table.add_column("Cache Read", justify="right", style=C_SKY)
        lc_table.add_column("Cache Write", justify="right", style=C_SKY)
        lc_table.add_column("Source", style=C_MUTED)
        for rule in lc_rows:
            prefixes = ", ".join(str(p) for p in rule["prefixes"])
            prices = tuple(float(v) for v in rule["prices"])
            source = str(rule.get("source") or "builtin")
            source_style = C_PEACH if source == "user" else (C_RED if source == "disabled" else C_MUTED)
            lc_table.add_row(
                prefixes,
                f"{int(rule['threshold']):,}",
                f"${prices[0]:.3f}",
                f"${prices[1]:.3f}",
                f"${prices[2]:.3f}",
                f"${prices[3]:.3f}",
                Text(source, style=source_style),
            )
        console.print()
        console.print(lc_table)

    cache_rows = get_user_cache_pricing()
    if cache_rows:
        cache_table = Table(
            title="Cache Duration Overrides (USD per 1M tokens)",
            title_style=f"bold {C_TEXT}",
            box=box.SIMPLE_HEAVY,
            border_style=C_SURFACE1,
            header_style=f"bold {C_SUBTEXT}",
            pad_edge=False,
        )
        cache_table.add_column("Model Prefix", style=C_MAUVE)
        cache_table.add_column("1h Write", justify="right", style=C_SKY)
        cache_table.add_column("Source", style=C_MUTED)
        for model, rule in sorted(cache_rows.items()):
            cache_table.add_row(
                model,
                f"${float(rule['write_1h']):.3f}" if "write_1h" in rule else "",
                Text("user", style=C_PEACH),
            )
        console.print()
        console.print(cache_table)


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
        raise typer.Exit(1)

    from .pricing import set_user_pricing

    set_user_pricing(model, input_price, output_price, cache_read, cache_write)
    console.print(
        f"[bold {C_GREEN}]✓[/] Set pricing for [bold {C_MAUVE}]{model}[/]: "
        f"input=[{C_TEAL}]${input_price:.3f}[/]  output=[{C_TEAL}]${output_price:.3f}[/]  "
        f"cache_read=[{C_SKY}]${cache_read:.3f}[/]  cache_write=[{C_SKY}]${cache_write:.3f}[/]"
    )
    _refresh_history_after_pricing_change()


@pricing_app.command("reset")
def pricing_reset(
    model: str = typer.Argument(None, help="Model to reset. Omit to reset all."),
    all_models: bool = typer.Option(False, "--all", help="Reset all user overrides."),
) -> None:
    """Reset pricing to builtin defaults."""
    from .pricing import remove_user_pricing, reset_all_user_pricing

    if all_models:
        reset_all_user_pricing()
        console.print(f"[bold {C_GREEN}]✓[/] All user pricing config removed.")
        _refresh_history_after_pricing_change()
    elif model:
        if remove_user_pricing(model):
            console.print(f"[bold {C_GREEN}]✓[/] Reset {model} to builtin default.")
            _refresh_history_after_pricing_change()
        else:
            console.print(f"[{C_YELLOW}]{model} has no user override.[/]")
    else:
        console.print(f"[{C_RED}]Specify a model name or use --all.[/]")
        raise typer.Exit(1)


@long_context_app.command("set", context_settings={"help_option_names": ["-h", "--help"]})
def pricing_long_context_set(
    ctx: typer.Context,
    model: str = typer.Argument(None, help="Model prefix, e.g. gpt-5.5."),
    threshold: int = typer.Option(None, "--threshold", "-t", help="Request input-token threshold."),
    input_price: float = typer.Option(None, "--input", "-i", help="Input price per 1M tokens."),
    output_price: float = typer.Option(None, "--output", "-o", help="Output price per 1M tokens."),
    cache_read: float = typer.Option(0.0, "--cache-read", "-cr", help="Cache read price per 1M tokens."),
    cache_write: float = typer.Option(0.0, "--cache-write", "-cw", help="Cache write price per 1M tokens."),
) -> None:
    """Add or update one long-context tier for a model prefix."""
    if model is None or threshold is None or input_price is None or output_price is None:
        console.print(ctx.get_help())
        console.print()
        console.print(f"[bold {C_TEXT}]Example:[/]")
        console.print(
            f"  [{C_MUTED}]agentic-metric pricing long-context set gpt-5.5 "
            f"--threshold 272000 -i 10 -o 45 -cr 1 -cw 0[/]"
        )
        console.print(
            f"  [{C_MUTED}]agentic-metric pricing long-context set gpt-5.5 "
            f"--threshold 512000 -i 12 -o 52 -cr 1.2 -cw 0[/]"
        )
        raise typer.Exit(1)

    from .pricing import set_user_long_context_pricing

    set_user_long_context_pricing(
        model,
        threshold,
        input_price,
        output_price,
        cache_read,
        cache_write,
    )
    console.print(
        f"[bold {C_GREEN}]✓[/] Set long-context pricing for [bold {C_MAUVE}]{model}[/]: "
        f"threshold=[{C_TEAL}]{threshold:,}[/]  input=[{C_TEAL}]${input_price:.3f}[/]  "
        f"output=[{C_TEAL}]${output_price:.3f}[/]  cache_read=[{C_SKY}]${cache_read:.3f}[/]  "
        f"cache_write=[{C_SKY}]${cache_write:.3f}[/]"
    )
    _refresh_history_after_pricing_change()


@long_context_app.command("reset")
def pricing_long_context_reset(
    model: str = typer.Argument(..., help="Model prefix to reset."),
    threshold: int = typer.Option(None, "--threshold", "-t", help="Remove one threshold tier only."),
) -> None:
    """Remove one or all user long-context tiers and fall back to builtin behavior."""
    from .pricing import remove_user_long_context_pricing

    if remove_user_long_context_pricing(model, threshold=threshold):
        if threshold is None:
            console.print(f"[bold {C_GREEN}]✓[/] Removed all long-context overrides for {model}.")
        else:
            console.print(f"[bold {C_GREEN}]✓[/] Removed long-context tier {threshold:,} for {model}.")
        _refresh_history_after_pricing_change()
    else:
        if threshold is None:
            console.print(f"[{C_YELLOW}]{model} has no long-context override.[/]")
        else:
            console.print(f"[{C_YELLOW}]{model} has no long-context tier at {threshold:,}.[/]")


@long_context_app.command("disable")
def pricing_long_context_disable(
    model: str = typer.Argument(..., help="Builtin model prefix to disable."),
) -> None:
    """Disable builtin long-context pricing for a model prefix."""
    from .pricing import disable_builtin_long_context

    disable_builtin_long_context(model)
    console.print(f"[bold {C_GREEN}]✓[/] Disabled builtin long-context pricing for {model}.")
    _refresh_history_after_pricing_change()


@long_context_app.command("enable")
def pricing_long_context_enable(
    model: str = typer.Argument(..., help="Builtin model prefix to enable."),
) -> None:
    """Re-enable builtin long-context pricing for a model prefix."""
    from .pricing import enable_builtin_long_context

    if enable_builtin_long_context(model):
        console.print(f"[bold {C_GREEN}]✓[/] Enabled builtin long-context pricing for {model}.")
        _refresh_history_after_pricing_change()
    else:
        console.print(f"[{C_YELLOW}]{model} was not disabled.[/]")


@cache_pricing_app.command("set", context_settings={"help_option_names": ["-h", "--help"]})
def pricing_cache_set(
    ctx: typer.Context,
    model: str = typer.Argument(None, help="Model prefix, e.g. claude-sonnet-4."),
    write_1h: float = typer.Option(None, "--write-1h", help="1-hour cache write price per 1M tokens."),
) -> None:
    """Add or update cache-duration pricing for a model prefix."""
    if model is None or write_1h is None:
        console.print(ctx.get_help())
        console.print()
        console.print(f"[bold {C_TEXT}]Example:[/]")
        console.print(
            f"  [{C_MUTED}]agentic-metric pricing cache set claude-sonnet-4 --write-1h 6[/]"
        )
        raise typer.Exit(1)

    from .pricing import set_user_cache_pricing

    set_user_cache_pricing(model, write_1h=write_1h)
    console.print(
        f"[bold {C_GREEN}]✓[/] Set cache pricing for [bold {C_MAUVE}]{model}[/]: "
        f"write_1h=[{C_SKY}]${write_1h:.3f}[/]"
    )
    _refresh_history_after_pricing_change()


@cache_pricing_app.command("reset")
def pricing_cache_reset(
    model: str = typer.Argument(..., help="Model prefix to reset."),
) -> None:
    """Remove user cache-duration pricing for a model prefix."""
    from .pricing import remove_user_cache_pricing

    if remove_user_cache_pricing(model):
        console.print(f"[bold {C_GREEN}]✓[/] Removed cache pricing override for {model}.")
        _refresh_history_after_pricing_change()
    else:
        console.print(f"[{C_YELLOW}]{model} has no cache pricing override.[/]")
