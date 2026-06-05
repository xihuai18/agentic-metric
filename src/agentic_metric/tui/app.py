"""Textual TUI application for agentic-metric."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Footer, Header
from textual.widgets._footer import FooterKey

from ..collectors import CollectorRegistry, create_default_registry
from ..config import AUTO_REFRESH_INTERVAL, DATA_SYNC_INTERVAL, LIVE_REFRESH_INTERVAL
from ..formatting import cache_hit_rate as _cache_hit_rate
from ..formatting import source_label as _source_label
from ..models import LiveSession
from ..store.aggregator import (
    get_heatmap,
    get_range_by_agent_model,
    get_range_by_project,
    get_range_totals,
    get_today_sessions,
    get_trend,
    resolve_range,
)
from ..store.database import Database
from .widgets import Breakdown, PeriodicHeatmap, SummaryCell, TrendBlocks
from .pricing_screen import PricingScreen
from .help_screen import HelpScreen


def _total_tokens(d: dict) -> int:
    return (
        (d.get("input_tokens") or 0)
        + (d.get("output_tokens") or 0)
        + (d.get("cache_read_tokens") or 0)
        + (d.get("cache_creation_tokens") or 0)
    )


def _cache_hit_pct(d: dict) -> int | None:
    pct = _cache_hit_rate(d)
    if pct < 0:
        return None
    return round(pct)


def _summary_label(kind: str, range_label: str, offset: int) -> str:
    """Return the compact label shown in a top summary card."""
    if offset <= 0:
        return kind.upper()
    if offset == 1:
        return range_label.upper()
    unit = {
        "today": "DAYS",
        "week": "WEEKS",
        "month": "MONTHS",
    }.get(kind, "PERIODS")
    return f"{offset} {unit} AGO"


# Trend configuration per focused view (long-range chart only; the
# today hour heatmap is rendered separately).
_TREND_CONFIG = {
    "today": ("day",   14, "last 14 days"),
    "week":  ("week",  12, "last 12 weeks"),
    "month": ("month", 12, "last 12 months"),
}


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


class AgenticMetricApp(App):
    """Minimal personal-usage dashboard for Codex + Claude Code."""

    TITLE = "agentic-metric"
    CSS_PATH = "styles.tcss"
    ENABLE_COMMAND_PALETTE = False

    _VIEWS = ("today", "week", "month")

    BINDINGS = [
        # ── Navigation ── paired keys collapse to one footer entry; the
        # second key in each pair stays bound but hidden so both still work.
        # ←→ switch view (today/week/month); PgUp/PgDn step the time range
        # (PgUp = previous period); ↑↓ scroll the breakdown panel.
        Binding("left,h", "prev_view", "View", key_display="←→"),
        Binding("right,l", "next_view", "View", show=False),
        Binding("pageup", "back_in_time", "Range", key_display="PgUp/PgDn", priority=True),
        Binding("pagedown", "forward_in_time", "Range", show=False, priority=True),
        Binding("up,k", "scroll_breakdown_up", show=False),
        Binding("down,j", "scroll_breakdown_down", show=False),
        Binding("ctrl+b", "scroll_breakdown_up", show=False),
        Binding("ctrl+f", "scroll_breakdown_down", show=False),
        Binding("period,0", "reset_offset", "Now", key_display="."),
        Binding("t", "focus('today')", "Today", show=False),
        Binding("w", "focus('week')", "Week", show=False),
        Binding("m", "focus('month')", "Month", show=False),
        # ── Data ── R = fast "live" sync (auto-sync already runs every
        # 5 min by default, so this just speeds it up). Two
        # bindings share R; `check_action` shows whichever matches state,
        # and the active one is highlighted via `-auto-on` in styles.tcss.
        Binding("R", "auto_refresh_on", "Auto", key_display="R"),
        Binding("R", "auto_refresh_off", "Auto", key_display="R"),
        # ── Other ──
        Binding("p", "show_pricing", "Pricing"),
        Binding("question_mark,?", "show_help", "Help", key_display="?"),
        # Keep Ctrl+C from quitting so a stray copy shortcut doesn't kill the app.
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
        self._sync_timer: Timer | None = None

    # ── Layout ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="summary-row"):
            yield SummaryCell("TODAY", id="cell-today")
            yield SummaryCell("WEEK", id="cell-week")
            yield SummaryCell("MONTH", id="cell-month")
        with Vertical(id="heatmap-panel"):
            yield PeriodicHeatmap(id="heatmap")
        with Vertical(id="chart-panel"):
            yield TrendBlocks(id="chart")
        with Vertical(id="breakdown-panel"):
            with VerticalScroll(id="breakdown-scroll"):
                yield Breakdown(id="breakdown-body")
        yield _AutoAwareFooter()

    def on_mount(self) -> None:
        self._today_sessions = get_today_sessions(self._db)
        self.sub_title = "syncing…"
        self._populate_all()
        self.set_interval(LIVE_REFRESH_INTERVAL, self._tick_live)
        self._sync_timer = self.set_interval(DATA_SYNC_INTERVAL, self._tick_sync)
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

        label, frm, to = resolve_range(self._focus, offset=self._offset)
        totals = get_range_totals(self._db, frm, to)
        project_rows = get_range_by_project(self._db, frm, to, limit=3)

        self.query_one("#heatmap", PeriodicHeatmap).update_data(
            buckets,
            highlight_index=None,
            totals=totals,
            projects=project_rows,
            total_cost=totals.get("estimated_cost_usd") or 0.0,
        )

        titles = {
            "today": "Today by hour",
            "week":  "This week by day",
            "month": "This month by week",
        }
        title = titles.get(self._focus, "")
        if self._offset > 0:
            if self._focus == "today":
                title = f"{label} by hour"
            elif self._focus == "week":
                title = f"{label} by day"
            elif self._focus == "month":
                title = f"{label} by week"
        self.query_one("#heatmap-panel", Vertical).border_title = title

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
            cell_offset = self._offset if kind == self._focus else 0
            label, frm, to = resolve_range(kind, offset=cell_offset)
            totals = get_range_totals(self._db, frm, to)
            cost = totals.get("estimated_cost_usd") or 0.0
            cost_unknown = _has_unknown_cost(totals)
            sess = totals.get("session_count") or 0
            turns = totals.get("user_turns") or 0
            msgs = totals.get("message_count") or 0
            requests = max(0, msgs - turns)
            tokens = _total_tokens(totals)
            cache_pct = _cache_hit_pct(totals)

            # Previous period for delta comparison
            _, p_frm, p_to = resolve_range(kind, offset=cell_offset + 1)
            prev = get_range_totals(self._db, p_frm, p_to)
            prev_cost = prev.get("estimated_cost_usd") or 0.0
            prev_cost_unknown = _has_unknown_cost(prev)

            # Sparkline of the last N buckets for this focus
            unit, count = spark_cfg[kind]
            trend = get_trend(self._db, unit, count)
            sparkline = [v for _, v in trend]

            cell = self.query_one(cell_id, SummaryCell)
            cell.label = _summary_label(kind, label, cell_offset)
            cell.update_data(
                cost, sess, tokens,
                active=active_count if kind == "today" and cell_offset == 0 else 0,
                prev_cost=prev_cost,
                sparkline=[] if cell_offset else sparkline,
                cost_unknown=cost_unknown,
                prev_cost_unknown=prev_cost_unknown,
                turns=turns,
                requests=requests,
                cache_pct=cache_pct,
            )
            cell.set_focused(kind == self._focus)

    def _populate_chart(self) -> None:
        unit, count, span_label = _TREND_CONFIG[self._focus]
        data = get_trend(self._db, unit, count)
        self.query_one("#chart", TrendBlocks).update_data(data, span_label)

        # Title sits on the panel border; keep the unit + span there so the
        # block strip can stay three lines tall.
        self.query_one("#chart-panel", Vertical).border_title = (
            f"Trend · USD — {span_label}"
        )

    def _populate_breakdown(self) -> None:
        label, frm, to = resolve_range(self._focus, offset=self._offset)
        rows = get_range_by_agent_model(self._db, frm, to)
        rows = [r for r in rows if (r["estimated_cost_usd"] or 0) > 0 or _has_unknown_cost(r)]

        # host (machine / source) is the top level so remote SSH aggregation
        # reads as "which machine spent what"; agent → provider → model nest
        # below it. A single host is folded away by the renderer.
        hosts_by_name: dict[str, dict] = {}
        for r in rows:
            at = r["agent_type"]
            provider = r.get("provider") or ""
            data_root = r.get("data_root") or ""
            source = _source_label(data_root)

            host = hosts_by_name.setdefault(source, {
                "host": source,
                "cost": 0.0,
                "tokens": 0,
                "input": 0,
                "output": 0,
                "cache": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unknown_cost_count": 0,
                "agents": {},
            })

            agent = host["agents"].setdefault(at, {
                "agent": at,
                "cost": 0.0,
                "tokens": 0,
                "input": 0,
                "output": 0,
                "cache": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unknown_cost_count": 0,
                "providers": {},
            })

            provider_key = (provider, data_root)
            g = agent["providers"].setdefault(provider_key, {
                "provider": provider,
                "data_root": data_root,
                "cost": 0.0,
                "tokens": 0,
                "input": 0,
                "output": 0,
                "cache": 0,
                "cache_read": 0,
                "cache_write": 0,
                "unknown_cost_count": 0,
                "models": [],
            })

            model_tokens = _total_tokens(r)
            cache_read = r.get("cache_read_tokens") or 0
            cache_write = r.get("cache_creation_tokens") or 0
            model_cache = cache_read + cache_write
            cost = r["estimated_cost_usd"] or 0.0
            input_tokens = r.get("input_tokens") or 0
            output_tokens = r.get("output_tokens") or 0
            unknown_count = r.get("unknown_cost_count") or 0

            for bucket in (host, agent, g):
                bucket["cost"] += cost
                bucket["tokens"] += model_tokens
                bucket["input"] += input_tokens
                bucket["output"] += output_tokens
                bucket["cache"] += model_cache
                bucket["cache_read"] += cache_read
                bucket["cache_write"] += cache_write
                bucket["unknown_cost_count"] += unknown_count

            g["models"].append({
                "model": r["model"],
                "raw_model": r.get("raw_model", ""),
                "cost": cost,
                "unknown_cost_count": unknown_count,
                "tokens": model_tokens,
                "input": input_tokens,
                "output": output_tokens,
                "cache": model_cache,
                "cache_read": cache_read,
                "cache_write": cache_write,
            })

        groups = []
        for host in hosts_by_name.values():
            agents = []
            for agent in host["agents"].values():
                providers = sorted(agent.pop("providers").values(), key=lambda g: -g["cost"])
                for provider_group in providers:
                    provider_group["models"].sort(
                        key=lambda m: (
                            1 if _has_unknown_cost(m) else 0,
                            m.get("cost") or 0,
                        ),
                        reverse=True,
                    )
                agent["providers"] = providers
                agents.append(agent)
            agents.sort(key=lambda a: -a["cost"])
            host["agents"] = agents
            groups.append(host)
        groups.sort(key=lambda h: -h["cost"])

        total_cost = sum(h["cost"] for h in groups)

        self.query_one("#breakdown-panel", Vertical).border_title = (
            f"By host → agent → provider → model — {label} ({frm} → {to})"
        )
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
        if self._focus == "today" and self._offset > 0:
            return
        cell = self.query_one("#cell-today", SummaryCell)
        cell.update_data(
            cell.cost, cell.sessions, cell.tokens,
            active=self._count_active(),
            prev_cost=cell.prev_cost,
            sparkline=cell.sparkline,
            cost_unknown=cell.cost_unknown,
            prev_cost_unknown=cell.prev_cost_unknown,
            turns=cell.turns,
            requests=cell.requests,
            cache_pct=cell.cache_pct,
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
        sync_errors = self._collectors.get_sync_errors()
        suffix = f" · {len(sync_errors)} remote skipped" if sync_errors else ""
        self.sub_title = f"synced {datetime.now().strftime('%H:%M:%S')}{suffix}"
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
        self._populate_all()


    def action_prev_view(self) -> None:
        idx = self._VIEWS.index(self._focus)
        self.action_focus(self._VIEWS[(idx - 1) % len(self._VIEWS)])

    def action_next_view(self) -> None:
        idx = self._VIEWS.index(self._focus)
        self.action_focus(self._VIEWS[(idx + 1) % len(self._VIEWS)])

    def action_back_in_time(self) -> None:
        self._offset += 1
        self._populate_all()

    def action_forward_in_time(self) -> None:
        if self._offset > 0:
            self._offset -= 1
            self._populate_all()

    def action_reset_offset(self) -> None:
        if self._offset != 0:
            self._offset = 0
            self._populate_all()

    def action_scroll_breakdown_up(self) -> None:
        self.query_one("#breakdown-scroll", VerticalScroll).scroll_page_up(animate=False)

    def action_scroll_breakdown_down(self) -> None:
        self.query_one("#breakdown-scroll", VerticalScroll).scroll_page_down(animate=False)

    def action_show_pricing(self) -> None:
        """Open the read-only pricing view, flagging unknown models in range."""
        _label, frm, to = resolve_range(self._focus, offset=self._offset)
        rows = get_range_by_agent_model(self._db, frm, to)
        unknown: list[str] = []
        for r in rows:
            if not _has_unknown_cost(r):
                continue
            name = (r.get("raw_model") or r.get("model") or "").strip()
            if name and name not in unknown:
                unknown.append(name)
        self.push_screen(PricingScreen(unknown))

    def action_show_help(self) -> None:
        """Open the keybinding cheatsheet."""
        self.push_screen(HelpScreen())

    def action_auto_refresh_on(self) -> None:
        """Enable fast auto-sync and pause the slow periodic one (single loop)."""
        if self._auto_refresh_timer is not None:
            return
        if self._sync_timer is not None:
            self._sync_timer.pause()
        self._auto_refresh_timer = self.set_interval(
            AUTO_REFRESH_INTERVAL, self._tick_sync
        )
        self._tick_sync()
        self.notify(f"Auto-refresh on — every {AUTO_REFRESH_INTERVAL}s")
        self.refresh_bindings()

    def action_auto_refresh_off(self) -> None:
        """Stop the fast auto-sync timer and resume the slow periodic one."""
        if self._auto_refresh_timer is None:
            return
        self._auto_refresh_timer.stop()
        self._auto_refresh_timer = None
        if self._sync_timer is not None:
            self._sync_timer.resume()
        self.notify("Auto-refresh off")
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide whichever auto-refresh binding doesn't match current state."""
        if action == "auto_refresh_on":
            return self._auto_refresh_timer is None
        if action == "auto_refresh_off":
            return self._auto_refresh_timer is not None
        # PgUp/PgDn step the range with priority=True so the scrollable
        # breakdown can't swallow them. Disable that priority while a modal
        # (help/pricing) is on top, so the key reaches the modal instead.
        if action in ("back_in_time", "forward_in_time"):
            return len(self.screen_stack) <= 1
        return True

    def action_noop(self) -> None:
        """Swallow Ctrl+C so it doesn't quit; point people at the quit key."""
        self.notify("Press [bold]q[/] to quit", severity="information")


def _has_unknown_cost(row: dict | None) -> bool:
    return bool(row and (row.get("unknown_cost_count") or 0) > 0)
