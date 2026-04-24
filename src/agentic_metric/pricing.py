"""Model pricing table and cost estimation."""

from __future__ import annotations

import json
import logging

from .config import PRICING_FILE

log = logging.getLogger(__name__)

# (input, output, cache_read, cache_write) — USD per million tokens.
# Only OpenAI / Anthropic / Google Gemini. Prices verified against the
# official pricing pages on 2026-04-23:
#   https://openai.com/api/pricing/
#   https://platform.claude.com/docs/en/docs/about-claude/pricing
#   https://ai.google.dev/gemini-api/docs/pricing
# Cache-write uses the 5-minute rate for Anthropic (matches Claude Code's
# default cache TTL). Gemini's tiered pricing (≤200k vs >200k) is
# represented with the ≤200k rate — good enough for a personal tracker.
_BUILTIN_PRICING: dict[str, tuple[float, float, float, float]] = {
    # ── Anthropic Claude ──
    "claude-opus-4-7":       (5.0,  25.0, 0.50,  6.25),
    "claude-opus-4-6":       (5.0,  25.0, 0.50,  6.25),
    "claude-opus-4-5":       (5.0,  25.0, 0.50,  6.25),
    "claude-opus-4-1":       (15.0, 75.0, 1.50, 18.75),
    "claude-opus-4":         (15.0, 75.0, 1.50, 18.75),
    "claude-sonnet-4-6":     (3.0,  15.0, 0.30,  3.75),
    "claude-sonnet-4-5":     (3.0,  15.0, 0.30,  3.75),
    "claude-sonnet-4":       (3.0,  15.0, 0.30,  3.75),
    "claude-sonnet-3-7":     (3.0,  15.0, 0.30,  3.75),
    "claude-3-7-sonnet":     (3.0,  15.0, 0.30,  3.75),
    "claude-3-5-sonnet":     (3.0,  15.0, 0.30,  3.75),
    "claude-haiku-4-5":      (1.0,   5.0, 0.10,  1.25),
    "claude-haiku-3-5":      (0.80,  4.0, 0.08,  1.00),
    "claude-3-5-haiku":      (0.80,  4.0, 0.08,  1.00),
    "claude-3-opus":         (15.0, 75.0, 1.50, 18.75),
    "claude-3-haiku":        (0.25, 1.25, 0.03,  0.30),
    # ── OpenAI ──
    "gpt-5.4-pro":           (30.0, 180.0, 0.0,  0.0),
    "gpt-5.4-mini":          (0.75,   4.5, 0.075, 0.0),
    "gpt-5.4-nano":          (0.20,  1.25, 0.02,  0.0),
    "gpt-5.4":               (2.5,  15.0,  0.25,  0.0),
    "gpt-5.3-codex":         (1.75, 14.0,  0.175, 0.0),
    "gpt-5.3-chat-latest":   (1.75, 14.0,  0.175, 0.0),
    "gpt-5.3":               (1.75, 14.0,  0.175, 0.0),
    # ── Google Gemini ──
    "gemini-3.1-pro":        (2.00, 12.00, 0.20, 0.0),
    "gemini-3.1-flash-lite": (0.25,  1.50, 0.025, 0.0),
    "gemini-3-pro":          (2.00, 12.00, 0.20, 0.0),
    "gemini-3-flash":        (0.50,  3.00, 0.05, 0.0),
    "gemini-2.5-pro":        (1.25, 10.00, 0.125, 0.0),
    "gemini-2.5-flash":      (0.30,  2.50, 0.03, 0.0),
    "gemini-2.5-flash-lite": (0.10,  0.40, 0.01, 0.0),
    "gemini-2.0-flash":      (0.10,  0.40, 0.025, 0.0),
    "gemini-2.0-flash-lite": (0.075, 0.30, 0.0,  0.0),
    # ── Moonshot Kimi ──
    "kimi-k2.6":             (0.95,  4.00, 0.16, 0.0),
    # ── Zhipu GLM ──
    "glm-5.1":               (0.95,  3.15, 0.10, 0.0),
}

# Family-based fallback: prefix → pricing tuple.
# Used when an exact/prefix match fails, so new model revisions get a
# reasonable estimate before they're added to the table above. Prefixes
# are tried in list order, so put more specific ones first.
_FAMILY_FALLBACK: list[tuple[str, tuple[float, float, float, float]]] = [
    ("claude-opus",   (5.0,  25.0, 0.50,  6.25)),
    ("claude-sonnet", (3.0,  15.0, 0.30,  3.75)),
    ("claude-haiku",  (1.0,   5.0, 0.10,  1.25)),
    ("gpt-5.4",       (2.5,  15.0, 0.25,  0.0)),
    ("gpt-5",         (1.75, 14.0, 0.175, 0.0)),
    ("gemini-3",      (2.0,  12.0, 0.20,  0.0)),
    ("gemini-2.5",    (1.25, 10.0, 0.125, 0.0)),
    ("gemini-2",      (0.30,  2.5, 0.03,  0.0)),
]

