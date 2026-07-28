"""Layout guards for the CLI report tables.

Metrics must never be ellipsized: a cost rendered as ``$6,850.…`` is worse than
a truncated project path, so the label columns give up the room first and dense
tables fold their secondary counters away on narrow terminals.
"""

from __future__ import annotations

import io
from contextlib import contextmanager

import pytest
from rich import box
from rich.cells import cell_len
from rich.console import Console
from rich.table import Table
from rich.text import Text

from agentic_metric import cli

WIDTHS = [60, 68, 76, 80, 88, 96, 100, 120, 160, 200]

# Distinctive so a partial match cannot pass by accident.
COSTS = [6850.59, 130.12, 12.13]
COST_TEXTS = ["$6,850.59", "$130.12", "$12.13"]
MODELS = ["gpt-5.6-sol", "claude-opus-5", "codex-auto-review"]

LONG_PATH = "/data/workspace/garyyfwang_reasoning_data/single_turn_collect"


@contextmanager
def _console_at(width: int):
    original = cli.console
    cli.console = Console(width=width, file=io.StringIO(), no_color=True)
    try:
        yield
    finally:
        cli.console = original


def _render(table: Table | None, width: int) -> str:
    assert table is not None
    buffer = io.StringIO()
    Console(width=width, file=buffer, no_color=True).print(table)
    return buffer.getvalue()


def _rows() -> list[dict]:
    return [
        {
            "host": "devcloud-team-with-a-long-hostname",
            "agent_type": "claude_code" if i else "codex",
            "provider": "openai" if i else "ichat",
            "model": MODELS[i],
            "raw_model": None,
            "project_path": f"{LONG_PATH}/run-{i}",
            "data_root": "ssh://devcloud-team/data/home/user/.codex",
            "session_count": 216 - i,
            "user_turns": 7130 - i,
            "message_count": 8280 - i,
            "input_tokens": 129_000_000,
            "output_tokens": 21_300_000,
            "cache_read_tokens": 6_900_000_000,
            "cache_creation_tokens": 32_600_000,
            "estimated_cost_usd": cost,
            "unknown_cost_count": 0,
        }
        for i, cost in enumerate(COSTS)
    ]


def _periodic() -> list[dict]:
    return [
        {
            "label": f"0{i}:00",
            "sublabel": "Tue",
            "session_count": 380 - i,
            "tokens": 58_900_000_000,
            "cost": cost,
            "unknown_cost_count": 0,
        }
        for i, cost in enumerate(COSTS)
    ]


def _tables(width: int) -> list[Table | None]:
    rows = _rows()
    return [
        cli._build_dimension_table("By host", "Host", rows, "host", max_label_width=18),
        cli._build_dimension_table("By model", "Model", rows, "model", max_label_width=32),
        cli._build_by_agent_model_table(rows),
        cli._build_cross_table(
            "By provider × model", "Provider", rows,
            lambda r: r.get("provider") or "—", a_width=cli._scale_width(10, 16),
        ),
        cli._build_cross_table(
            "By project × model", "Project", rows,
            lambda r: r["project_path"], a_width=max(28, min(120, width - 60)),
            model_width=cli._scale_width(14, 24),
        ),
        cli._build_top_projects_table(rows),
        cli._build_project_agent_table(rows),
        cli._build_periodic_table(_periodic(), "today"),
    ]


@pytest.mark.parametrize("width", WIDTHS)
def test_report_tables_fit_the_console(width):
    with _console_at(width):
        rendered = [_render(tbl, width) for tbl in _tables(width)]
    for text in rendered:
        for line in text.splitlines():
            assert cell_len(line.rstrip()) <= width, line


@pytest.mark.parametrize("width", WIDTHS)
def test_report_tables_never_ellipsize_costs(width):
    with _console_at(width):
        rendered = [_render(tbl, width) for tbl in _tables(width)]
    for text in rendered:
        for cost in COST_TEXTS:
            assert cost in text, f"{cost} missing at width {width}:\n{text}"


@pytest.mark.parametrize("width", WIDTHS)
def test_report_tables_never_ellipsize_token_counts(width):
    with _console_at(width):
        text = _render(cli._build_top_projects_table(_rows()), width)
    if width < 96:
        # 6.9B read + 129.0M in + 21.3M out + 32.6M writes.
        assert "7.1B" in text, text
    else:
        for value in ("129.0M", "21.3M", "6.9B"):
            assert value in text, text


def test_wide_console_keeps_the_full_project_path():
    with _console_at(200):
        text = _render(cli._build_top_projects_table(_rows()), 200)
    assert f"devcloud-team:{LONG_PATH}/run-0" in text


