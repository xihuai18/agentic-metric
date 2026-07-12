"""Custom widgets for the Agentic Metric TUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.console import Group
from rich.text import Text
from textual.geometry import Size
from textual.widgets import Static

from ..formatting import cache_hit_rate as _cache_hit_rate
from ..formatting import cache_hit_rate_band as _cache_hit_rate_band
from ..formatting import clip as _clip
from ..formatting import root_label as _root_label
from ..formatting import source_prefixed_path as _source_prefixed_path
from ..formatting import token_summary as _token_summary


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


_CACHE_PCT_STYLES = {
    "excellent": "bold bright_green",
    "good": "bright_green",
    "warn": "bright_yellow",
    "low": "yellow",
    "none": "white",
}


def _cache_pct_style(cache_pct: float | int | None) -> str:
    return _CACHE_PCT_STYLES[_cache_hit_rate_band(cache_pct)]


@dataclass(frozen=True)
class _HistogramRender:
    bars: Group
    axis: Text
    width: int


def _histogram_layout(point_count: int, available: int) -> tuple[int, int, int]:
    """Return ``(bar_width, gap_width, total_width)`` for one histogram."""
    if point_count <= 0:
        return 0, 0, 0

    if point_count <= 8:
        preferred_bar_w, preferred_gap_w = 3, 2
    elif point_count <= 14:
        preferred_bar_w, preferred_gap_w = 3, 1
    else:
        preferred_bar_w, preferred_gap_w = 2, 1

    for gap_w in (preferred_gap_w, 1, 0):
        usable = available - gap_w * (point_count - 1)
        if usable < point_count:
            continue
        bar_w = max(1, min(preferred_bar_w, usable // point_count))
        return bar_w, gap_w, point_count * bar_w + (point_count - 1) * gap_w

    return 1, 0, point_count


def _axis_from_layout(
    labels: list[str],
    *,
    bar_w: int,
    gap_w: int,
    width: int,
    highlight_index: int | None = None,
) -> Text:
    """Build the axis label row, evenly spaced and as dense as fits.

    The current bucket's label (``highlight_index``) is emphasized instead of
    inverting its bar, so a full-height bar never has to be drawn in reverse
    (which makes it vanish into the background).
    """
    if width <= 0 or not labels:
        return Text("  ")

    pitch = bar_w + gap_w
    n = len(labels)
    if n <= 8:
        indexes = list(range(n))
    else:
        # As many evenly spaced labels as fit without colliding: pick a step so
        # consecutive labels are at least one space apart (e.g. a ~30-day month
        # lands a label roughly every 4-10 days instead of only 3 total).
        max_len = max((len(str(label or "")) for label in labels), default=1)
        step = max(1, -(-(max_len + 2) // pitch))  # ceil division
        indexes = list(range(0, n, step))
        if n - 1 not in indexes:
            indexes.append(n - 1)

    # Place the highlighted label first so it always survives collision pruning.
    order = list(indexes)
    if highlight_index is not None and 0 <= highlight_index < n and highlight_index not in order:
        order.insert(0, highlight_index)

    chars = [" "] * width
    styles: list[str] = ["white"] * width
    occupied = [False] * width
    for idx in order:
        label = str(labels[idx] or "")
        if not label:
            continue
        label = label[:width]
        center = idx * pitch + (bar_w // 2)
        start = max(0, min(width - len(label), center - len(label) // 2))
        end = start + len(label)
        if any(occupied[max(0, start - 1):min(width, end + 1)]):
            continue
        style = "bold reverse" if idx == highlight_index else "white"
        for pos, ch in enumerate(label, start):
            chars[pos] = ch
            occupied[pos] = True
            styles[pos] = style

    out = Text("  ")
    i = 0
    while i < width:
        j = i
        while j < width and styles[j] == styles[i]:
            j += 1
        out.append("".join(chars[i:j]), style=styles[i])
        i = j
    return out


def _render_histogram(
    values: list[float],
    *,
    labels: list[str],
    available: int,
    colors: list[str],
    highlighted: list[bool] | None = None,
    rows: int = 4,
) -> _HistogramRender:
    """Render a multi-row vertical bar chart.

    Bars are stacked solid ``█`` blocks: every cell is a *full* block (which
    renders flush in every terminal font), so bar height is encoded by how many
    rows are filled from the bottom — never by partial-height glyphs (``▁▂▄``),
    which some fonts center vertically and make look like they float.

    Both bar height (filled rows) and color intensity are proportional to the
    peak (``value / max``), so a bar honestly represents its value relative to
    the busiest bucket: taller is always brighter, never the reverse. Color has
    finer granularity than the row count, so it still separates bars that round
    to the same height. Absolute totals live in the peak/total captions.
    """
    bar_w, gap_w, strip_width = _histogram_layout(len(values), available)
    v_max = max(values) if values else 0
    highlight_flags = highlighted or [False] * len(values)
    highlight_index = next((i for i, flag in enumerate(highlight_flags) if flag), None)

    lines = [Text("  ") for _ in range(rows)]
    for idx, value in enumerate(values):
        if value <= 0 or v_max <= 0:
            filled = 0
            style = colors[0]
        else:
            ratio = value / v_max
            filled = max(1, min(rows, int(round(ratio * rows))))
            color_idx = max(1, min(len(colors) - 1, int(round(ratio * (len(colors) - 1)))))
            style = colors[color_idx]
        for row, line in enumerate(lines):  # row 0 = top
            if idx:
                line.append(" " * gap_w, style="default")
            lit = (rows - row) <= filled
            line.append("█" * bar_w if lit else " " * bar_w, style=style if lit else "default")

    bars = Group(*lines)
    axis = _axis_from_layout(
        labels, bar_w=bar_w, gap_w=gap_w, width=strip_width, highlight_index=highlight_index
    )
    return _HistogramRender(bars=bars, axis=axis, width=strip_width)


# ── Widgets ───────────────────────────────────────────────────────────


_HEATMAP_HISTOGRAM_ROWS = 8


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
        self.prev_cost: float | None = None
        self.prev_cost_unknown = False
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
        prev_cost: float | None = None,
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
        self.prev_cost = prev_cost
        self.prev_cost_unknown = prev_cost_unknown
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

    @staticmethod
    def _count_label(value: int, label: str, *, compact: bool) -> str:
        number = fmt_tokens(value) if compact else f"{value:,}"
        return f"{number} {label}"

    def _stats_lines(self, available: int) -> list[tuple[tuple[str, str], ...]]:
        """Return pre-wrapped stats rows that fit within the summary cell."""
        width = available if available > 0 else 36

        def parts(*, compact: bool) -> list[tuple[str, str]]:
            return [
                (self._count_label(self.sessions, "sess", compact=compact), "white"),
                (self._count_label(self.requests, "req", compact=compact), "white"),
                (self._count_label(self.turns, "turns", compact=compact), "white"),
            ]

        def line_len(line: list[tuple[str, str]]) -> int:
            return sum(len(text) for text, _style in line)

        def joined_line(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
            line: list[tuple[str, str]] = []
            for idx, item in enumerate(items):
                if idx:
                    line.append((" · ", "white"))
                line.append(item)
            return line

        for compact in (False, True):
            inline = joined_line(parts(compact=compact))
            if line_len(inline) <= width:
                return [tuple(inline)]

        compact_parts = parts(compact=True)
        first = joined_line(compact_parts[:2])
        second = [compact_parts[2]]
        return [tuple(first), tuple(second)]

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
        t.append("Token ", style="white")
        t.append(fmt_tokens(self.tokens), style="bold bright_cyan")
        if self.cache_pct is not None:
            t.append("  ·  Cache % ", style="white")
            t.append(f"{self.cache_pct}%", style=_cache_pct_style(self.cache_pct))
        t.append("\n")
        # Sessions / requests / turns — fit deliberately so labels never wrap.
        try:
            avail = self.content_size.width
        except Exception:
            avail = 36
        for row_idx, row in enumerate(self._stats_lines(avail)):
            if row_idx:
                t.append("\n")
            for text, style in row:
                t.append(text, style=style)
        return t


class PeriodicHeatmap(Static):
    """Focused-period histogram panel body.

    Renders (top to bottom):
        - token split line (input · output · cache read · cache write)
        - histogram strip + axis labels
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
        self._providers: list[dict] = []
        self._total_cost: float = 0.0

    def update_data(
        self,
        buckets: list[dict],
        highlight_index: int | None = None,
        totals: dict | None = None,
        projects: list[dict] | None = None,
        providers: list[dict] | None = None,
        total_cost: float = 0.0,
    ) -> None:
        self._buckets = buckets
        self._highlight = highlight_index
        self._totals = totals or {}
        self._projects = projects or []
        self._providers = providers or []
        self._total_cost = total_cost
        self.refresh()

    @staticmethod
    def _fit_buckets(
        buckets: list[dict],
        max_points: int,
        highlight_index: int | None,
    ) -> list[dict]:
        """Collapse older buckets only when the terminal is too narrow."""
        if max_points <= 0:
            return []
        if len(buckets) <= max_points:
            return [
                {**bucket, "_highlighted": i == highlight_index}
                for i, bucket in enumerate(buckets)
            ]

        bucket_w = len(buckets) / max_points
        fitted: list[dict] = []
        for i in range(max_points):
            start = int(i * bucket_w)
            end = int((i + 1) * bucket_w)
            if i == max_points - 1:
                end = len(buckets)
            chunk = buckets[start:max(end, start + 1)]
            fitted.append({
                "label": chunk[-1].get("label", ""),
                "cost": sum(b.get("cost") or 0 for b in chunk),
                "tokens": sum(b.get("tokens") or 0 for b in chunk),
                "unknown_cost_count": sum(b.get("unknown_cost_count") or 0 for b in chunk),
                "_highlighted": (
                    highlight_index is not None
                    and any(start <= highlight_index < end for _b in chunk)
                ),
            })
        return fitted

    def render(self) -> Group | Text:
        if not self._buckets:
            return Text("  (no data)", style="white")

        try:
            width = self.content_size.width
        except Exception:
            width = 0
        available = max(1, width - 2) if width > 0 else 80

        max_points = max(1, available)
        points = self._fit_buckets(self._buckets, max_points, self._highlight)
        if not points:
            return Text("  (no data)", style="white")

        colors = [
            "grey35",
            "dim green",
            "green",
            "green",
            "bright_green",
            "bright_green",
            "bold bright_green",
            "bold bright_green",
        ]
        histogram = _render_histogram(
            [b.get("cost") or 0 for b in points],
            labels=[str(b.get("label") or "") for b in points],
            available=available,
            colors=colors,
            highlighted=[bool(b.get("_highlighted")) for b in points],
            rows=_HEATMAP_HISTOGRAM_ROWS,
        )

        buckets = self._buckets
        n = len(buckets)
        known_peak_idx = max(range(n), key=lambda i: buckets[i].get("cost") or 0)
        unknown_peak_idx = next(
            (i for i, b in enumerate(buckets) if _has_unknown_cost(b)),
            None,
        )
        peak_idx = known_peak_idx if (buckets[known_peak_idx].get("cost") or 0) > 0 else (
            unknown_peak_idx if unknown_peak_idx is not None else known_peak_idx
        )

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

        body: list[Text | Group] = []

        tsummary = _token_summary_block(self._totals)
        if tsummary is not None:
            body.append(tsummary)

        body.append(histogram.bars)
        body.append(histogram.axis)
        body.append(peak_line)

        # Providers + Top projects share a single separator from the chart and
        # sit back-to-back (each carries its own row label) to stay compact.
        lists: list[Text] = []
        providers_block = _top_providers_block(
            self._providers,
            self._total_cost,
            total_unknown=_has_unknown_cost(self._totals),
        )
        if providers_block is not None:
            lists.extend(providers_block)

        projects_block = _top_projects_block(
            self._projects,
            self._total_cost,
            total_unknown=_has_unknown_cost(self._totals),
        )
        if projects_block is not None:
            lists.extend(projects_block)

        if lists:
            body.append(Text(""))
            body.extend(lists)

        return Group(*body)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        """Reserve every rendered row so Textual does not clip the lower details."""
        if not self._buckets:
            return 1

        height = _HEATMAP_HISTOGRAM_ROWS + 2  # histogram axis + peak row
        if _token_summary_block(self._totals) is not None:
            height += 2

        detail_rows = 0
        providers = _top_providers_block(
            self._providers,
            self._total_cost,
            total_unknown=_has_unknown_cost(self._totals),
        )
        projects = _top_projects_block(
            self._projects,
            self._total_cost,
            total_unknown=_has_unknown_cost(self._totals),
        )
        detail_rows += len(providers or ())
        detail_rows += len(projects or ())
        if detail_rows:
            height += 1 + detail_rows
        return height


