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

# (section, [(keys, description), ...]). The footer only shows the
# high-traffic keys, so this is where the hidden ones (t/w/m, PageUp/Down,
# .) are documented. Sections render with a divider between them.
_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Navigation", [
        ("← → / h l", "Switch view (Today / Week / Month)"),
        ("PageUp / PageDown", "Move time range earlier / later (PgUp = previous)"),
        (". / 0", "Jump back to now (reset offset)"),
        ("t / w / m", "Focus Today / Week / Month directly"),
        ("↑ ↓ / k j", "Scroll the breakdown panel"),
        ("Ctrl+B / Ctrl+F", "Scroll the breakdown panel"),
    ]),
    ("Data", [
        ("R", "Toggle fast auto-refresh (live sync)"),
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
