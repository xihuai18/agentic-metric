"""Textual TUI application for agentic-metric."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Footer, Header, Static
from textual.widgets._footer import FooterKey
from textual_plotext import PlotextPlot

from ..collectors import CollectorRegistry, create_default_registry
from ..config import AUTO_REFRESH_INTERVAL, DATA_SYNC_INTERVAL, LIVE_REFRESH_INTERVAL
from ..models import LiveSession
from ..store.aggregator import (
    get_heatmap,
    get_range_by_agent_model,
    get_range_by_project,
    get_range_by_time_model,
    get_range_totals,
    get_today_sessions,
    get_trend,
    resolve_range,
)
from ..store.database import Database
from .widgets import Breakdown, CostDriverSummary, PeriodicHeatmap, SummaryCell, fmt_cost, fmt_tokens


def _total_tokens(d: dict) -> int:
    return (
        (d.get("input_tokens") or 0)
        + (d.get("output_tokens") or 0)
        + (d.get("cache_read_tokens") or 0)
        + (d.get("cache_creation_tokens") or 0)
    )


def _cache_hit_pct(d: dict) -> int | None:
    input_tokens = d.get("input_tokens") or 0
    cache_tokens = (d.get("cache_read_tokens") or 0) + (d.get("cache_creation_tokens") or 0)
    denom = input_tokens + cache_tokens
    if denom <= 0:
        return None
    return round((cache_tokens / denom) * 100)


# Trend configuration per focused view (long-range chart only; the
# today hour heatmap is rendered separately).
_TREND_CONFIG = {
    "today": ("day",   30, "last 30 days"),
    "week":  ("week",  12, "last 12 weeks"),
    "month": ("month", 12, "last 12 months"),
}


def _fmt_bar_label(v: float) -> str:
    """Short dollar label for trend bar tops."""
    if v >= 1000:
        return f"${v / 1000:.1f}k"
    if v >= 100:
        return f"${v:.0f}"
    if v >= 10:
        return f"${v:.0f}"
    if v >= 1:
        return f"${v:.1f}"
    if v > 0:
        return f"${v:.2f}"
    return ""


def _bucket_label(row: dict) -> str:
    date_s = row.get("usage_date") or ""
    hour = int(row.get("usage_hour") or 0)
    try:
        dt = datetime.strptime(date_s, "%Y-%m-%d")
        day = f"{dt.strftime('%b')} {dt.day}"
    except ValueError:
        day = date_s
    return f"{day} {hour:02d}:00" if day else f"{hour:02d}:00"


def _short_path(path: str, max_len: int = 38) -> str:
    if not path:
        return "(unspecified)"
    path = _shorten_home(path)
    if len(path) <= max_len:
        return path
    return path[: max_len - 1] + "…"


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


class _AutoAwareFooter(Footer):
    """Footer that tags the currently-visible auto-refresh binding.

    ``check_action`` hides the "off" variant while auto-refresh is inactive
    (and vice versa), so whichever FooterKey survives here is the one
    matching the current state. The ``-auto-on`` class on the "off" key
    lets the stylesheet highlight it while running.
    """

    def compose(self) -> ComposeResult:
        for child in super().compose():
            if isinstance(child, FooterKey) and child.action == "auto_refresh_off":
                child.add_class("-auto-on")
            yield child


def _split_label(row: dict) -> str:
    cache = (row.get("cache_read_tokens") or row.get("cache") or 0) + (
        row.get("cache_creation_tokens") or 0
    )
    parts = [
        f"in {fmt_tokens(row.get('input_tokens') or row.get('input') or 0)}",
        f"out {fmt_tokens(row.get('output_tokens') or row.get('output') or 0)}",
    ]
    if cache > 0:
        parts.append(f"cache {fmt_tokens(cache)}")
    return "  ".join(parts)


class AgenticMetricApp(App):
    """Minimal personal-usage dashboard for Codex + Claude Code."""

    TITLE = "agentic-metric"
    CSS_PATH = "styles.tcss"
    ENABLE_COMMAND_PALETTE = False

    _VIEWS = ("today", "week", "month")

    BINDINGS = [
        Binding("left,h", "prev_view", "View", key_display="←"),
        Binding("right,l", "next_view", "View", key_display="→"),
        Binding("up,k", "back_in_time", "Earlier", key_display="↑"),
        Binding("down,j", "forward_in_time", "Later", key_display="↓"),
        Binding("period,0", "reset_offset", "Now", key_display="."),
        Binding("t", "focus('today')", "Today", show=False),
        Binding("w", "focus('week')", "Week", show=False),
        Binding("m", "focus('month')", "Month", show=False),
        Binding("r", "refresh_all", "Refresh"),
        # Two bindings on the same key; `check_action` picks whichever
        # matches the current state so the footer always shows one label.
        # The "off" variant (visible while auto-refresh is running) is
        # styled via the `-auto-on` class in styles.tcss.
        Binding("R", "auto_refresh_on", "Auto", key_display="R"),
        Binding("R", "auto_refresh_off", "Auto", key_display="R"),
        Binding("alt+c,ctrl+y", "copy_view", "Copy", key_display="Alt+C", priority=True, show=False),
        # Keep Ctrl+C from quitting; some terminals also use it while copying.
        Binding("ctrl+c", "noop", show=False, priority=True),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._db = Database()
        self._collectors: CollectorRegistry = create_default_registry()
        self._live_sessions: list[LiveSession] = []
        self._today_sessions: list[dict] = []
        self._focus: str = "today"
        self._offset: int = 0  # 0 = current period; N = N units in the past
        self._auto_refresh_timer: Timer | None = None

    # ── Layout ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="summary-row"):
            yield SummaryCell("TODAY", id="cell-today")
            yield SummaryCell("WEEK", id="cell-week")
            yield SummaryCell("MONTH", id="cell-month")
        with Vertical(id="heatmap-panel"):
            yield Static("Today by hour", id="heatmap-title")
            yield PeriodicHeatmap(id="heatmap")
            yield CostDriverSummary(id="driver-line")
        with Vertical(id="chart-panel"):
            yield Static("Trend", id="chart-title")
            yield PlotextPlot(id="chart")
        with Vertical(id="breakdown-panel"):
            yield Static("By agent × model", id="breakdown-title")
            yield Breakdown(id="breakdown-body")
        yield _AutoAwareFooter()

    def on_mount(self) -> None:
        self._today_sessions = get_today_sessions(self._db)
        self._populate_all()
        self.set_interval(LIVE_REFRESH_INTERVAL, self._tick_live)
        self.set_interval(DATA_SYNC_INTERVAL, self._tick_sync)
        self.run_worker(self._initial_sync_worker, thread=True, exclusive=True, group="sync")

    async def _initial_sync_worker(self) -> None:
        db = Database()
        try:
            self._collectors.sync_all(db)
            db.commit()
        finally:
            db.close()
        self.call_from_thread(self._on_sync_done)

    def on_unmount(self) -> None:
        if self._auto_refresh_timer is not None:
            self._auto_refresh_timer.stop()
            self._auto_refresh_timer = None
        self._db.close()

    # ── Rendering ─────────────────────────────────────────────────────

    def _populate_all(self) -> None:
        self._populate_summary()
        self._populate_heatmap()
        self._populate_chart()
        self._populate_breakdown()

    def _populate_chart_and_breakdown(self) -> None:
        """Refresh everything that depends on focus/offset (not summary)."""
        self._populate_heatmap()
        self._populate_chart()
        self._populate_breakdown()

    def _populate_heatmap(self) -> None:
        """Populate the heatmap strip for the currently focused view."""
        buckets = get_heatmap(self._db, self._focus, offset=self._offset)

        self.query_one("#heatmap", PeriodicHeatmap).update_data(
            buckets, highlight_index=None,
        )

        titles = {
            "today": "Today by hour",
            "week":  "This week by day",
            "month": "This month by week",
        }
        title = titles.get(self._focus, "")
        if self._offset > 0:
            if self._focus == "today":
                d = (datetime.now() - timedelta(days=self._offset)).date()
                title = f"{d.strftime('%b')} {d.day} by hour"
            elif self._focus == "week":
                title = f"{self._offset} week(s) ago by day"
            elif self._focus == "month":
                title = f"{self._offset} month(s) ago by week"
        self.query_one("#heatmap-title", Static).update(
            Text.from_markup(f"[bold]{title}[/]")
        )
        self._populate_driver_line()

    def _populate_driver_line(self) -> None:
        """Show the strongest cost driver for the focused period."""
        _label, frm, to = resolve_range(self._focus, offset=self._offset)
        peak_rows = get_range_by_time_model(self._db, frm, to, limit=1)
        project_rows = get_range_by_project(self._db, frm, to, limit=1)

        peak = peak_rows[0] if peak_rows else None
        project = None
        if project_rows and (
            (project_rows[0].get("estimated_cost_usd") or 0) > 0
            or _has_unknown_cost(project_rows[0])
        ):
            project = project_rows[0]

        self.query_one("#driver-line", CostDriverSummary).update_data(peak, project)

    def _populate_summary(self) -> None:
        active_count = self._count_active()
        # Sparkline config per view: (trend_unit, bucket_count)
        spark_cfg = {
            "today": ("day",   7),
            "week":  ("week",  8),
            "month": ("month", 6),
        }
        for kind, cell_id in (
            ("today", "#cell-today"),
            ("week", "#cell-week"),
            ("month", "#cell-month"),
        ):
            _, frm, to = resolve_range(kind)
            totals = get_range_totals(self._db, frm, to)
            cost = totals.get("estimated_cost_usd") or 0.0
            cost_unknown = _has_unknown_cost(totals)
            sess = totals.get("session_count") or 0
            tokens = _total_tokens(totals)

            # Previous period for delta comparison
            _, p_frm, p_to = resolve_range(kind, offset=1)
            prev = get_range_totals(self._db, p_frm, p_to)
            prev_cost = prev.get("estimated_cost_usd") or 0.0
            prev_cost_unknown = _has_unknown_cost(prev)

            # Sparkline of the last N buckets for this focus
            unit, count = spark_cfg[kind]
            trend = get_trend(self._db, unit, count)
            sparkline = [v for _, v in trend]

            cell = self.query_one(cell_id, SummaryCell)
            cell.update_data(
                cost, sess, tokens,
                active=active_count if kind == "today" else 0,
                prev_cost=prev_cost,
                sparkline=sparkline,
                cost_unknown=cost_unknown,
                prev_cost_unknown=prev_cost_unknown,
            )
            cell.set_focused(kind == self._focus)

    def _populate_chart(self) -> None:
        unit, count, span_label = _TREND_CONFIG[self._focus]
        plot_widget = self.query_one("#chart", PlotextPlot)
        plt = plot_widget.plt
        plt.clear_figure()

        data = get_trend(self._db, unit, count)
        if not data or all(c == 0 for _, c in data):
            plt.title(f"No activity in the {span_label}")
            plot_widget.refresh()
            return

        labels = [d[0] for d in data]
        ys = [d[1] for d in data]
        xs = list(range(len(data)))
        max_y = max(ys) or 1

        plt.bar(xs, ys, marker="sd", color="yellow+")
        # show ~6 ticks to avoid crowding
        step = max(1, len(xs) // 6)
        plt.xticks(xs[::step], labels[::step])
        plt.ylabel("USD")

        # Stretch the y-axis a bit so bar-top labels don't get clipped.
        plt.ylim(0, max_y * 1.18)

        # Only label bars that are tall enough relative to the chart; too
        # many labels makes it noisy.
        threshold = max_y * 0.08
        for x, y in zip(xs, ys):
            if y >= threshold:
                plt.text(_fmt_bar_label(y), x=x, y=y + max_y * 0.05,
                         alignment="center", color="cyan+")

        # Let the chart fill whatever the chart-panel gives it rather than
        # pinning a hard-coded height.
        plot_widget.refresh()

        title = self.query_one("#chart-title", Static)
        title.update(Text.from_markup(f"[bold]Trend[/] — [bright_white]{span_label}[/]"))

    def _populate_breakdown(self) -> None:
        label, frm, to = resolve_range(self._focus, offset=self._offset)
        rows = get_range_by_agent_model(self._db, frm, to)
        rows = [r for r in rows if (r["estimated_cost_usd"] or 0) > 0 or _has_unknown_cost(r)]

        groups_by_agent: dict[str, dict] = {}
        for r in rows:
            at = r["agent_type"]
            g = groups_by_agent.setdefault(at, {
                "agent": at,
                "cost": 0.0,
                "tokens": 0,
                "input": 0,
                "output": 0,
                "cache": 0,
                "unknown_cost_count": 0,
                "models": [],
            })
            model_tokens = _total_tokens(r)
            model_cache = (r.get("cache_read_tokens") or 0) + (r.get("cache_creation_tokens") or 0)
            g["cost"] += r["estimated_cost_usd"] or 0.0
            g["tokens"] += model_tokens
            g["input"] += r.get("input_tokens") or 0
            g["output"] += r.get("output_tokens") or 0
            g["cache"] += model_cache
            g["unknown_cost_count"] += r.get("unknown_cost_count") or 0
            g["models"].append({
                "model": r["model"],
                "raw_model": r.get("raw_model", ""),
                "cost": r["estimated_cost_usd"] or 0.0,
                "unknown_cost_count": r.get("unknown_cost_count") or 0,
                "tokens": model_tokens,
                "input": r.get("input_tokens") or 0,
                "output": r.get("output_tokens") or 0,
                "cache": model_cache,
            })

        groups = sorted(groups_by_agent.values(), key=lambda g: -g["cost"])

        total_cost = sum(g["cost"] for g in groups)

        title_widget = self.query_one("#breakdown-title", Static)
        title_widget.update(Text.from_markup(
            f"[bold]By agent × model[/] — [bright_cyan]{label}[/] [white]({frm} → {to})[/]"
        ))
        self.query_one("#breakdown-body", Breakdown).update_data(groups, total_cost)

    # ── Counters ──────────────────────────────────────────────────────

    def _count_active(self) -> int:
        return sum(
            1 for s in self._live_sessions
            if s.user_turns > 0 or s.output_tokens > 0
        )

    # ── Live refresh (1 s) ────────────────────────────────────────────

    def _tick_live(self) -> None:
        self.run_worker(self._live_worker, thread=True, exclusive=True, group="live")

    async def _live_worker(self) -> None:
        try:
            sessions = self._collectors.get_live_sessions()
        except Exception:
            sessions = []
        self.call_from_thread(self._on_live_update, sessions)

    def _on_live_update(self, sessions: list[LiveSession]) -> None:
        self._live_sessions = sessions
        # Only the TODAY cell's active count needs refreshing each second.
        cell = self.query_one("#cell-today", SummaryCell)
        cell.update_data(
            cell.cost, cell.sessions, cell.tokens,
            active=self._count_active(),
            prev_cost=cell.prev_cost,
            sparkline=cell.sparkline,
            cost_unknown=cell.cost_unknown,
            prev_cost_unknown=cell.prev_cost_unknown,
        )

    # ── Periodic sync (5 min) ─────────────────────────────────────────

    def _tick_sync(self) -> None:
        self.run_worker(self._sync_worker, thread=True, exclusive=True, group="sync")

    async def _sync_worker(self) -> None:
        db = Database()
        try:
            self._collectors.sync_all(db)
            db.commit()
        finally:
            db.close()
        self.call_from_thread(self._on_sync_done)

    def _on_sync_done(self) -> None:
        self._today_sessions = get_today_sessions(self._db)
        self._populate_all()

    # ── Actions ───────────────────────────────────────────────────────

    def action_focus(self, kind: str) -> None:
        if kind not in self._VIEWS:
            return
        self._focus = kind
        self._offset = 0  # switching view resets the time offset
        for k, cell_id in (
            ("today", "#cell-today"),
            ("week", "#cell-week"),
            ("month", "#cell-month"),
        ):
            self.query_one(cell_id, SummaryCell).set_focused(k == kind)
        self._populate_chart_and_breakdown()


    def action_prev_view(self) -> None:
        idx = self._VIEWS.index(self._focus)
        self.action_focus(self._VIEWS[(idx - 1) % len(self._VIEWS)])

    def action_next_view(self) -> None:
        idx = self._VIEWS.index(self._focus)
        self.action_focus(self._VIEWS[(idx + 1) % len(self._VIEWS)])

    def action_back_in_time(self) -> None:
        self._offset += 1
        self._populate_chart_and_breakdown()

    def action_forward_in_time(self) -> None:
        if self._offset > 0:
            self._offset -= 1
            self._populate_chart_and_breakdown()

    def action_reset_offset(self) -> None:
        if self._offset != 0:
            self._offset = 0
            self._populate_chart_and_breakdown()

    def action_refresh_all(self) -> None:
        self.notify("Syncing…")
        self.run_worker(self._sync_worker, thread=True, exclusive=True, group="sync")

    def action_auto_refresh_on(self) -> None:
        """Enable the fast auto-sync timer (runs in addition to the 5-min one)."""
        if self._auto_refresh_timer is not None:
            return
        self._auto_refresh_timer = self.set_interval(
            AUTO_REFRESH_INTERVAL, self._tick_sync
        )
        self._tick_sync()
        self.notify(f"Auto-refresh on — every {AUTO_REFRESH_INTERVAL}s")
        self.refresh_bindings()

    def action_auto_refresh_off(self) -> None:
        """Stop the fast auto-sync timer."""
        if self._auto_refresh_timer is None:
            return
        self._auto_refresh_timer.stop()
        self._auto_refresh_timer = None
        self.notify("Auto-refresh off")
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide whichever auto-refresh binding doesn't match current state."""
        if action == "auto_refresh_on":
            return self._auto_refresh_timer is None
        if action == "auto_refresh_off":
            return self._auto_refresh_timer is not None
        return True

    def action_noop(self) -> None:
        """Keep Ctrl+C from quitting; point people at the TUI copy key."""
        self.notify("Press [bold]q[/] to quit", severity="information")

    def action_copy_view(self) -> None:
        """Copy selected text, or fall back to a compact snapshot of the current view."""
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            self.notify("Copied selected text", severity="information")
            return

        label, frm, to = resolve_range(self._focus, offset=self._offset)
        totals = get_range_totals(self._db, frm, to)
        peak_rows = get_range_by_time_model(self._db, frm, to, limit=1)
        project_rows = get_range_by_project(self._db, frm, to, limit=1)

        lines = [f"{label}  {frm} -> {to}"]
        stats = [
            f"Cost {fmt_cost(totals.get('estimated_cost_usd'), unknown=_has_unknown_cost(totals))}",
            f"Sessions {totals.get('session_count') or 0:,}",
            f"Turns {totals.get('user_turns') or 0:,}",
            f"Tokens {fmt_tokens(_total_tokens(totals))}",
        ]
        cache_hit = _cache_hit_pct(totals)
        if cache_hit is not None:
            stats.append(f"Cache hit {cache_hit}%")
        lines.append(" | ".join(stats))

        token_parts = [
            f"Token input {fmt_tokens(totals.get('input_tokens') or 0)}",
            f"output {fmt_tokens(totals.get('output_tokens') or 0)}",
            f"cache read {fmt_tokens(totals.get('cache_read_tokens') or 0)}",
        ]
        cache_write = totals.get("cache_creation_tokens") or 0
        if cache_write:
            token_parts.append(f"cache write {fmt_tokens(cache_write)}")
        lines.append(" | ".join(token_parts))

        if peak_rows:
            peak = peak_rows[0]
            peak_model = peak["model"]
            if peak_model == "Unknown" and peak.get("raw_model"):
                peak_model = f"Unknown: {peak['raw_model']}"
            lines.append(
                "Top agent × model: "
                f"{peak['agent_type']} / {peak_model} | "
                f"{_bucket_label(peak)} | "
                f"{fmt_cost(peak.get('estimated_cost_usd'), unknown=_has_unknown_cost(peak))} | "
                f"{_split_label(peak)}"
            )

        if project_rows and (
            (project_rows[0].get("estimated_cost_usd") or 0) > 0
            or _has_unknown_cost(project_rows[0])
        ):
            project = project_rows[0]
            lines.append(
                "Top project: "
                f"{_short_path(project['project_path'])} | "
                f"{fmt_cost(project.get('estimated_cost_usd'), unknown=_has_unknown_cost(project))}"
            )

        self.copy_to_clipboard("\n".join(lines))
        self.notify("Copied current view summary", severity="information")


def _has_unknown_cost(row: dict | None) -> bool:
    return bool(row and (row.get("unknown_cost_count") or 0) > 0)