class TrendBlocks(Static):
    """Compact cost trend rendered as colored blocks."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._data: list[tuple[str, float]] = []
        self._span_label = ""
        self._provider_totals: list[dict] = []

    def update_data(
        self,
        data: list[tuple[str, float]],
        span_label: str,
        provider_totals: list[dict] | None = None,
    ) -> None:
        self._data = data
        self._span_label = span_label
        self._provider_totals = provider_totals or []
        self.refresh()

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
        histogram = _render_histogram(
            [cost for _label, cost in points],
            labels=[label for label, _cost in points],
            available=available,
            colors=colors,
            rows=3,
        )

        summary = self._summary(available)

        rows = [histogram.bars, histogram.axis, summary]
        provider_line = self._provider_line(available)
        if provider_line is not None:
            rows.append(provider_line)
        return Group(*rows)

    def _provider_line(self, available: int) -> Text | None:
        """One-line per-provider cost total over the trend window.

        Renders ``<name> $X · <name> $Y``. Lower-priority providers drop off
        the right on narrow terminals so the line never wraps past the fixed
        panel height.
        """
        entries = [
            p for p in self._provider_totals
            if (p.get("estimated_cost_usd") or 0) > 0 or _has_unknown_cost(p)
        ]
        if not entries:
            return None
        line = Text("  ")
        used = 2
        for i, p in enumerate(entries):
            name = p.get("provider") or "—"
            cost = fmt_cost(p.get("estimated_cost_usd"), unknown=_has_unknown_cost(p))
            prefix = "" if i == 0 else " · "
            seg = f"{prefix}{name} {cost}"
            if used + len(seg) > available:
                break
            if prefix:
                line.append(prefix, style="white")
            line.append(name, style="bright_blue")
            line.append(f" {cost}", style="bright_yellow")
            used += len(seg)
        return line

    def _summary(self, available: int) -> Text:
        """Build the one-line summary, dropping segments that don't fit.

        The chart panel has fixed rows (histogram / axis / summary), so a
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
    summary = _token_summary(totals)
    input_t = summary["input_tokens"]
    output_t = summary["output_tokens"]
    cache_r = summary["cache_read_tokens"]
    cache_w = summary["cache_creation_tokens"]
    total_t = summary["total_tokens"]
    if total_t == 0:
        return None

    cache_pct = summary["cache_pct"]

    line_total = Text("  ")
    line_total.append("Token total ", style="white")
    line_total.append(fmt_tokens(total_t), style="bright_cyan")
    if cache_pct >= 0:
        line_total.append("  ·  Cache % ", style="white")
        line_total.append(f"{cache_pct:.0f}%", style=_cache_pct_style(cache_pct))

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


