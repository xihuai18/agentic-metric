"""Custom widgets for the Agentic Metric TUI."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.widgets import Static


# ── Formatting helpers ────────────────────────────────────────────────


def fmt_tokens(n: int) -> str:
    """Compact token count: 1234 → 1.2K, 1234567 → 1.2M, 1.2B."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_cost(usd: float) -> str:
    """Format a USD cost value with thousands separator."""
    if usd >= 1.0:
        return f"${usd:,.2f}"
    return f"${usd:.3f}"


_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def fmt_sparkline(values: list[float]) -> str:
    """Compress a sequence of numbers into a unicode sparkline string.

    Zero stays as a blank; other values are mapped across ▁-█ by
    relative height. Returns an empty string for an empty list.
    """
    if not values:
        return ""
    max_v = max(values)
    if max_v <= 0:
        return " " * len(values)
    out = []
    for v in values:
        if v <= 0:
            out.append(" ")
            continue
        idx = int(round((v / max_v) * (len(_SPARK_BLOCKS) - 1)))
        idx = max(0, min(len(_SPARK_BLOCKS) - 1, idx))
        out.append(_SPARK_BLOCKS[idx])
    return "".join(out)


def ts_to_local(ts: str) -> str:
    """Convert an ISO-8601 timestamp to a short local-time string.

    Shows ``HH:MM`` for today, ``MM-DD HH:MM`` for other days.
    """
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        if dt.date() == datetime.now().astimezone().date():
            return dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts[:16]


# ── Widgets ───────────────────────────────────────────────────────────


