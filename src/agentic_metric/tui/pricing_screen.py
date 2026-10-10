"""Pricing view shown inside the TUI (press `p`)."""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..pricing import (
    get_long_context_rules,
    get_observed_model_pricing,
    get_user_cache_pricing,
    long_context_rule_applies,
)

_SOURCE_STYLES = {
    "builtin": "white",
    "override": "bright_yellow",
    "custom": "bright_green",
    "catalog": "bright_blue",
}


class PricingScreen(ModalScreen):
    """Scrollable snapshot of the pricing that applies to recorded usage."""

    BINDINGS = [
        Binding("escape,q,p", "dismiss", "Close", key_display="Esc"),
        Binding("u", "update_pricing", "Update prices"),
    ]

    def __init__(
        self,
        unknown_models: list[str] | None = None,
        used_models: list[str] | None = None,
        *,
        updating: bool = False,
    ) -> None:
        super().__init__()
        self._unknown = unknown_models or []
        self._used = used_models or []
        self._updating = updating

    def compose(self) -> ComposeResult:
        with Vertical(id="pricing-box"):
            with Horizontal(id="pricing-header"):
                yield Static(
                    Text.from_markup(
                        "[bold]Pricing[/]  [white]USD per 1M tokens · models with "
                        "recorded usage · Esc to close[/]"
                    ),
                    id="pricing-title",
                )
                yield Button(self._button_label(), id="pricing-update", disabled=self._updating)
            with VerticalScroll(id="pricing-scroll"):
                yield Static(self._build_content(), id="pricing-content")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pricing-update":
            self.action_update_pricing()

    def action_update_pricing(self) -> None:
        if self._updating:
            return
        request = getattr(self.app, "request_pricing_update", None)
        if request is not None:
            request()

    def update_view(
        self,
        unknown_models: list[str],
        used_models: list[str],
        *,
        updating: bool,
    ) -> None:
        """Re-render after a sync or pricing update without reopening the modal."""
        self._unknown = unknown_models
        self._used = used_models
        self._updating = updating
        button = self.query_one("#pricing-update", Button)
        button.label = self._button_label()
        button.disabled = updating
        self.query_one("#pricing-content", Static).update(self._build_content())

    def _button_label(self) -> str:
        return "Updating…" if self._updating else "Update prices (u)"

    # ── Rendering ──────────────────────────────────────────────────

    def _build_content(self) -> Group:
        blocks: list[object] = []

        if self._unknown:
            configurable = [
                name for name in self._unknown if name != "(unknown model)"
            ]
            missing = Text("Pricing missing: ", style="bright_yellow")
            missing.append(", ".join(self._unknown), style="bold bright_magenta")
            blocks.append(missing)
            blocks.append(Text(
                "Cost totals exclude usage without applicable pricing.",
                style="white",
            ))
            if "(unknown model)" in self._unknown:
                blocks.append(Text(
                    "Records labeled (unknown model) have no model id; pricing cannot be configured.",
                    style="white",
                ))
            if configurable:
                blocks.append(Text(
                    "set with: agentic-metric pricing set <model> -i <input> -o <output>",
                    style="white",
                ))
            blocks.append(Text(""))

        rows = get_observed_model_pricing(self._used)
        if rows:
            blocks.append(self._models_table(rows))
        else:
            blocks.append(Text("No priced usage recorded yet.", style="white"))
        bases = {str(row["base_model"]) for row in rows}
        lc = self._long_context_table(bases)
        if lc is not None:
            blocks.append(lc)
        cache = self._cache_table()
        if cache is not None:
            blocks.append(cache)
        return Group(*blocks)

    @staticmethod
    def _price_columns(tbl: Table, *names: tuple[str, str]) -> None:
        for name, style in names:
            tbl.add_column(name, justify="right", style=style, no_wrap=True)

    def _models_table(self, rows: list[dict[str, object]]) -> Table:
        tbl = Table(
            title="Models",
            title_style="bold bright_white",
            title_justify="left",
            box=box.SIMPLE_HEAVY,
            header_style="bold bright_white",
            pad_edge=False,
        )
        tbl.add_column("Model", style="bright_magenta", overflow="fold")
        self._price_columns(
            tbl,
            ("Input", "bright_cyan"),
            ("Output", "bright_cyan"),
            ("Cache read", "bright_blue"),
            ("Cache write", "bright_blue"),
        )
        tbl.add_column("Source", no_wrap=True)
        for row in rows:
            p = row["prices"]
            source = str(row["source"])
            tbl.add_row(
                str(row["model"]),
                f"${p[0]:.3f}", f"${p[1]:.3f}", f"${p[2]:.3f}", f"${p[3]:.3f}",
                Text(source, style=_SOURCE_STYLES.get(source, "white")),
            )
        return tbl

    def _long_context_table(self, models: set[str]) -> Table | None:
        rows = sorted(
            (
                rule for rule in get_long_context_rules(include_disabled=True)
                if long_context_rule_applies(rule, models)
            ),
            key=lambda rule: (
                ", ".join(str(p) for p in rule["prefixes"]),
                int(rule["threshold"]),
            ),
        )
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
        tbl.add_column("Model prefix", style="bright_magenta", overflow="fold")
        self._price_columns(
            tbl,
            ("Threshold", "bright_cyan"),
            ("Input", "bright_cyan"),
            ("Output", "bright_cyan"),
            ("Cache read", "bright_blue"),
        )
        tbl.add_column("Source", no_wrap=True)
        for rule in rows:
            prefixes = ", ".join(str(p) for p in rule["prefixes"])
            prices = tuple(float(v) for v in rule["prices"])
            source = str(rule.get("source") or "builtin")
            style = (
                "bright_yellow" if source == "user"
                else "bright_red" if source == "disabled"
                else _SOURCE_STYLES.get(source, "white")
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
        tbl.add_column("Model prefix", style="bright_magenta", overflow="fold")
        self._price_columns(tbl, ("1h write", "bright_blue"))
        for model, rule in sorted(rows.items()):
            tbl.add_row(
                model,
                f"${float(rule['write_1h']):.3f}" if "write_1h" in rule else "",
            )
        return tbl