def test_narrow_model_table_merges_rows_per_model():
    rows = _rows()
    # Same model reached through two different sources.
    rows[1]["model"] = rows[2]["model"] = "gpt-5.6-sol"
    rows[1]["data_root"] = "ssh://other-host/data/home/user/.codex"
    with _console_at(70):
        text = _render(cli._build_by_agent_model_table(rows), 70)
    assert text.count("gpt-5.6-sol") == 1
    # 6850.59 + 130.12 + 12.13 all merged into one row.
    assert "$6,992.84" in text
    assert "Source" not in text


def test_wide_model_table_keeps_the_grouping_columns():
    with _console_at(120):
        text = _render(cli._build_by_agent_model_table(_rows()), 120)
    assert "Source" in text
    assert "devcloud-team" in text
    for cost in COST_TEXTS:
        assert cost in text


def test_periodic_table_drops_the_cost_bar_when_cramped():
    with _console_at(58):
        cramped = cli._build_periodic_table(_periodic(), "today")
    with _console_at(100):
        roomy = cli._build_periodic_table(_periodic(), "today")
    assert cramped is not None and roomy is not None
    assert len(cramped.columns) == 5
    assert len(roomy.columns) == 6


def test_dropping_the_cost_bar_matches_a_table_built_without_it():
    """``columns.pop()`` is only sound for the *last* column — Rich derives
    padding and edge handling from a column's index. Prove a popped table
    renders exactly like one that never had that column."""

    def build(with_bar: bool) -> Table:
        tbl = Table(show_header=True, box=box.SIMPLE_HEAVY, pad_edge=False)
        tbl.add_column("Hour")
        tbl.add_column("Sessions", justify="right")
        tbl.add_column("Cache %", justify="right", min_width=7)
        tbl.add_column("Tokens", justify="right")
        tbl.add_column("Cost", justify="right")
        if with_bar:
            tbl.add_column("", justify="left")
        for i, cost in enumerate(COST_TEXTS):
            values = [f"0{i}:00", f"{380 - i:,}", "98%", "58.9B", cost]
            if with_bar:
                bar = Text()
                bar.append("█" * i, style="red")
                bar.append("░" * (14 - i), style="blue")
                values.append(bar)
            tbl.add_row(*values)
        return tbl

    popped = build(True)
    popped.columns.pop()
    never = build(False)
    for width in (20, 40, 58, 80, 120):
        assert _render(popped, width) == _render(never, width)


@pytest.mark.parametrize("width", [44, 60, 80, 100, 140, 200])
def test_fit_text_columns_converges_on_pathological_labels(width):
    """Long labels must not exhaust the fitter's iteration budget."""
    rows = _rows()
    for i, row in enumerate(rows):
        row["project_path"] = f"/{'segment' * 12}/{i}"
        row["model"] = "Unknown: " + "m" * 60
        row["agent_type"] = "agent-" + "n" * 40
        row["host"] = "host-" + "h" * 50
    with _console_at(width):
        rendered = [_render(tbl, width) for tbl in _tables(width)]
    for text in rendered:
        for line in text.splitlines():
            assert cell_len(line.rstrip()) <= width, line
        for cost in COST_TEXTS:
            assert cost in text, f"{cost} missing at width {width}:\n{text}"


def test_fit_text_columns_leaves_a_table_that_already_fits_alone():
    with _console_at(200):
        table = cli._build_top_projects_table(_rows())
    assert table is not None
    assert table.columns[0].max_width == max(24, min(140, 200 - 44))


def _header_cells() -> list[Text]:
    cost = Text("COST")
    cost.append("\n")
    cost.append("$7,266.49")
    cost.append("\n")
    cost.append("▼ -89% vs $65,084.09")
    return [
        cost,
        cli._stat("Cache %", "98%", "green"),
        cli._stat("Sessions", "270", "magenta"),
        cli._stat("Requests", "8,954", "cyan"),
        cli._stat("Turns", "7,171", "cyan"),
    ]


@pytest.mark.parametrize("width", [40, 46, 50, 54, 58, 62, 70, 80, 100])
def test_header_stats_never_ellipsize_the_headline_numbers(width):
    inner = min(width, cli.MAX_PANEL_WIDTH) - 6
    with _console_at(width):
        row = cli._fit_stats_row(_header_cells(), inner)
        buffer = io.StringIO()
        Console(width=inner, file=buffer, no_color=True).print(row)
    rendered = buffer.getvalue()
    for value in ("$7,266.49", "$65,084.09", "98%", "270", "8,954", "7,171"):
        assert value in rendered, f"{value} missing at width {width}:\n{rendered}"


def test_wide_header_keeps_every_stat_on_one_row():
    with _console_at(100):
        row = cli._fit_stats_row(_header_cells(), 94)
        buffer = io.StringIO()
        Console(width=94, file=buffer, no_color=True).print(row)
    first = buffer.getvalue().splitlines()[0]
    for label in ("COST", "CACHE %", "SESSIONS", "REQUESTS", "TURNS"):
        assert label in first, first
