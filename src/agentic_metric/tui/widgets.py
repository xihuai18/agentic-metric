"""Custom widgets for the Agentic Metric TUI."""

from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.text import Text
from textual.widgets import Static

from ..formatting import cache_hit_rate as _cache_hit_rate
from ..formatting import root_label as _root_label
from ..formatting import source_prefixed_path as _source_prefixed_path


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
    usd_value = 0.0 if usd is None else usd
    if unknown and usd_value > 0:
        return f"{fmt_cost(usd_value)} + ?"
    if unknown or usd is None:
        return "?"
    if usd_value >= 1.0:
        return f"${usd_value:,.2f}"
    return f"${usd_value:.3f}"


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


def _cache_hit_pct(row: dict) -> int | None:
    input_tokens = row.get("input_tokens")
    if input_tokens is None:
        input_tokens = row.get("input") or 0

    cache_read = row.get("cache_read_tokens")
    if cache_read is None:
        cache_read = row.get("cache_read")
    if cache_read is None:
        cache_read = row.get("cache") or 0

    cache_write = row.get("cache_creation_tokens")
    if cache_write is None:
        cache_write = row.get("cache_write") or 0

    denom = (input_tokens or 0) + (cache_read or 0) + (cache_write or 0)
    if denom <= 0:
        return None
    return round((cache_read or 0) / denom * 100)


# ── Widgets ───────────────────────────────────────────────────────────


class SummaryCell(Static):
    """One column in the top summary row: TODAY / WEEK / MONTH."""

    def __init__(self, label: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.label = label
        self.cost = 0.0
        self.cost_unknown = False
        self.sessions = 0
        self.turns = 0
        self.requests = 0
        self.tokens = 0
        self.cache_pct: int | None = None
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
        turns: int = 0,
        requests: int = 0,
        cache_pct: int | None = None,
    ) -> None:
        self.cost = cost
        self.cost_unknown = cost_unknown
        self.sessions = sessions
        self.turns = turns
        self.requests = requests
        self.tokens = tokens
        self.cache_pct = cache_pct
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
        if self.cache_pct is not None:
            t.append("Cache % ", style="white")
            t.append(f"{self.cache_pct}%", style="bold bright_green")
            t.append("\n")
        # Sparkline (trend of the last N buckets for this focus)
        if self.sparkline:
            t.append(fmt_sparkline(self.sparkline), style="bright_cyan")
            t.append("\n")
        # Sessions / requests / turns — inline if they fit, stacked otherwise.
        parts = [f"{self.sessions:,} sess", f"{self.requests:,} req", f"{self.turns:,} turns"]
        inline = " · ".join(parts)
        live_str = f"● {self.active} live" if self.active else ""
        # content_size.width accounts for padding; fall back to 30 if unknown.
        try:
            avail = self.content_size.width
        except Exception:
            avail = 30
        need = len(inline) + (2 + len(live_str) if live_str else 0)
        if avail >= need:
            t.append(inline, style="white")
            if live_str:
                t.append("  ")
                t.append(live_str, style="bold bright_green")
        else:
            t.append(parts[0], style="white")
            t.append(" · ", style="white")
            t.append(parts[1], style="white")
            t.append(" · ", style="white")
            t.append(parts[2], style="white")
            if live_str:
                t.append("\n")
                t.append(live_str, style="bold bright_green")
        return t


