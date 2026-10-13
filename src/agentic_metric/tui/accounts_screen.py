"""Read-only account equivalents and recorded payments for the selected range."""

from collections.abc import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..accounts import format_account_report


class AccountsScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,a", "dismiss", "Close", key_display="Esc")]
    DEFAULT_CSS = """
    AccountsScreen { align: center middle; }
    #accounts-box { width: 92%; height: 90%; border: round $primary; background: $surface; padding: 1 2; }
    #accounts-heading { height: auto; margin-bottom: 1; }
    #accounts-scroll { height: 1fr; }
    #accounts-content { height: auto; }
    #accounts-footer { height: auto; margin-top: 1; }
    """

    def __init__(self, get_report: Callable[[], dict]) -> None:
        super().__init__()
        self._get_report = get_report

    def compose(self) -> ComposeResult:
        with Vertical(id="accounts-box"):
            yield Static("Account costs · Esc to close", id="accounts-heading", markup=False)
            with VerticalScroll(id="accounts-scroll"):
                yield Static(Text(format_account_report(self._get_report())), id="accounts-content")
            yield Static("Usage follows background sync. Labels/payments: agentic-metric accounts --help", id="accounts-footer", markup=False)

    def on_mount(self) -> None:
        self.set_interval(3, self.refresh_report)

    def refresh_report(self) -> None:
        self.query_one("#accounts-content", Static).update(Text(format_account_report(self._get_report())))
