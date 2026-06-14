"""Pure formatting helpers — no Rich, no Typer, no side effects."""

from __future__ import annotations

import os
from pathlib import Path


def fmt_cost(cost: float | None, *, unknown: bool = False) -> str:
    cost_value = 0.0 if cost is None else cost
    if unknown and cost_value > 0:
        return f"{fmt_cost(cost_value)} + ?"
    if unknown or cost is None:
        return "?"
    if cost_value >= 1.0:
        return f"${cost_value:,.2f}"
    return f"${cost_value:.3f}"


def fmt_tokens(n: int) -> str:
    """Compact token count: 1234 -> 1.2K, 1234567 -> 1.2M, 1.2B."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def clip(value: str, max_len: int) -> str:
    value = value or ""
    if len(value) <= max_len:
        return value
    return value[: max(0, max_len - 1)] + "…"


def short_session_id(session_id: str) -> str:
    if not session_id:
        return "(unknown)"
    # Codex session IDs use "prefix:suffix" format.
    if ":" in session_id:
        head, tail = session_id.split(":", 1)
        return f"{head[:8]}:{tail[:10]}"
    return session_id[:8]


def short_path(path: str, max_len: int = 42) -> str:
    if not path:
        return "(unspecified)"
    path = shorten_home(path)
    return clip(path, max_len)


def split_data_root(data_root: str) -> tuple[str, str]:
    """Return ``(source, root)`` for local or SSH-backed data roots."""
    data_root = data_root or ""
    if data_root.startswith("ssh://"):
        rest = data_root[len("ssh://"):]
        source, sep, root = rest.partition("/")
        return source or "remote", root if sep else ""
    return "local", data_root


def source_label(data_root: str) -> str:
    return split_data_root(data_root)[0]


def root_label(data_root: str, max_len: int = 42) -> str:
    _, root = split_data_root(data_root)
    return short_path(root or "(unspecified)", max_len=max_len)


def source_root_label(data_root: str, max_len: int = 42) -> str:
    source, root = split_data_root(data_root)
    root_text = short_path(root or "(unspecified)", max_len=max_len)
    if source == "local":
        return root_text
    return clip(f"{source}:{root_text}", max_len)


def source_prefixed_path(project_path: str, data_root: str, max_len: int = 72) -> str:
    source = source_label(data_root)
    path = short_path(project_path or "(unspecified)", max_len=max_len)
    if source == "local":
        return path
    prefix = f"{source}:"
    return clip(f"{prefix}{path}", max_len)


def shorten_home(path: str) -> str:
    if not path:
        return path
    try:
        home = os.path.normpath(str(Path.home()))
        candidate = os.path.normpath(os.path.expanduser(path))
        home_key = os.path.normcase(home)
        candidate_key = os.path.normcase(candidate)
        if os.path.commonpath([home_key, candidate_key]) == home_key:
            rel = os.path.relpath(candidate, home)
            return "~" if rel == "." else str(Path("~") / rel)
    except (OSError, ValueError):
        pass
    return path


def has_unknown_cost(row: dict | None) -> bool:
    return bool(row and (row.get("unknown_cost_count") or 0) > 0)


def has_cost_signal(row: dict, *, cost_key: str = "estimated_cost_usd") -> bool:
    return (row.get(cost_key) or 0) > 0 or has_unknown_cost(row)


def share_pct(
    cost: float,
    total_cost: float,
    *,
    unknown: bool = False,
    total_unknown: bool = False,
) -> str:
    if unknown or total_unknown or total_cost <= 0:
        return "—"
    return f"{(100.0 * cost / total_cost):.1f}%"


def share_suffix(
    cost: float,
    total_cost: float,
    *,
    unknown: bool = False,
    total_unknown: bool = False,
) -> str:
    pct = share_pct(cost, total_cost, unknown=unknown, total_unknown=total_unknown)
    return "" if pct == "—" else f" ({pct})"


def sum_tokens(r: dict) -> int:
    return (
        (r.get("input_tokens") or 0)
        + (r.get("output_tokens") or 0)
        + (r.get("cache_read_tokens") or 0)
        + (r.get("cache_creation_tokens") or 0)
    )


def cache_tokens(r: dict) -> int:
    return (r.get("cache_read_tokens") or 0) + (r.get("cache_creation_tokens") or 0)


def cache_hit_rate(r: dict) -> float:
    """Return cache-read share of prompt-side tokens in %, or -1 if N/A."""
    cache_read = r.get("cache_read_tokens")
    if cache_read is None:
        cache_read = r.get("cache_read")
    if cache_read is None:
        cache_read = r.get("cache") or 0

    input_tok = r.get("input_tokens")
    if input_tok is None:
        input_tok = r.get("input") or 0

    cache_create = r.get("cache_creation_tokens")
    if cache_create is None:
        cache_create = r.get("cache_write") or 0

    prompt_side = cache_read + input_tok + cache_create
    if prompt_side <= 0:
        return -1.0
    return 100.0 * cache_read / prompt_side


def cache_hit_rate_band(rate: float | int | None) -> str:
    """Return the shared display band for a cache-hit percentage."""
    if rate is None or rate < 0:
        return "none"
    if rate >= 95:
        return "excellent"
    if rate >= 90:
        return "good"
    if rate >= 80:
        return "warn"
    return "low"


def token_summary(r: dict) -> dict:
    """Return canonical token totals used by CLI and TUI summaries."""
    input_tokens = r.get("input_tokens") or 0
    output_tokens = r.get("output_tokens") or 0
    cache_read_tokens = r.get("cache_read_tokens") or 0
    cache_creation_tokens = r.get("cache_creation_tokens") or 0
    total_tokens = (
        input_tokens
        + output_tokens
        + cache_read_tokens
        + cache_creation_tokens
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "total_tokens": total_tokens,
        "cache_pct": cache_hit_rate(r),
    }


def time_bucket_label(row: dict) -> str:
    date_s = row.get("usage_date") or ""
    hour = int(row.get("usage_hour") or 0)
    return f"{date_s} {hour:02d}:00" if date_s else f"{hour:02d}:00"


def time_bucket_label_short(row: dict) -> str:
    date_s = row.get("usage_date") or ""
    hour = int(row.get("usage_hour") or 0)
    if len(date_s) == 10:
        date_s = date_s[5:]
    return f"{date_s} {hour:02d}" if date_s else f"{hour:02d}"