class PeriodicHeatmap(Static):
    """Heatmap panel body.

    Renders (top to bottom):
        - token split line (input · output · cache read · cache write)
        - heatmap colored blocks + axis labels
        - peak bucket summary (``peak <label>  <cost>  <tokens>``)
        - top 3 projects (``Top projects  <path> · $X (pct)``)

    ``highlight_index`` marks a "current" bucket with a reverse style.
    Pass ``None`` to disable.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._buckets: list[dict] = []
        self._highlight: int | None = None
        self._totals: dict = {}
        self._projects: list[dict] = []
        self._total_cost: float = 0.0

    def update_data(
        self,
        buckets: list[dict],
        highlight_index: int | None = None,
        totals: dict | None = None,
        projects: list[dict] | None = None,
        total_cost: float = 0.0,
    ) -> None:
        self._buckets = buckets
        self._highlight = highlight_index
        self._totals = totals or {}
        self._projects = projects or []
        self._total_cost = total_cost
        self.refresh()

    def render(self) -> Group | Text:
        if not self._buckets:
            return Text("  (no data)", style="white")

        # 7-level single-hue gradient. We keep one green family and vary
        # density / intensity so the strip reads cleanly in terminals
        # without turning into a rainbow.
        blocks = ["·", "•", "░", "▒", "▓", "█", "█"]
        colors = [
            "grey35",            # 0: idle
            "dim green",         # 1: trace
            "green",             # 2: low
            "green",             # 3: low-mid
            "bright_green",      # 4: mid
            "bright_green",      # 5: high
            "bold bright_green", # 6: peak
        ]
        max_v = max((b.get("cost") or 0) for b in self._buckets) or 1.0
        levels = len(blocks)

        n = len(buckets := self._buckets)
        if n >= 20:
            preferred_cell_w = 4
            label_every = 3
        elif n >= 10:
            preferred_cell_w = 6
            label_every = 1
        elif n >= 6:
            preferred_cell_w = 8
            label_every = 1
        else:
            preferred_cell_w = 12
            label_every = 1
        try:
            available = max(1, self.size.width - 2)
        except Exception:
            available = max(1, preferred_cell_w * max(n, 1))

        min_cell_w = 2 if available >= 2 else 1
        if n and n * min_cell_w <= available:
            cell_w = min(preferred_cell_w, max(min_cell_w, available // n))
            buckets_per_row = n
        else:
            cell_w = min_cell_w
            buckets_per_row = max(1, available // max(cell_w, 1))

        known_peak_idx = max(range(n), key=lambda i: buckets[i].get("cost") or 0)
        unknown_peak_idx = next((i for i, b in enumerate(buckets) if _has_unknown_cost(b)), None)
        peak_idx = known_peak_idx if (buckets[known_peak_idx].get("cost") or 0) > 0 else (
            unknown_peak_idx if unknown_peak_idx is not None else known_peak_idx
        )

        rows: list[Text] = []
        for start in range(0, n, buckets_per_row):
            chunk = buckets[start : start + buckets_per_row]
            row_blocks = Text(" ")
            row_labels = Text(" ")
            for offset, b in enumerate(chunk):
                i = start + offset
                ratio = (b.get("cost") or 0) / max_v
                lvl = min(levels - 1, int(round(ratio * (levels - 1))))
                block = blocks[lvl]
                style = colors[lvl]
                if i == self._highlight:
                    style = f"{style} reverse"

                row_blocks.append(block * cell_w, style=style)

                if i % label_every == 0:
                    label = b["label"][:cell_w]
                    row_labels.append(label.center(cell_w), style="white")
                else:
                    row_labels.append(" " * cell_w, style="default")
            rows.extend([row_blocks, row_labels])

        peak_b = buckets[peak_idx]
        peak_unknown = _has_unknown_cost(peak_b)
        peak_line = Text("  ")
        if (peak_b.get("cost") or 0) > 0 or peak_unknown:
            peak_line.append("peak ", style="white")
            peak_line.append(peak_b["label"], style="bold")
            peak_line.append(
                f"  {fmt_cost(peak_b.get('cost'), unknown=peak_unknown)}",
                style="bright_yellow",
            )
            peak_line.append(
                f"  {fmt_tokens(peak_b.get('tokens') or 0)}",
                style="bright_cyan",
            )
        else:
            peak_line.append("peak —", style="white")

        body: list[Text] = []

        tsummary = _token_summary_block(self._totals)
        if tsummary is not None:
            body.append(tsummary)
            body.append(Text(""))

        body.extend(rows)
        body.append(peak_line)

        projects_block = _top_projects_block(
            self._projects,
            self._total_cost,
            total_unknown=_has_unknown_cost(self._totals),
        )
        if projects_block is not None:
            body.append(Text(""))
            body.extend(projects_block)

        return Group(*body)


class TrendBlocks(Static):
    """Compact cost trend rendered as colored blocks."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._data: list[tuple[str, float]] = []
        self._span_label = ""

    def update_data(self, data: list[tuple[str, float]], span_label: str) -> None:
        self._data = data
        self._span_label = span_label
        self.refresh()

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        if width <= 0:
            return ""
        if len(text) <= width:
            return text
        if width <= 1:
            return text[:width]
        return text[: width - 1] + "…"

    @staticmethod
    def _fit_points(data: list[tuple[str, float]], max_points: int) -> list[tuple[str, float]]:
        """Collapse older buckets only when the terminal is too narrow."""
        if max_points <= 0:
            return []
        if len(data) <= max_points:
            return data

        bucket_w = len(data) / max_points
        fitted: list[tuple[str, float]] = []
        for i in range(max_points):
            start = int(i * bucket_w)
            end = int((i + 1) * bucket_w)
            if i == max_points - 1:
                end = len(data)
            chunk = data[start:max(end, start + 1)]
            label = chunk[-1][0]
            fitted.append((label, sum(v for _label, v in chunk)))
        return fitted

    def _axis(self, width: int, labels: list[str]) -> str:
        if width <= 0 or not labels:
            return ""
        left = self._truncate(labels[0], max(1, width))
        right = self._truncate(labels[-1], max(1, width))
        mid = self._truncate(labels[len(labels) // 2], max(1, width))

        chars = [" "] * width
        right_start = max(0, width - len(right))
        for i, ch in enumerate(left[:width]):
            chars[i] = ch
        if right_start >= len(left) + 2:
            for i, ch in enumerate(right):
                chars[right_start + i] = ch

        mid_start = max(0, width // 2 - len(mid) // 2)
        mid_end = mid_start + len(mid)
        if mid_start >= len(left) + 2 and mid_end <= right_start - 2:
            for i, ch in enumerate(mid):
                chars[mid_start + i] = ch
        return "".join(chars).rstrip()

    def render(self) -> Group | Text:
        if not self._data:
            return Text(f"  No activity in the {self._span_label}.", style="white")

        values = [cost for _label, cost in self._data]
        if all(v <= 0 for v in values):
            return Text(f"  No activity in the {self._span_label}.", style="white")

        try:
            width = self.content_size.width
        except Exception:
            width = 0
        # An unmounted / not-yet-laid-out widget reports width 0; fall back to
        # a sensible default instead of collapsing everything to one column.
        available = max(1, width - 2) if width > 0 else 80

        points = self._fit_points(self._data, available)
        if not points:
            return Text(f"  No activity in the {self._span_label}.", style="white")

        n = len(points)
        preferred_cell_w = 2 if n >= 20 else 4 if n >= 10 else 6
        cell_w = max(1, min(preferred_cell_w, available // max(n, 1)))
        strip_width = n * cell_w

        blocks = ["·", "▁", "▂", "▃", "▄", "▅", "▆", "█"]
        colors = [
            "grey35",
            "dim yellow",
            "yellow",
            "yellow",
            "bright_yellow",
            "bright_yellow",
            "bold bright_yellow",
            "bold bright_yellow",
        ]
        max_v = max(cost for _label, cost in points) or 1.0

        strip = Text("  ")
        for _label, cost in points:
            if cost <= 0:
                level = 0
            else:
                level = max(1, int(round((cost / max_v) * (len(blocks) - 1))))
            level = min(level, len(blocks) - 1)
            strip.append(blocks[level] * cell_w, style=colors[level])

        axis = Text("  ")
        axis.append(self._axis(strip_width, [label for label, _cost in points]), style="white")

        summary = self._summary(available)

        return Group(strip, axis, summary)

    def _summary(self, available: int) -> Text:
        """Build the one-line summary, dropping segments that don't fit.

        The chart panel is a fixed three rows (strip / axis / summary), so a
        wrapped summary would overflow it. Lower-priority segments (total,
        then peak) drop off first on narrow terminals; "latest" is always
        shown.
        """
        peak_label, peak_cost = max(self._data, key=lambda item: item[1])
        latest_label, latest_cost = self._data[-1]
        total_cost = sum(cost for _label, cost in self._data)

        segments: list[list[tuple[str, str]]] = [
            [("latest ", "white"), (latest_label, "bold"),
             (f" {fmt_cost(latest_cost)}", "bright_yellow")],
            [("peak ", "white"), (peak_label, "bold"),
             (f" {fmt_cost(peak_cost)}", "bright_yellow")],
            [("total ", "white"), (fmt_cost(total_cost), "bright_yellow")],
        ]
        sep = "  ·  "
        summary = Text("  ")
        used = 0
        for seg in segments:
            seg_len = sum(len(text) for text, _style in seg)
            if used and used + len(sep) + seg_len > available:
                break
            if used:
                summary.append(sep, style="white")
                used += len(sep)
            for text, style in seg:
                summary.append(text, style=style)
            used += seg_len
        return summary


def _token_summary_block(totals: dict) -> Group | None:
    """Two-line token block used at the top of the heatmap panel.

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

    line_total = Text("  ")
    line_total.append("Token total ", style="white")
    line_total.append(fmt_tokens(total_t), style="bright_cyan")
    if cache_pct >= 0:
        line_total.append("  ·  Cache % ", style="white")
        line_total.append(f"{cache_pct:.0f}%", style="bright_green")

    line_split = Text("  ")
    line_split.append("Token ", style="white")
    line_split.append("input ", style="white")
    line_split.append(fmt_tokens(input_t), style="bright_cyan")
    line_split.append("  ·  output ", style="white")
    line_split.append(fmt_tokens(output_t), style="bright_cyan")
    line_split.append("  ·  cache read ", style="white")
    line_split.append(fmt_tokens(cache_r), style="bright_green")
    if cache_w:
        line_split.append("  ·  cache write ", style="white")
        line_split.append(fmt_tokens(cache_w), style="bright_green")

    return Group(line_total, line_split)


def _top_projects_block(
    projects: list[dict],
    total_cost: float,
    *,
    total_unknown: bool = False,
    limit: int = 3,
) -> list[Text] | None:
    """Up to ``limit`` project rows. First row is labeled "Top projects"."""
    if not projects:
        return None
    entries = [
        p for p in projects
        if (p.get("estimated_cost_usd") or 0) > 0 or _has_unknown_cost(p)
    ][:limit]
    if not entries:
        return None

    any_unknown = total_unknown or any(_has_unknown_cost(p) for p in entries)
    label_text = "Top projects"

    lines: list[Text] = []
    for i, p in enumerate(entries):
        unknown = _has_unknown_cost(p)
        share = None
        if total_cost and not any_unknown:
            share = (p.get("estimated_cost_usd") or 0) / total_cost * 100

        line = Text("  ")
        if i == 0:
            line.append(label_text, style="white")
        else:
            line.append(" " * len(label_text), style="default")
        line.append("  ")
        line.append(
            _source_prefixed_path(
                p["project_path"] or "",
                p.get("data_root") or "",
                max_len=44,
            ),
            style="bright_blue",
        )
        line.append(
            f" · {fmt_cost(p.get('estimated_cost_usd'), unknown=unknown)}",
            style="bold bright_yellow" if i == 0 else "bright_yellow",
        )
        if share is not None:
            line.append(f" ({share:.1f}%)", style="white")
        lines.append(line)
    return lines




class Breakdown(Static):
    """Host → agent → provider → model nested breakdown.

    A single host is folded away (its agents render at the top level) so
    local-only setups stay compact. Data shape::

        [
            {
                "host": "local",
                "cost": 1234.56,
                "agents": [
                    {
                        "agent": "claude_code",
                        "cost": 1234.56,
                        "providers": [
                            {
                                "provider": "anthropic",
                                "data_root": "~/.claude-alt",
                                "models": [
                                    {"model": "claude-opus-4-7", "cost": 800.00},
                                    ...
                                ],
                            },
                        ],
                    },
                ],
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

    # Fixed-width value columns to the right of every tree row:
    #   cost · share% · in · out · cached · cache%
    # Fixed widths keep them aligned and stop the line from wrapping; the
    # column titles are shown once on a header row at the top of the panel.
    _W_COST = 10
    _W_SHARE = 6
    _W_IN = 7
    _W_OUT = 7
    _W_CACHED = 8
    _W_PCT = 6
    _VALUE_W = 1 + _W_COST + 1 + _W_SHARE + 1 + _W_IN + 1 + _W_OUT + 1 + _W_CACHED + 1 + _W_PCT

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        if width <= 0:
            return ""
        if len(text) <= width:
            return text
        if width <= 1:
            return text[:width]
        return text[: width - 1] + "…"

    def _append_label(self, t: Text, parts: list[tuple[str, str]], width: int) -> None:
        used = sum(len(text) for text, _style in parts)
        for text, style in parts:
            t.append(text, style=style)
        gap = width - used
        if gap <= 0:
            t.append(" ", style="white")
            return
        # Fill the space between the label and the right-aligned value block
        # with a faint dot leader so wide panels stay justified instead of
        # leaving a big blank gap.
        if gap >= 4:
            t.append(" ", style="white")
            t.append("·" * (gap - 2), style="grey35")
            t.append(" ", style="white")
        else:
            t.append(" " * gap, style="white")

    def _append_header(self, t: Text, label_width: int) -> None:
        """Column-title row so IN / OUT / CACHED stay labeled."""
        t.append(" " * label_width, style="white")
        for title, width in (
            ("Cost", self._W_COST),
            ("%", self._W_SHARE),
            ("In", self._W_IN),
            ("Out", self._W_OUT),
            ("Cached", self._W_CACHED),
            ("Cache%", self._W_PCT),
        ):
            t.append(f" {title:>{width}}", style="bold white")
        t.append("\n")

    def _append_value_columns(
        self,
        t: Text,
        row: dict,
        *,
        cost: float | None,
        cost_unknown: bool,
        share: str = "",
        cost_style: str = "bright_yellow",
    ) -> None:
        cache_pct = _cache_hit_pct(row)
        pct_str = f"{cache_pct}%" if cache_pct is not None else ""
        t.append(f" {fmt_cost(cost, unknown=cost_unknown):>{self._W_COST}}", style=cost_style)
        t.append(f" {share:>{self._W_SHARE}}", style="white")
        t.append(f" {fmt_tokens(row.get('input') or 0):>{self._W_IN}}", style="bright_cyan")
        t.append(f" {fmt_tokens(row.get('output') or 0):>{self._W_OUT}}", style="bright_cyan")
        t.append(f" {fmt_tokens(row.get('cache') or 0):>{self._W_CACHED}}", style="bright_green")
        t.append(f" {pct_str:>{self._W_PCT}}", style="bright_green")
        t.append("\n")

    @staticmethod
    def _share(cost: float, unknown: bool, total: float, total_unknown: bool) -> str:
        if unknown or total_unknown:
            return "—"
        return f"{(cost / total) * 100:.1f}%"

    def _emit_agent(
        self,
        t: Text,
        agent: dict,
        *,
        label_width: int,
        total: float,
        total_unknown: bool,
        base_bar: str,
        agent_last: bool,
        top_level: bool,
    ) -> None:
        """Render one agent subtree (agent → provider → model)."""
        if top_level:
            # No host above it: agent sits at column 0, like a plain list.
            agent_parts = [(self._truncate(agent["agent"], label_width), "bold bright_magenta")]
            provider_base = "  "
        else:
            connector = "└ " if agent_last else "├ "
            agent_prefix = base_bar + connector
            agent_parts = [
                (agent_prefix, "white"),
                (self._truncate(agent["agent"], label_width - len(agent_prefix)), "bold bright_magenta"),
            ]
            provider_base = base_bar + ("  " if agent_last else "│ ")

        agent_cost = agent.get("cost") or 0.0
        agent_unknown = _has_unknown_cost(agent)
        self._append_label(t, agent_parts, label_width)
        self._append_value_columns(
            t,
            agent,
            cost=agent_cost,
            cost_unknown=agent_unknown,
            share=self._share(agent_cost, agent_unknown, total, total_unknown),
            cost_style="bold bright_yellow",
        )

        providers = agent.get("providers") or [agent]
        for p_index, provider_group in enumerate(providers):
            provider_last = p_index == len(providers) - 1
            provider_connector = "└ " if provider_last else "├ "
            provider = provider_group.get("provider") or "—"
            provider_prefix = provider_base + provider_connector
            provider_width = 9
            path_width = max(6, label_width - len(provider_prefix) - provider_width - 1)
            data_root = _root_label(provider_group.get("data_root") or "", max_len=path_width)
            provider_cost = provider_group.get("cost") or 0.0
            provider_unknown = _has_unknown_cost(provider_group)

            self._append_label(
                t,
                [
                    (provider_prefix, "white"),
                    (self._truncate(provider, provider_width).ljust(provider_width), "bright_cyan"),
                    (" ", "white"),
                    (data_root, "white"),
                ],
                label_width,
            )
            self._append_value_columns(
                t,
                provider_group,
                cost=provider_cost,
                cost_unknown=provider_unknown,
                share=self._share(provider_cost, provider_unknown, total, total_unknown),
                cost_style="bold bright_yellow",
            )

            # The enclosing panel scrolls, so model rows can stay complete.
            model_base = provider_base + ("  " if provider_last else "│ ")
            raw_models = provider_group.get("models", []) or []
            visible = sorted(
                [m for m in raw_models if (m.get("cost") or 0) > 0 or _has_unknown_cost(m)],
                key=lambda m: -(m.get("cost") or 0),
            )
            for j, m in enumerate(visible):
                last = j == len(visible) - 1
                model_prefix = model_base + ("└ " if last else "├ ")
                model_name = m.get("model") or "(unknown)"
                if model_name == "Unknown" and m.get("raw_model"):
                    model_name = f"Unknown: {m['raw_model']}"
                self._append_label(
                    t,
                    [
                        (model_prefix, "white"),
                        (self._truncate(model_name, label_width - len(model_prefix)), "bright_cyan"),
                    ],
                    label_width,
                )
                self._append_value_columns(
                    t,
                    m,
                    cost=m.get("cost"),
                    cost_unknown=_has_unknown_cost(m),
                    share="",
                    cost_style="bright_yellow",
                )

    def render(self) -> Text:
        if not self._groups:
            return Text("  No activity in the selected range.", style="white")

        total = max(self._total_cost, 1e-9)
        total_unknown = any(_has_unknown_cost(h) for h in self._groups)
        try:
            width = self.content_size.width
        except Exception:
            width = 120
        # Right-align the value block to the panel edge (labels left, values
        # right — justified across the full width) and truncate labels so the
        # value columns never wrap onto a second line.
        label_width = max(24, width - self._VALUE_W)
        t = Text()
        self._append_header(t, label_width)

        # Fold the host level away when there is only one machine, so the
        # common local-only case stays a flat agent → provider → model tree.
        single_host = len(self._groups) == 1

        for host in self._groups:
            agents = host.get("agents") or []
            if single_host:
                for a_index, agent in enumerate(agents):
                    self._emit_agent(
                        t, agent,
                        label_width=label_width, total=total, total_unknown=total_unknown,
                        base_bar="", agent_last=(a_index == len(agents) - 1), top_level=True,
                    )
                continue

            host_cost = host.get("cost") or 0.0
            host_unknown = _has_unknown_cost(host)
            self._append_label(
                t,
                [(self._truncate(host.get("host") or "—", label_width), "bold white")],
                label_width,
            )
            self._append_value_columns(
                t,
                host,
                cost=host_cost,
                cost_unknown=host_unknown,
                share=self._share(host_cost, host_unknown, total, total_unknown),
                cost_style="bold bright_yellow",
            )
            for a_index, agent in enumerate(agents):
                self._emit_agent(
                    t, agent,
                    label_width=label_width, total=total, total_unknown=total_unknown,
                    base_bar="", agent_last=(a_index == len(agents) - 1), top_level=False,
                )

        return t


def _has_unknown_cost(row: dict | None) -> bool:
    return bool(row and (row.get("unknown_cost_count") or 0) > 0)