class SummaryCell(Static):
    """One column in the top summary row: TODAY / WEEK / MONTH."""

    def __init__(self, label: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.label = label
        self.cost = 0.0
        self.sessions = 0
        self.tokens = 0
        self.active = 0
        self.prev_cost: float | None = None
        self.sparkline: list[float] = []
        self.focused_view = False

    def set_focused(self, focused: bool) -> None:
        self.focused_view = focused
        if focused:
            self.add_class("-focused")
        else:
            self.remove_class("-focused")
        self.refresh()

    def update_data(
        self, cost: float, sessions: int, tokens: int,
        active: int = 0, prev_cost: float | None = None,
        sparkline: list[float] | None = None,
    ) -> None:
        self.cost = cost
        self.sessions = sessions
        self.tokens = tokens
        self.active = active
        self.prev_cost = prev_cost
        if sparkline is not None:
            self.sparkline = sparkline
        self.refresh()

    def _delta(self) -> tuple[str, str] | None:
        """Return (text, style) for the delta line, or None."""
        if self.prev_cost is None:
            return None
        if self.prev_cost <= 0 and self.cost <= 0:
            return None
        if self.prev_cost <= 0:
            return ("▲ new", "yellow")
        ratio = self.cost / self.prev_cost
        if abs(self.cost - self.prev_cost) < 0.01 or abs(ratio - 1.0) < 0.01:
            return ("≈ flat", "bright_black")
        if self.cost > self.prev_cost:
            if ratio >= 10:
                return ("▲ ≫10×", "red")
            pct = (ratio - 1) * 100
            return (f"▲ +{pct:.0f}%", "red")
        pct = (1 - ratio) * 100
        return (f"▼ -{pct:.0f}%", "green")

    def render(self) -> Text:
        # Use ANSI named colors so we inherit the terminal's palette.
        label_style = (
            "bold black on yellow" if self.focused_view else "bold bright_black"
        )
        cost_style = "bold yellow reverse" if self.focused_view else "bold yellow"
        t = Text()
        t.append(f" {self.label} ", style=label_style)
        t.append("\n\n")
        t.append(fmt_cost(self.cost), style=cost_style)
        # Delta line (if we have a prev period)
        delta = self._delta()
        if delta:
            t.append("  ")
            t.append(delta[0], style=delta[1])
        t.append("\n")
        # Sparkline (trend of the last N buckets for this focus)
        if self.sparkline:
            t.append(fmt_sparkline(self.sparkline), style="cyan")
            t.append("\n")
        t.append(f"{self.sessions:,} sessions", style="bright_black")
        t.append("  ")
        t.append(f"{fmt_tokens(self.tokens)} tok", style="bright_black")
        if self.active:
            t.append("  ")
            t.append(f"● {self.active} live", style="bold green")
        return t


class PeriodicHeatmap(Static):
    """Heatmap strip that renders N buckets across three lines.

    The input is a list of ``{"label", "cost", "tokens", ...}`` dicts
    returned by ``aggregator.get_heatmap``. The widget renders:

        line 1: colored block per bucket, shaded by relative cost
        line 2: axis labels aligned to each bucket
        line 3: one-line summary — peak bucket, total cost, total tokens

    ``highlight_index`` marks the "current" bucket (e.g. current hour
    for today, today's weekday for week) with a reverse style. Pass
    ``None`` to disable.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._buckets: list[dict] = []
        self._highlight: int | None = None

    def update_data(
        self,
        buckets: list[dict],
        highlight_index: int | None = None,
    ) -> None:
        self._buckets = buckets
        self._highlight = highlight_index
        self.refresh()

    def render(self) -> Text:
        if not self._buckets:
            return Text("  (no data)", style="bright_black")

        # 7-level low → hot gradient. Keeps block-character steps so
        # the strip still reads without color support, but when colors
        # are available the transition across the strip forms a
        # continuous heat gradient.
        blocks = [" ", "·", "░", "▒", "▓", "█", "█"]
        colors = [
            "default",      # 0: idle
            "bright_black", # 1: trace
            "green",        # 2: low
            "bright_green", # 3: low-mid
            "yellow",       # 4: mid
            "red",          # 5: high
            "bright_red",   # 6: peak
        ]
        max_v = max(b["cost"] for b in self._buckets) or 1.0
        levels = len(blocks)

        n = len(buckets := self._buckets)
        # Per-bucket cell width — blocks fill the whole cell so the strip
        # reads as one continuous heat gradient; labels get centred
        # underneath.
        if n >= 20:
            cell_w = 4
            label_every = 3
        elif n >= 10:
            cell_w = 6
            label_every = 1
        elif n >= 6:
            cell_w = 8
            label_every = 1
        else:
            cell_w = 12
            label_every = 1
        try:
            available = max(12, self.size.width - 2)
            cell_w = min(cell_w, max(2, available // max(n, 1)))
        except Exception:
            pass

        row_blocks = Text()
        row_labels = Text()
        row_blocks.append(" ")
        row_labels.append(" ")

        peak_idx = max(range(n), key=lambda i: buckets[i]["cost"])
        total_cost = sum(b["cost"] for b in buckets)
        total_tokens = sum(b["tokens"] for b in buckets)

        for i, b in enumerate(buckets):
            ratio = b["cost"] / max_v
            lvl = min(levels - 1, int(round(ratio * (levels - 1))))
            block = blocks[lvl]
            style = colors[lvl]
            if i == self._highlight:
                style = f"bold {style} reverse"

            # Fill the whole cell with the block char — no inter-bucket
            # spacing, so adjacent buckets form a continuous strip.
            row_blocks.append(block * cell_w, style=style)

            if i % label_every == 0:
                label = b["label"][:cell_w]
                row_labels.append(label.center(cell_w), style="bright_black")
            else:
                row_labels.append(" " * cell_w, style="default")

        peak_b = buckets[peak_idx]
        summary = Text()
        summary.append("  ")
        if peak_b["cost"] > 0:
            summary.append("peak ", style="bright_black")
            summary.append(peak_b["label"], style="bold")
            summary.append(f"  ${peak_b['cost']:,.2f}", style="yellow")
            summary.append(f"  {_fmt_tokens_shared(peak_b['tokens'])}", style="cyan")
            summary.append("    ")
        summary.append("total ", style="bright_black")
        summary.append(f"${total_cost:,.2f}", style="bold yellow")
        summary.append(f"  {_fmt_tokens_shared(total_tokens)} tokens", style="cyan")

        t = Text()
        t.append_text(row_blocks)
        t.append("\n")
        t.append_text(row_labels)
        t.append("\n")
        t.append_text(summary)
        return t


# Keep the old name as an alias for backwards compatibility
HourHeatmap = PeriodicHeatmap


def _fmt_tokens_shared(n: int) -> str:
    return fmt_tokens(n)


class Breakdown(Static):
    """Agent × model nested breakdown with cost bars.

    Data shape::

        [
            {
                "agent": "claude_code",
                "cost": 1234.56,
                "tokens": ...,
                "models": [
                    {"model": "claude-opus-4-7", "cost": 800.00, "tokens": ...},
                    ...
                ]
            },
            ...
        ]
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._groups: list[dict] = []
        self._total_cost: float = 0.0

    def update_data(self, groups: list[dict], total_cost: float) -> None:
        self._groups = groups
        self._total_cost = total_cost
        self.refresh()

    def _bar(self, ratio: float, width: int = 18) -> Text:
        filled = int(round(ratio * width))
        filled = max(0, min(width, filled))
        bar = Text()
        bar.append("█" * filled, style="bold cyan")
        bar.append("░" * (width - filled), style="bright_black")
        return bar

    def _split(self, row: dict) -> str:
        return (
            f"in {fmt_tokens(row.get('input') or 0)}  "
            f"out {fmt_tokens(row.get('output') or 0)}  "
            f"cache {fmt_tokens(row.get('cache') or 0)}"
        )

    def render(self) -> Text:
        if not self._groups:
            return Text("  No activity in the selected range.", style="bright_black")

        total = max(self._total_cost, 1e-9)
        t = Text()
        for i, g in enumerate(self._groups):
            agent = g["agent"]
            cost = g["cost"]
            ratio = cost / total
            pct = ratio * 100

            # Agent line — magenta, bold
            t.append(f"  {agent:<14}", style="bold magenta")
            t.append(f" {fmt_cost(cost):>10} ", style="bold yellow")
            t.append_text(self._bar(ratio))
            t.append(f" {pct:>4.1f}%\n", style="bright_black")
            t.append("    ")
            t.append(self._split(g), style="bright_black")
            t.append("\n")

            # Model rows: keep the panel readable, then roll up the tail.
            raw_models = g.get("models", []) or []
            nonzero = [m for m in raw_models if (m.get("cost") or 0) > 0]
            visible = nonzero[:6]
            hidden = nonzero[6:]
            for j, m in enumerate(visible):
                last = (j == len(visible) - 1 and not hidden)
                connector = "└─" if last else "├─"
                t.append(f"    {connector} ", style="bright_black")
                model_name = m.get("model") or "(unknown)"
                t.append(f"{model_name:<28}", style="cyan")
                t.append(f" {fmt_cost(m['cost']):>10}", style="yellow")
                t.append(f"  {self._split(m)}\n", style="bright_black")
            if hidden:
                hidden_cost = sum(m.get("cost") or 0 for m in hidden)
                hidden_tokens = sum(m.get("tokens") or 0 for m in hidden)
                hidden_row = {
                    "input": sum(m.get("input") or 0 for m in hidden),
                    "output": sum(m.get("output") or 0 for m in hidden),
                    "cache": sum(m.get("cache") or 0 for m in hidden),
                }
                t.append("    └─ ", style="bright_black")
                t.append(f"+{len(hidden)} more models".ljust(28), style="bright_black")
                t.append(f" {fmt_cost(hidden_cost):>10}", style="yellow")
                t.append(f"  {self._split(hidden_row)}")
                t.append(f"  total {fmt_tokens(hidden_tokens)}\n", style="bright_black")
            t.append("\n")

        return t
