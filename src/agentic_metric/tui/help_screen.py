"""Keybinding cheatsheet shown inside the TUI (press `?`)."""

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

# (section, [(keys, description), ...]). Keep this to the primary keys shown
# to users; alternate bindings remain hidden so the cheatsheet stays simple.
_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Navigation", [
        ("← →", "Switch view (Today / Week / Month)"),
        ("PgUp / PgDn", "Move time range earlier / later"),
        (".", "Jump back to now"),
        ("↑ ↓", "Scroll the breakdown panel"),
    ]),
    ("Data", [
        ("r", "Toggle fast auto-refresh"),
        ("p", "Pricing (read-only; flags unknown models)"),
    ]),
    ("Other", [
        ("?", "This help"),
        ("q", "Quit"),
    ]),
]


class HelpScreen(ModalScreen):
    """Scrollable, read-only keybinding reference."""

    BINDINGS = [
        Binding("escape,q,question_mark,?", "dismiss", "Close", key_display="Esc"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-box"):
            yield Static(self._build_content(), id="help-content")

    def _build_content(self) -> Group:
        tbl = Table(
            box=box.SIMPLE_HEAVY,
            header_style="bold bright_white",
            pad_edge=False,
        )
        tbl.add_column("Key", style="bold bright_magenta", no_wrap=True)
        tbl.add_column("Action", style="white")
        for s_index, (section, keys) in enumerate(_SECTIONS):
            if s_index:
                tbl.add_section()
            tbl.add_row(Text(section, style="bold bright_cyan"), "")
            for key, desc in keys:
                tbl.add_row(key, desc)
        return Group(
            Text.from_markup("[bold]Keybindings[/]  [white]press Esc to close[/]"),
            tbl,
            Text("To copy data out, use the CLI (agentic-metric report / today / week / month).", style="white"),
        )
