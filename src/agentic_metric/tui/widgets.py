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


def fmt_cost(usd: float | None, *, unknown: bool = False) -> str:
    """Format a USD cost value with thousands separator."""
    if unknown or usd is None:
        return "?"
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
        self.cost_unknown = False
        self.sessions = 0
        self.tokens = 0
        self.active = 0
        self.prev_cost: float | None = None
        self.prev_cost_unknown = False
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
        cost_unknown: bool = False,
        prev_cost_unknown: bool = False,
    ) -> None:
        self.cost = cost
        self.cost_unknown = cost_unknown
        self.sessions = sessions
        self.tokens = tokens
        self.active = active
        self.prev_cost = prev_cost
        self.prev_cost_unknown = prev_cost_unknown
        if sparkline is not None:
            self.sparkline = sparkline
        self.refresh()

    def _delta(self) -> tuple[str, str] | None:
        """Return (text, style) for the delta line, or None."""
        if self.cost_unknown or self.prev_cost_unknown:
            return None
        if self.prev_cost is None:
            return None
        if self.prev_cost <= 0 and self.cost <= 0:
            return None
        if self.prev_cost <= 0:
            return ("▲ new", "bright_yellow")
        ratio = self.cost / self.prev_cost
        if abs(self.cost - self.prev_cost) < 0.01 or abs(ratio - 1.0) < 0.01:
            return ("≈ flat", "white")
        if self.cost > self.prev_cost:
            if ratio >= 10:
                return ("▲ ≫10×", "bright_red")
            pct = (ratio - 1) * 100
            return (f"▲ +{pct:.0f}%", "bright_red")
        pct = (1 - ratio) * 100
        return (f"▼ -{pct:.0f}%", "bright_green")

    def render(self) -> Text:
        # Use ANSI named colors so we inherit the terminal's palette.
        label_style = (
            "bold black on bright_yellow" if self.focused_view else "bold bright_white"
        )
        cost_style = "bold bright_yellow reverse" if self.focused_view else "bold bright_yellow"
        t = Text()
        t.append(f" {self.label} ", style=label_style)
        t.append("\n\n")
        t.append(fmt_cost(self.cost, unknown=self.cost_unknown), style=cost_style)
        # Delta line (if we have a prev period)
        delta = self._delta()
        if delta:
            t.append("  ")
            t.append(delta[0], style=delta[1])
        t.append("\n")
        # Sparkline (trend of the last N buckets for this focus)
        if self.sparkline:
            t.append(fmt_sparkline(self.sparkline), style="bright_cyan")
            t.append("\n")
        t.append(f"{self.sessions:,} sessions", style="white")
        t.append("  ")
        t.append(f"{fmt_tokens(self.tokens)} tok", style="white")
        if self.active:
            t.append("  ")
            t.append(f"● {self.active} live", style="bold bright_green")
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
            return Text("  (no data)", style="white")

        # 7-level low → hot gradient. Keeps block-character steps so
        # the strip still reads without color support, but when colors
        # are available the transition across the strip forms a
        # continuous heat gradient.
        blocks = [" ", "·", "░", "▒", "▓", "█", "█"]
        colors = [
            "default",      # 0: idle
            "bright_blue",  # 1: trace
            "bright_green", # 2: low
            "bright_cyan",  # 3: low-mid
            "bright_yellow",# 4: mid
            "bright_red",   # 5: high
            "bright_red",   # 6: peak
        ]
        max_v = max((b.get("cost") or 0) for b in self._buckets) or 1.0
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

        known_peak_idx = max(range(n), key=lambda i: buckets[i].get("cost") or 0)
        unknown_peak_idx = next((i for i, b in enumerate(buckets) if _has_unknown_cost(b)), None)
        peak_idx = known_peak_idx if (buckets[known_peak_idx].get("cost") or 0) > 0 else (
            unknown_peak_idx if unknown_peak_idx is not None else known_peak_idx
        )
        total_cost = sum(b.get("cost") or 0 for b in buckets)
        total_unknown = any(_has_unknown_cost(b) for b in buckets)
        total_tokens = sum(b.get("tokens") or 0 for b in buckets)

        for i, b in enumerate(buckets):
            ratio = (b.get("cost") or 0) / max_v
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
                row_labels.append(label.center(cell_w), style="white")
            else:
                row_labels.append(" " * cell_w, style="default")

        peak_b = buckets[peak_idx]
        summary = Text()
        summary.append("  ")
        peak_unknown = _has_unknown_cost(peak_b)
        if (peak_b.get("cost") or 0) > 0 or peak_unknown:
            summary.append("peak ", style="white")
            summary.append(peak_b["label"], style="bold")
            summary.append(f"  {fmt_cost(peak_b.get('cost'), unknown=peak_unknown)}", style="bright_yellow")
            summary.append(f"  {_fmt_tokens_shared(peak_b.get('tokens') or 0)}", style="bright_cyan")
            summary.append("    ")
        summary.append("total ", style="white")
        summary.append(fmt_cost(total_cost, unknown=total_unknown), style="bold bright_yellow")
        summary.append(f"  {_fmt_tokens_shared(total_tokens)} tokens", style="bright_cyan")

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
        self._visible_model_limit = 4

    def update_data(self, groups: list[dict], total_cost: float) -> None:
        self._groups = groups
        self._total_cost = total_cost
        self.refresh()

    def _bar(self, ratio: float, width: int = 18) -> Text:
        filled = int(round(ratio * width))
        filled = max(0, min(width, filled))
        bar = Text()
        bar.append("█" * filled, style="bold bright_cyan")
        bar.append("░" * (width - filled), style="white")
        return bar

    def _split(self, row: dict) -> str:
        return (
            f"in {fmt_tokens(row.get('input') or 0)}  "
            f"out {fmt_tokens(row.get('output') or 0)}  "
            f"cache {fmt_tokens(row.get('cache') or 0)}"
        )

    def render(self) -> Text:
        if not self._groups:
            return Text("  No activity in the selected range.", style="white")

        total = max(self._total_cost, 1e-9)
        total_unknown = any(_has_unknown_cost(g) for g in self._groups)
        t = Text()
        for i, g in enumerate(self._groups):
            agent = g["agent"]
            cost = g["cost"]
            unknown = _has_unknown_cost(g)
            ratio = cost / total
            pct = ratio * 100

            # Agent line — magenta, bold
            t.append(f"  {agent:<14}", style="bold bright_magenta")
            t.append(f" {fmt_cost(cost, unknown=unknown):>10} ", style="bold bright_yellow")
            t.append_text(self._bar(ratio))
            t.append("   — \n" if unknown or total_unknown else f" {pct:>4.1f}%\n", style="white")
            t.append("    ")
            t.append(self._split(g), style="white")
            t.append("\n")

            # Model rows: keep the panel readable, then roll up the tail.
            raw_models = g.get("models", []) or []
            nonzero = [m for m in raw_models if (m.get("cost") or 0) > 0 or _has_unknown_cost(m)]
            nonzero.sort(key=lambda m: (0 if _has_unknown_cost(m) else 1, -(m.get("cost") or 0)))
            visible = nonzero[: self._visible_model_limit]
            hidden = nonzero[self._visible_model_limit :]
            for j, m in enumerate(visible):
                last = (j == len(visible) - 1 and not hidden)
                connector = "└─" if last else "├─"
                t.append(f"    {connector} ", style="white")
                model_name = m.get("model") or "(unknown)"
                t.append(f"{model_name:<28}", style="bright_cyan")
                t.append(f" {fmt_cost(m.get('cost'), unknown=_has_unknown_cost(m)):>10}", style="bright_yellow")
                t.append(f"  {self._split(m)}\n", style="white")
            if hidden:
                hidden_cost = sum(m.get("cost") or 0 for m in hidden)
                hidden_unknown = any(_has_unknown_cost(m) for m in hidden)
                hidden_tokens = sum(m.get("tokens") or 0 for m in hidden)
                hidden_row = {
                    "input": sum(m.get("input") or 0 for m in hidden),
                    "output": sum(m.get("output") or 0 for m in hidden),
                    "cache": sum(m.get("cache") or 0 for m in hidden),
                }
                t.append("    └─ ", style="white")
                t.append(f"+{len(hidden)} more models".ljust(28), style="white")
                t.append(f" {fmt_cost(hidden_cost, unknown=hidden_unknown):>10}", style="bright_yellow")
                t.append(f"  {self._split(hidden_row)}")
                t.append(f"  total {fmt_tokens(hidden_tokens)}\n", style="white")
            t.append("\n")

        return t


def _has_unknown_cost(row: dict | None) -> bool:
    return bool(row and (row.get("unknown_cost_count") or 0) > 0)