_DEFAULT_PRICING = (3.0, 15.0, 0.30, 3.75)  # fallback to mid-range (sonnet)

_MODEL_ALIASES: dict[str, str] = {
    "claude-4.5-sonnet-thinking": "claude-sonnet-4-5",
    "claude-4.5-opus-high-thinking": "claude-opus-4-5",
    "gpt-5.1-codex-max": "gpt-5.1-codex",
}

_PRICING_FINGERPRINT_VERSION = 1

# Track warned models to avoid spamming logs
_warned_models: set[str] = set()

# ── User pricing file I/O (with mtime-based memo cache) ────────────


_user_cache: dict[str, tuple[float, float, float, float]] | None = None
_user_cache_mtime: float = -1.0


def _load_user_pricing() -> dict[str, tuple[float, float, float, float]]:
    """Load user pricing overrides from JSON file, cached by mtime."""
    global _user_cache, _user_cache_mtime

    if not PRICING_FILE.exists():
        if _user_cache is not None:
            _user_cache = None
            _user_cache_mtime = -1.0
        return {}

    try:
        mtime = PRICING_FILE.stat().st_mtime
    except OSError:
        return _user_cache or {}

    if _user_cache is not None and mtime == _user_cache_mtime:
        return _user_cache

    try:
        data = json.loads(PRICING_FILE.read_text())
        result: dict[str, tuple[float, float, float, float]] = {}
        for model, vals in data.items():
            result[model] = (
                float(vals[0]), float(vals[1]),
                float(vals[2]), float(vals[3]),
            )
        _user_cache = result
        _user_cache_mtime = mtime
        return result
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
        log.warning("Failed to parse %s, ignoring user overrides", PRICING_FILE)
        return {}


def _save_user_pricing(
    overrides: dict[str, tuple[float, float, float, float]],
) -> None:
    """Save user pricing overrides to JSON file and invalidate cache."""
    global _user_cache, _user_cache_mtime
    PRICING_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {model: list(vals) for model, vals in sorted(overrides.items())}
    PRICING_FILE.write_text(json.dumps(data, indent=2) + "\n")
    _user_cache = None
    _user_cache_mtime = -1.0


def set_user_pricing(
    model: str,
    input_price: float,
    output_price: float,
    cache_read_price: float = 0.0,
    cache_write_price: float = 0.0,
) -> None:
    """Add or update a user pricing override."""
    overrides = _load_user_pricing()
    model = normalize_model(model)
    overrides[model] = (input_price, output_price, cache_read_price, cache_write_price)
    _save_user_pricing(overrides)


def remove_user_pricing(model: str) -> bool:
    """Remove a user pricing override. Returns True if it existed."""
    overrides = _load_user_pricing()
    model = normalize_model(model)
    if model not in overrides:
        return False
    del overrides[model]
    _save_user_pricing(overrides)
    return True


def reset_all_user_pricing() -> None:
    """Remove all user pricing overrides."""
    global _user_cache, _user_cache_mtime
    if PRICING_FILE.exists():
        PRICING_FILE.unlink()
    _user_cache = None
    _user_cache_mtime = -1.0


# ── Public API ─────────────────────────────────────────────────────

# Kept as a public alias so existing ``from pricing import PRICING`` still works
PRICING = _BUILTIN_PRICING


def normalize_model(name: str) -> str:
    """Normalize external model names to our pricing keys."""
    if not name:
        return ""
    return _MODEL_ALIASES.get(name, name)


def get_pricing(model: str) -> tuple[float, float, float, float]:
    """Look up pricing: user overrides → builtin prefix match → family fallback → default.

    Prefix matching is done longest-prefix-first to ensure ``gpt-5.4-mini``
    matches its own entry before falling back to ``gpt-5.4``.
    """
    model = normalize_model(model)

    # 1. User overrides (exact match only)
    user = _load_user_pricing()
    if model in user:
        return user[model]

    # 2. Builtin (prefix match — longest prefix first)
    for prefix, pricing in sorted(_BUILTIN_PRICING.items(), key=lambda x: len(x[0]), reverse=True):
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


def get_pricing_fingerprint() -> str:
    """Return a stable fingerprint for repricing stored sessions."""
    payload = {
        "version": _PRICING_FINGERPRINT_VERSION,
        "builtin": sorted((model, list(prices)) for model, prices in _BUILTIN_PRICING.items()),
        "user": sorted((model, list(prices)) for model, prices in _load_user_pricing().items()),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def estimate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Estimate API-equivalent cost in USD.

    ``input_tokens`` must NOT include cached tokens — collectors are
    responsible for stripping cached portions before storing, per each
    provider's API semantics (Anthropic: already separate; OpenAI:
    ``input_tokens`` is total, subtract ``cached_input_tokens``).
    """
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
