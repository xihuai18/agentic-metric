"""Custom widgets for the Agentic Metric TUI."""

from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.text import Text
from textual.widgets import Static

from ..formatting import short_path as _short_path


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


# Keep the old name as an alias for backwards compatibility
HourHeatmap = PeriodicHeatmap


def _token_summary_block(totals: dict) -> Group | None:
    """Two-line token block used at the top of the heatmap panel.

    Line 1: ``Token total N · cache hit P%``
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

    # cache hit rate = cache-reuse / (cache-reuse + fresh input)
    denom = input_t + cache_r + cache_w
    cache_pct = round(cache_r / denom * 100) if denom > 0 else None

    line_total = Text("  ")
    line_total.append("Token total ", style="white")
    line_total.append(fmt_tokens(total_t), style="bright_cyan")
    if cache_pct is not None:
        line_total.append("  ·  cache hit ", style="white")
        line_total.append(f"{cache_pct}%", style="bright_green")

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
        line.append(_short_path(p["project_path"] or "", max_len=44),
                    style="bright_blue")
        line.append(
            f" · {fmt_cost(p.get('estimated_cost_usd'), unknown=unknown)}",
            style="bold bright_yellow" if i == 0 else "bright_yellow",
        )
        if share is not None:
            line.append(f" ({share:.1f}%)", style="white")
        lines.append(line)
    return lines




class Breakdown(Static):
    """Agent → provider → model nested breakdown with cost bars.

    Data shape::

        [
            {
                "agent": "claude_code",
                "cost": 1234.56,
                "tokens": ...,
                "providers": [
                    {
                        "provider": "anthropic",
                        "data_root": "~/.claude-alt",
                        "models": [
                            {"model": "claude-opus-4-7", "cost": 800.00, "tokens": ...},
                            ...
                        ],
                    },
                ]
            },
            ...
        ]
    """

    _MIN_MODEL_LIMIT = 3

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
        bar.append("█" * filled, style="bold bright_cyan")
        bar.append("░" * (width - filled), style="white")
        return bar

    def _split(self, row: dict) -> str:
        return (
            f"in {fmt_tokens(row.get('input') or 0)}  "
            f"out {fmt_tokens(row.get('output') or 0)}  "
            f"cache {fmt_tokens(row.get('cache') or 0)}"
        )

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
        if used < width:
            t.append(" " * (width - used), style="white")
        else:
            t.append(" ", style="white")

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
        t.append(f" {fmt_cost(cost, unknown=cost_unknown):>10}", style=cost_style)
        t.append(f" {share:>7}", style="white")
        t.append("  ")
        t.append(self._split(row), style="white")
        t.append("\n")

    def render(self) -> Text:
        if not self._groups:
            return Text("  No activity in the selected range.", style="white")

        total = max(self._total_cost, 1e-9)
        total_unknown = any(_has_unknown_cost(g) for g in self._groups)
        # Each provider uses one line plus model rows; each agent uses one
        # summary line and a spacer. Compute a per-provider model budget from
        # the visible height so wide ranges stay readable.
        provider_count = sum(len(g.get("providers") or [g]) for g in self._groups)
        provider_count = max(provider_count, 1)
        # Compute how many model lines we can afford from the available height.
        avail = self.size.height
        overhead = len(self._groups) * 2 + provider_count
        model_budget = max(avail - overhead, provider_count * self._MIN_MODEL_LIMIT)
        model_limit = max(self._MIN_MODEL_LIMIT, model_budget // provider_count)
        try:
            width = self.content_size.width
        except Exception:
            width = 120
        label_width = max(38, min(72, width - 58))
        t = Text()
        for i, g in enumerate(self._groups):
            agent = g["agent"]
            cost = g["cost"]
            unknown = _has_unknown_cost(g)
            ratio = cost / total
            pct = ratio * 100

            # Agent summary line. Provider and model detail are nested below it.
            self._append_label(
                t,
                [("  ", "white"), (self._truncate(agent, label_width - 2), "bold bright_magenta")],
                label_width,
            )
            self._append_value_columns(
                t,
                g,
                cost=cost,
                cost_unknown=unknown,
                share="—" if unknown or total_unknown else f"{pct:.1f}%",
                cost_style="bold bright_yellow",
            )

            providers = g.get("providers") or [g]
            for p_index, provider_group in enumerate(providers):
                provider_last = p_index == len(providers) - 1
                provider_connector = "└─" if provider_last else "├─"
                provider = provider_group.get("provider") or "—"
                provider_prefix = f"    {provider_connector} "
                provider_width = 10
                path_width = max(8, label_width - len(provider_prefix) - provider_width - 1)
                data_root = _short_path(provider_group.get("data_root") or "—", max_len=path_width)
                provider_cost = provider_group.get("cost") or 0.0
                provider_unknown = _has_unknown_cost(provider_group)
                provider_ratio = provider_cost / total
                provider_pct = provider_ratio * 100

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
                    share="—" if provider_unknown or total_unknown else f"{provider_pct:.1f}%",
                    cost_style="bold bright_yellow",
                )

                # Model rows: keep the panel readable, then roll up the tail.
                # Unknown models are always visible, never hidden.
                raw_models = provider_group.get("models", []) or []
                nonzero = [
                    m for m in raw_models
                    if (m.get("cost") or 0) > 0 or _has_unknown_cost(m)
                ]
                known = sorted(
                    [m for m in nonzero if not _has_unknown_cost(m)],
                    key=lambda m: -(m.get("cost") or 0),
                )
                unknown_models = sorted(
                    [m for m in nonzero if _has_unknown_cost(m)],
                    key=lambda m: -(m.get("cost") or 0),
                )
                visible = known[: model_limit] + unknown_models
                hidden = known[model_limit :]
                child_prefix = "       " if provider_last else "    │  "

                for j, m in enumerate(visible):
                    last = (j == len(visible) - 1 and not hidden)
                    connector = "└─" if last else "├─"
                    model_prefix = f"{child_prefix}{connector} "
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
                if hidden:
                    hidden_cost = sum(m.get("cost") or 0 for m in hidden)
                    hidden_unknown = any(_has_unknown_cost(m) for m in hidden)
                    hidden_row = {
                        "input": sum(m.get("input") or 0 for m in hidden),
                        "output": sum(m.get("output") or 0 for m in hidden),
                        "cache": sum(m.get("cache") or 0 for m in hidden),
                    }
                    model_prefix = f"{child_prefix}└─ "
                    label = f"+{len(hidden)} more models"
                    self._append_label(
                        t,
                        [
                            (model_prefix, "white"),
                            (self._truncate(label, label_width - len(model_prefix)), "white"),
                        ],
                        label_width,
                    )
                    self._append_value_columns(
                        t,
                        hidden_row,
                        cost=hidden_cost,
                        cost_unknown=hidden_unknown,
                        share="",
                        cost_style="bright_yellow",
                    )
            if i != len(self._groups) - 1:
                t.append("\n")

        return t


def _has_unknown_cost(row: dict | None) -> bool:
    return bool(row and (row.get("unknown_cost_count") or 0) > 0)
