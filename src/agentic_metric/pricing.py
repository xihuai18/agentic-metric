"""Model pricing table and cost estimation."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import PRICING_FILE

log = logging.getLogger(__name__)

# (input, output, cache_read, cache_write) — USD per million tokens
_BUILTIN_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4-6":   (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-5":   (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-1":   (15.0, 75.0, 1.50, 18.75),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-4":   (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5":  (1.0,  5.0, 0.10, 1.25),
    "gpt-5.3-codex":     (1.75, 14.0, 0.175, 0.0),
    "gpt-5.2":           (1.75, 14.0, 0.175, 0.0),
    "gpt-5.1-codex":     (1.25, 10.0, 0.125, 0.0),
    "gpt-4o":            (2.5, 10.0, 1.25, 0.0),
    "gemini-3-pro":      (2.0, 12.0, 0.20, 0.0),
    "o3":                (2.0,  8.0, 0.50, 0.0),
    "o4-mini":           (1.1,  4.4, 0.275, 0.0),
    "kimi-k2":           (0.60, 2.50, 0.15, 0.0),
    "kimi-k2.5":         (0.60, 3.00, 0.15, 0.0),
    "coder-model":       (0.65, 3.25, 0.13, 0.0),
}

# Family-based fallback: prefix → pricing tuple
# Used when an exact/prefix match fails, to avoid defaulting to Opus for all
_FAMILY_FALLBACK: list[tuple[str, tuple[float, float, float, float]]] = [
    ("claude-opus",   (5.0, 25.0, 0.50, 6.25)),
    ("claude-sonnet", (3.0, 15.0, 0.30, 3.75)),
    ("claude-haiku",  (1.0,  5.0, 0.10, 1.25)),
    ("gpt-",          (2.0, 10.0, 0.50, 0.0)),
    ("o4-",           (1.1,  4.4, 0.275, 0.0)),
    ("gemini-",       (2.0, 12.0, 0.20, 0.0)),
    ("kimi-",         (0.60, 2.50, 0.15, 0.0)),
]

_DEFAULT_PRICING = (3.0, 15.0, 0.30, 3.75)  # fallback to mid-range (sonnet)

_MODEL_ALIASES: dict[str, str] = {
    "claude-4.5-sonnet-thinking": "claude-sonnet-4-5",
    "claude-4.5-opus-high-thinking": "claude-opus-4-5",
    "gpt-5.1-codex-max": "gpt-5.1-codex",
}

# Copilot Chat result.details display name → our pricing key
_COPILOT_MODEL_MAP: dict[str, str] = {
    "claude opus 4.6": "claude-opus-4-6",
    "claude opus 4.5": "claude-opus-4-5",
    "claude sonnet 4.6": "claude-sonnet-4-6",
    "claude sonnet 4.5": "claude-sonnet-4-5",
    "claude sonnet 4": "claude-sonnet-4",
    "claude sonnet 3.5": "claude-sonnet-4",
    "claude haiku 4.5": "claude-haiku-4-5",
    "gpt-5.2": "gpt-5.2",
    "gpt-5 mini": "gpt-4o",
    "gpt-4o mini": "gpt-4o",
    "gpt-4o": "gpt-4o",
    "o3": "o3",
    "o4-mini": "o4-mini",
    "gemini 3 pro": "gemini-3-pro",
}

# Track warned models to avoid spamming logs
_warned_models: set[str] = set()

# ── User pricing file I/O ──────────────────────────────────────────


def _load_user_pricing() -> dict[str, tuple[float, float, float, float]]:
    """Load user pricing overrides from JSON file."""
    if not PRICING_FILE.exists():
        return {}
    try:
        data = json.loads(PRICING_FILE.read_text())
        result: dict[str, tuple[float, float, float, float]] = {}
        for model, vals in data.items():
            result[model] = (
                float(vals[0]), float(vals[1]),
                float(vals[2]), float(vals[3]),
            )
        return result
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
        log.warning("Failed to parse %s, ignoring user overrides", PRICING_FILE)
        return {}


def _save_user_pricing(
    overrides: dict[str, tuple[float, float, float, float]],
) -> None:
    """Save user pricing overrides to JSON file."""
    PRICING_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {model: list(vals) for model, vals in sorted(overrides.items())}
    PRICING_FILE.write_text(json.dumps(data, indent=2) + "\n")


def set_user_pricing(
    model: str,
    input_price: float,
    output_price: float,
    cache_read_price: float = 0.0,
    cache_write_price: float = 0.0,
) -> None:
    """Add or update a user pricing override."""
    overrides = _load_user_pricing()
    overrides[model] = (input_price, output_price, cache_read_price, cache_write_price)
    _save_user_pricing(overrides)


def remove_user_pricing(model: str) -> bool:
    """Remove a user pricing override. Returns True if it existed."""
    overrides = _load_user_pricing()
    if model not in overrides:
        return False
    del overrides[model]
    _save_user_pricing(overrides)
    return True


def reset_all_user_pricing() -> None:
    """Remove all user pricing overrides."""
    if PRICING_FILE.exists():
        PRICING_FILE.unlink()


# ── Public API (backward compatible) ───────────────────────────────

# Kept as a public alias so existing ``from pricing import PRICING`` still works
PRICING = _BUILTIN_PRICING


def normalize_copilot_model(details: str) -> str:
    """Normalize Copilot Chat result.details string to a pricing key.

    Input looks like ``"Claude Haiku 4.5 • 1x"`` or ``"GPT-5 mini • 0.9x"``.
    """
    if not details:
        return ""
    # Strip the "• Nx" suffix and lowercase
    name = details.split("•")[0].strip().lower()
    # Strip "(Preview)" or similar tags
    name = name.replace("(preview)", "").strip()
    return _COPILOT_MODEL_MAP.get(name, "")


def normalize_model(name: str) -> str:
    """Normalize external model names to our pricing keys."""
    if not name:
        return ""
    return _MODEL_ALIASES.get(name, name)


def get_pricing(model: str) -> tuple[float, float, float, float]:
    """Look up pricing: user overrides → builtin prefix match → family fallback → default."""
    # 1. User overrides (exact match)
    user = _load_user_pricing()
    if model in user:
        return user[model]
    # User overrides (prefix match)
    for prefix, pricing in user.items():
        if model.startswith(prefix):
            return pricing

    # 2. Builtin (prefix match)
    for prefix, pricing in _BUILTIN_PRICING.items():
        if model.startswith(prefix):
            return pricing

    # 3. Family fallback
    for family_prefix, pricing in _FAMILY_FALLBACK:
        if model.startswith(family_prefix):
            if model not in _warned_models:
                _warned_models.add(model)
                log.warning(
                    "Unknown model %r — using %s family pricing. "
                    "Run 'agentic-metric pricing set' to configure.",
                    model, family_prefix.rstrip("-"),
                )
            return pricing

    # 4. Default
    if model and model not in _warned_models:
        _warned_models.add(model)
        log.warning(
            "Unknown model %r — using default pricing ($%.1f/$%.1f per 1M tokens). "
            "Run 'agentic-metric pricing set' to configure.",
            model, _DEFAULT_PRICING[0], _DEFAULT_PRICING[1],
        )
    return _DEFAULT_PRICING


def get_all_pricing() -> dict[str, tuple[float, float, float, float]]:
    """Return merged pricing: builtin defaults overridden by user values."""
    merged = dict(_BUILTIN_PRICING)
    merged.update(_load_user_pricing())
    return merged


def estimate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Estimate API-equivalent cost in USD."""
    p_in, p_out, p_cr, p_cw = get_pricing(model)
    cost = (
        input_tokens * p_in
        + output_tokens * p_out
        + cache_read_tokens * p_cr
        + cache_creation_tokens * p_cw
    ) / 1_000_000
    return cost


def estimate_session_cost(session) -> float:
    """Estimate cost for a LiveSession object."""
    return estimate_cost(
        model=session.model,
        input_tokens=session.input_tokens,
        output_tokens=session.output_tokens,
        cache_read_tokens=session.cache_read_tokens,
        cache_creation_tokens=session.cache_creation_tokens,
    )
