"""Layout guards for the TUI summary row."""

from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.containers import Horizontal

from agentic_metric.tui.widgets import SummaryCell

CSS = """
#summary-row { height: auto; }
SummaryCell { width: 1fr; height: auto; margin: 0 1; padding: 0 2; border: round white; }
"""


class SummaryRowApp(App):
    CSS = CSS

    def compose(self) -> ComposeResult:
        with Horizontal(id="summary-row"):
            yield SummaryCell("TODAY", id="cell-today")
            yield SummaryCell("WEEK", id="cell-week")
            yield SummaryCell("MONTH", id="cell-month")


def _displayed(cell: SummaryCell) -> list[str]:
    """Render a cell the way the terminal shows it, wrapping included."""
    buffer = io.StringIO()
    Console(width=cell.content_size.width, file=buffer, no_color=True).print(
        cell.render()
    )
    return [line.rstrip() for line in buffer.getvalue().splitlines()]


async def _cell_lines(width: int) -> list[list[str]]:
    app = SummaryRowApp()
    async with app.run_test(size=(width, 20), notifications=False) as pilot:
        for cell_id, tokens in (
            ("cell-today", 1_100_000),
            ("cell-week", 2_500_000),
            ("cell-month", 2_800_000),
        ):
            cell = app.query_one(f"#{cell_id}", SummaryCell)
            cell.update_data(15.95, 6, tokens, prev_cost=5.7, cache_pct=77,
                             turns=75, requests=153)
        await pilot.pause()
        return [
            _displayed(app.query_one(f"#{cell_id}", SummaryCell))
            for cell_id in ("cell-today", "cell-week", "cell-month")
        ]


def _line_starting_with(lines: list[str], prefix: str) -> str:
    matches = [line for line in lines if line.startswith(prefix)]
    assert matches, f"no line starting with {prefix!r} in {lines}"
    return matches[0]


@pytest.mark.parametrize("width", [80, 92, 98, 99, 100, 101, 102, 110, 132, 160])
def test_summary_cells_wrap_identically(width):
    """A one-column ``1fr`` remainder must not give one cell a different shape."""
    lines = asyncio.run(_cell_lines(width))
    shapes = [len(cell) for cell in lines]
    assert len(set(shapes)) == 1, lines


def test_narrow_summary_cell_keeps_the_cache_label_with_its_number():
    lines = asyncio.run(_cell_lines(80))[0]
    assert _line_starting_with(lines, "Cache %") == "Cache % 77%"
    assert _line_starting_with(lines, "Token ") == "Token 1.1M"


def test_wide_summary_cell_keeps_tokens_and_cache_on_one_line():
    lines = asyncio.run(_cell_lines(160))[0]
    assert _line_starting_with(lines, "Token ") == "Token 1.1M  ·  Cache % 77%"

