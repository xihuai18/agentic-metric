"""Keep the README builtin-pricing tables in sync with ``pricing.py``.

The README pricing tables used to be hand-maintained and drifted from the code
(missing Gemini models, a Claude row absent from the Chinese README). Here the
table rows are generated from ``_BUILTIN_PRICING`` and checked into both README
files between ``<!-- pricing:<vendor>:start/end -->`` markers. Run with
``UPDATE_README_PRICING=1`` to regenerate the blocks after changing pricing.
"""

from __future__ import annotations

import os
from pathlib import Path

from agentic_metric.pricing import _BUILTIN_PRICING

REPO_ROOT = Path(__file__).resolve().parents[1]
README_FILES = [REPO_ROOT / "README.md", REPO_ROOT / "README-CN.md"]

# (marker vendor key, model-id prefix). Order matches the README sections.
_VENDORS = [
    ("anthropic", "claude-"),
    ("openai", "gpt-"),
    ("gemini", "gemini-"),
]


def _money(value: float) -> str:
    """Format a per-1M-token price like the README: ``$5.00``, ``$0.075``."""
    text = f"{value:.3f}".rstrip("0")
    integer, _, frac = text.partition(".")
    return f"${integer}.{frac.ljust(2, '0')}"


def _generate_rows(prefix: str) -> str:
    """Build markdown rows for one vendor, merging identical price tuples.

    Models that share an exact (input, output, cache-read, cache-write) tuple
    collapse onto one ``name / name`` row, preserving first-seen order — this
    reproduces the README's grouped style without hand maintenance.
    """
    groups: dict[tuple, list[str]] = {}
    for model, price in _BUILTIN_PRICING.items():
        if not model.startswith(prefix):
            continue
        groups.setdefault(tuple(price), []).append(model)

    rows = []
    for price, names in groups.items():
        cache_write = "—" if price[3] == 0.0 else _money(price[3])
        rows.append(
            f"| {' / '.join(names)} | {_money(price[0])} | {_money(price[1])} "
            f"| {_money(price[2])} | {cache_write} |"
        )
    return "\n".join(rows)


def _markers(vendor: str) -> tuple[str, str]:
    return (f"<!-- pricing:{vendor}:start -->", f"<!-- pricing:{vendor}:end -->")


def _extract_block(text: str, vendor: str) -> str:
    start, end = _markers(vendor)
    assert start in text and end in text, f"missing {vendor} markers"
    body = text.split(start, 1)[1].split(end, 1)[0]
    return body.strip("\n")


def _replace_block(text: str, vendor: str, rows: str) -> str:
    start, end = _markers(vendor)
    head, _, rest = text.partition(start)
    _, _, tail = rest.partition(end)
    return f"{head}{start}\n{rows}\n{end}{tail}"


def test_readme_pricing_tables_in_sync():
    """Both READMEs' pricing rows must match the generated tables.

    Set ``UPDATE_README_PRICING=1`` to rewrite the marker blocks in place.
    """
    update = os.environ.get("UPDATE_README_PRICING") == "1"
    generated = {vendor: _generate_rows(prefix) for vendor, prefix in _VENDORS}

    for path in README_FILES:
        text = path.read_text(encoding="utf-8")
        if update:
            for vendor, _prefix in _VENDORS:
                text = _replace_block(text, vendor, generated[vendor])
            path.write_text(text, encoding="utf-8")
            continue
        for vendor, _prefix in _VENDORS:
            assert _extract_block(text, vendor) == generated[vendor], (
                f"{path.name} {vendor} pricing table out of sync; "
                f"run UPDATE_README_PRICING=1 pytest to refresh"
            )
