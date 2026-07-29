"""Read-only pricing view shown inside the TUI (press `p`)."""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..pricing import (
    _BUILTIN_PRICING,
    _load_user_pricing,
    get_all_pricing,
    get_long_context_rules,
    get_user_cache_pricing,
)


class PricingScreen(ModalScreen):
    """Scrollable, read-only snapshot of effective pricing."""

    BINDINGS = [
        Binding("escape,q,p", "dismiss", "Close", key_display="Esc"),
    ]

    def __init__(self, unknown_models: list[str] | None = None) -> None:
        super().__init__()
        self._unknown = unknown_models or []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="pricing-box"):
            yield Static(self._build_content(), id="pricing-content")

    # ── Rendering ──────────────────────────────────────────────────

    def _build_content(self) -> Group:
        blocks: list[object] = [
            Text.from_markup("[bold]Pricing[/]  [white]USD per 1M tokens · press Esc to close[/]"),
        ]

        if self._unknown:
            configurable = [
                name for name in self._unknown if name != "(unknown model)"
            ]
            missing = Text("  Pricing missing: ", style="bright_yellow")
            missing.append(", ".join(self._unknown), style="bold bright_magenta")
            blocks.append(missing)
            blocks.append(Text(
                "  Cost totals exclude usage without applicable pricing.",
                style="white",
            ))
            if "(unknown model)" in self._unknown:
                blocks.append(Text(
                    "  Records labeled (unknown model) have no model id; pricing cannot be configured.",
                    style="white",
                ))
            if configurable:
                blocks.append(Text(
                    "  set with: agentic-metric pricing set <model> -i <input> -o <output>",
                    style="white",
                ))

        blocks.append(self._models_table())
        lc = self._long_context_table()
        if lc is not None:
            blocks.append(lc)
        cache = self._cache_table()
        if cache is not None:
            blocks.append(cache)
        return Group(*blocks)

    def _models_table(self) -> Table:
        user = _load_user_pricing()
        merged = get_all_pricing()
        tbl = Table(
            title="Models",
            title_style="bold bright_white",
            title_justify="left",
            box=box.SIMPLE_HEAVY,
            header_style="bold bright_white",
            pad_edge=False,
        )
        tbl.add_column("Model", style="bright_magenta", no_wrap=True)
        tbl.add_column("Input", justify="right", style="bright_cyan")
        tbl.add_column("Output", justify="right", style="bright_cyan")
        tbl.add_column("Cache read", justify="right", style="bright_blue")
        tbl.add_column("Cache write", justify="right", style="bright_blue")
        tbl.add_column("Source", style="white")
        for model in sorted(merged):
            p = merged[model]
            if model in user and model in _BUILTIN_PRICING:
                source = (
                    Text("builtin", style="white")
                    if tuple(p) == tuple(_BUILTIN_PRICING[model])
                    else Text("override", style="bright_yellow")
                )
            elif model in user:
                source = Text("custom", style="bright_green")
            else:
                source = Text("builtin", style="white")
            tbl.add_row(
                model,
                f"${p[0]:.3f}", f"${p[1]:.3f}", f"${p[2]:.3f}", f"${p[3]:.3f}",
                source,
            )
        return tbl

    def _long_context_table(self) -> Table | None:
        rows = get_long_context_rules(include_disabled=True)
        if not rows:
            return None
        tbl = Table(
            title="Long context (per request over threshold)",
            title_style="bold bright_white",
            title_justify="left",
            box=box.SIMPLE_HEAVY,
            header_style="bold bright_white",
            pad_edge=False,
        )
        tbl.add_column("Model prefix", style="bright_magenta", no_wrap=True)
        tbl.add_column("Threshold", justify="right", style="bright_cyan")
        tbl.add_column("Input", justify="right", style="bright_cyan")
        tbl.add_column("Output", justify="right", style="bright_cyan")
        tbl.add_column("Cache read", justify="right", style="bright_blue")
        tbl.add_column("Source", style="white")
        for rule in rows:
            prefixes = ", ".join(str(p) for p in rule["prefixes"])
            prices = tuple(float(v) for v in rule["prices"])
            source = str(rule.get("source") or "builtin")
            style = (
                "bright_yellow" if source == "user"
                else "bright_red" if source == "disabled"
                else "white"
            )
            tbl.add_row(
                prefixes,
                f"{int(rule['threshold']):,}",
                f"${prices[0]:.3f}", f"${prices[1]:.3f}", f"${prices[2]:.3f}",
                Text(source, style=style),
            )
        return tbl

    def _cache_table(self) -> Table | None:
        rows = get_user_cache_pricing()
        if not rows:
            return None
        tbl = Table(
            title="Cache-duration overrides",
            title_style="bold bright_white",
            title_justify="left",
            box=box.SIMPLE_HEAVY,
            header_style="bold bright_white",
            pad_edge=False,
        )
        tbl.add_column("Model prefix", style="bright_magenta", no_wrap=True)
        tbl.add_column("1h write", justify="right", style="bright_blue")
        for model, rule in sorted(rows.items()):
            tbl.add_row(
                model,
                f"${float(rule['write_1h']):.3f}" if "write_1h" in rule else "",
            )
        return tbl