def _top_providers_block(
    providers: list[dict],
    total_cost: float,
    *,
    total_unknown: bool = False,
    limit: int = 3,
) -> list[Text] | None:
    """Up to ``limit`` provider cost rows for the focused TUI range."""
    entries = [
        p for p in providers
        if (p.get("estimated_cost_usd") or 0) > 0 or _has_unknown_cost(p)
    ][:limit]
    if not entries:
        return None

    all_entries = [
        p for p in providers
        if (p.get("estimated_cost_usd") or 0) > 0 or _has_unknown_cost(p)
    ]
    any_unknown = total_unknown or any(_has_unknown_cost(p) for p in all_entries)
    label_text = "Providers"

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
        line.append(_clip(str(p.get("provider") or "—"), 16), style="bright_blue")
        line.append(
            f" · {fmt_cost(p.get('estimated_cost_usd'), unknown=unknown)}",
            style="bold bright_yellow" if i == 0 else "bright_yellow",
        )
        if share is not None:
            line.append(f" ({share:.1f}%)", style="white")
        lines.append(line)

    hidden = len(all_entries) - len(entries)
    if hidden > 0:
        line = Text("  ")
        line.append(" " * len(label_text), style="default")
        line.append(f"  +{hidden} more", style="white")
        lines.append(line)
    return lines


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
        self.refresh(layout=True)

    # Fixed-width value columns to the right of every tree row:
    #   cost · share% · in · out · cache read · cache%
    # Fixed widths keep them aligned and stop the line from wrapping; the
    # column titles are shown once on a header row at the top of the panel.
    _W_COST = 10
    _W_SHARE = 6
    _W_IN = 7
    _W_OUT = 7
    _W_CACHED = 10
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
        """Column-title row so input/output/cache-read values stay labeled."""
        t.append(" " * label_width, style="white")
        for title, width in (
            ("Cost", self._W_COST),
            ("%", self._W_SHARE),
            ("In", self._W_IN),
            ("Out", self._W_OUT),
            ("Cache", self._W_CACHED),
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
        cache_pct = _cache_hit_rate(row)
        pct_str = f"{cache_pct:.0f}%" if cache_pct >= 0 else ""
        pct_style = _cache_pct_style(cache_pct)
        t.append(f" {fmt_cost(cost, unknown=cost_unknown):>{self._W_COST}}", style=cost_style)
        t.append(f" {share:>{self._W_SHARE}}", style="white")
        t.append(f" {fmt_tokens(row.get('input') or 0):>{self._W_IN}}", style="bright_cyan")
        t.append(f" {fmt_tokens(row.get('output') or 0):>{self._W_OUT}}", style="bright_cyan")
        t.append(f" {fmt_tokens(row.get('cache') or 0):>{self._W_CACHED}}", style="bright_green")
        t.append(f" {pct_str:>{self._W_PCT}}", style=pct_style)
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

    def _render_for_width(self, width: int) -> Text:
        if not self._groups:
            return Text("  No activity in the selected range.", style="white")

        total = max(self._total_cost, 1e-9)
        total_unknown = any(_has_unknown_cost(h) for h in self._groups)
        width = width or 120
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

    def render(self) -> Text:
        try:
            width = self.content_size.width
        except Exception:
            width = 120
        return self._render_for_width(width)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self._render_for_width(width).plain.splitlines()) or 1

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return max(container.width, self._VALUE_W + 24)


def _has_unknown_cost(row: dict | None) -> bool:
    return bool(row and (row.get("unknown_cost_count") or 0) > 0)
