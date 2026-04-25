"""Model pricing table and cost estimation."""

from __future__ import annotations

import json
import logging

from .config import PRICING_FILE

log = logging.getLogger(__name__)

# (input, output, cache_read, cache_write) — USD per million tokens.
# Core OpenAI / Anthropic / Google Gemini prices were verified against
# official pricing docs on 2026-04-25:
#   https://openai.com/api/pricing/
#   https://developers.openai.com/api/docs/models/gpt-5.4/
#   https://platform.claude.com/docs/en/docs/about-claude/pricing
#   https://ai.google.dev/gemini-api/docs/pricing
# Cache-write uses the 5-minute rate for Anthropic unless a collector observes
# a different cache duration. Provider speed/priority modes are intentionally
# ignored because the local histories this tool reads do not expose reliable
# non-standard markers.
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
    "gpt-5.5":               (5.0,  30.0,  0.50,  0.0),
    "gpt-5.4-mini":          (0.75,   4.5, 0.075, 0.0),
    "gpt-5.4-nano":          (0.20,  1.25, 0.02,  0.0),
    "gpt-5.4":               (2.5,  15.0,  0.25,  0.0),
    "gpt-5.2-codex":         (1.75, 14.0,  0.175, 0.0),
    "gpt-5.2-chat-latest":   (1.75, 14.0,  0.175, 0.0),
    "gpt-5.2":               (1.75, 14.0,  0.175, 0.0),
    "gpt-5.1-codex-max":     (1.25, 10.0,  0.125, 0.0),
    "gpt-5.1-codex":         (1.25, 10.0,  0.125, 0.0),
    "gpt-5.1-chat-latest":   (1.25, 10.0,  0.125, 0.0),
    "gpt-5.1":               (1.25, 10.0,  0.125, 0.0),
    "gpt-5-codex":           (1.25, 10.0,  0.125, 0.0),
    "gpt-5-chat-latest":     (1.25, 10.0,  0.125, 0.0),
    "gpt-5":                 (1.25, 10.0,  0.125, 0.0),
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

_MODEL_ALIASES: dict[str, str] = {
    "claude-4.5-sonnet-thinking": "claude-sonnet-4-5",
    "claude-4.5-opus-high-thinking": "claude-opus-4-5",
    "codex-auto-review": "gpt-5.3-codex",
    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
}

# Internal placeholder/system responses that should never be billed as a model.
_NON_BILLABLE_MODELS = {"<synthetic>"}

# Explicitly unsupported paid-model families. Keep these before builtin prefix
# matching so ``gpt-5.4-pro`` cannot accidentally inherit ``gpt-5.4`` pricing.
_UNKNOWN_MODEL_PREFIXES = (
    "gpt-5.5-pro",
    "gpt-5.4-pro",
    "gpt-5.3-pro",
    "gpt-5.2-pro",
    "gpt-5.1-pro",
    "gpt-5-pro",
)

_PRICING_FINGERPRINT_VERSION = 9

# Long-context pricing applies per request/prompt, not per stored hour/session.
# Collectors pass single-event usage into ``estimate_cost`` before aggregating
# buckets; aggregate-only callers get a best-effort fallback.
_LONG_CONTEXT_RULES: list[dict[str, object]] = [
    {
        "prefixes": ("gpt-5.5",),
        "threshold": 270_000,
        "prices": (10.0, 45.0, 1.0, 0.0),
    },
    {
        "prefixes": ("gpt-5.4",),
        "excluded_prefixes": ("gpt-5.4-mini", "gpt-5.4-nano"),
        "threshold": 272_000,
        "prices": (5.0, 22.5, 0.50, 0.0),
    },
    {
        "prefixes": ("gemini-3.1-pro",),
        "threshold": 200_000,
        "prices": (4.0, 18.0, 0.40, 0.0),
    },
    {
        "prefixes": ("gemini-2.5-pro",),
        "threshold": 200_000,
        "prices": (2.5, 15.0, 0.25, 0.0),
    },
    {
        "prefixes": ("claude-sonnet-4",),
        "excluded_prefixes": ("claude-sonnet-4-5", "claude-sonnet-4-6"),
        "threshold": 200_000,
        "prices": (6.0, 22.5, 0.60, 7.5),
    },
]

# Track warned models to avoid spamming logs
_warned_models: set[str] = set()


def _matches_model_prefix(model: str, prefix: str) -> bool:
    """Return True for an exact model id or a dated/preview variant."""
    return model == prefix or model.startswith(f"{prefix}-")


def _matches_any_model_prefix(model: str, prefixes: tuple[str, ...]) -> bool:
    return any(_matches_model_prefix(model, prefix) for prefix in prefixes)

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


def get_pricing(model: str) -> tuple[float, float, float, float] | None:
    """Look up pricing: user overrides → builtin prefix match → unknown.

    Prefix matching is done longest-prefix-first to ensure ``gpt-5.4-mini``
    matches its own entry before falling back to ``gpt-5.4``.
    """
    model = normalize_model(model)

    if model in _NON_BILLABLE_MODELS:
        return (0.0, 0.0, 0.0, 0.0)

    # 1. User overrides (exact match only)
    user = _load_user_pricing()
    if model in user:
        return user[model]

    if _matches_any_model_prefix(model, _UNKNOWN_MODEL_PREFIXES):
        return None

    # 2. Builtin (prefix match — longest prefix first)
    for prefix, pricing in sorted(_BUILTIN_PRICING.items(), key=lambda x: len(x[0]), reverse=True):
        if _matches_model_prefix(model, prefix):
            return pricing

    if model and model not in _warned_models:
        _warned_models.add(model)
        log.warning(
            "Unknown model %r — cost will be shown as '?'. "
            "Run 'agentic-metric pricing set' to configure pricing.",
            model,
        )
    return None


def is_model_priced(model: str) -> bool:
    """Return True when a model has explicit builtin or user pricing."""
    return get_pricing(model) is not None


def _long_context_prices(
    model: str,
    input_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> tuple[float, float, float, float] | None:
    """Return request-size pricing when this usage crosses a model threshold."""
    if input_tokens < 0 or cache_read_tokens < 0 or cache_creation_tokens < 0:
        return None

    model = normalize_model(model)
    if model in _load_user_pricing():
        return None
    if _matches_any_model_prefix(model, _UNKNOWN_MODEL_PREFIXES):
        return None
    total_input_tokens = input_tokens + cache_read_tokens + cache_creation_tokens
    for rule in _LONG_CONTEXT_RULES:
        prefixes = tuple(str(p) for p in rule["prefixes"])
        excluded = tuple(str(p) for p in rule.get("excluded_prefixes", ()))
        if not _matches_any_model_prefix(model, prefixes) or (
            excluded and _matches_any_model_prefix(model, excluded)
        ):
            continue
        if total_input_tokens > int(rule["threshold"]):
            return tuple(float(v) for v in rule["prices"])  # type: ignore[return-value]
    return None


def get_all_pricing() -> dict[str, tuple[float, float, float, float]]:
    """Return merged pricing: builtin defaults overridden by user values."""
    merged = dict(_BUILTIN_PRICING)
    merged.update(_load_user_pricing())
    return merged


def get_pricing_fingerprint() -> str:
    """Return a stable fingerprint for repricing stored sessions."""
    payload = {
        "version": _PRICING_FINGERPRINT_VERSION,
        "aliases": sorted(_MODEL_ALIASES.items()),
        "builtin": sorted((model, list(prices)) for model, prices in _BUILTIN_PRICING.items()),
        "long_context_rules": _LONG_CONTEXT_RULES,
        "non_billable": sorted(_NON_BILLABLE_MODELS),
        "unknown_model_prefixes": sorted(_UNKNOWN_MODEL_PREFIXES),
        "user": sorted((model, list(prices)) for model, prices in _load_user_pricing().items()),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def estimate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
    apply_long_context: bool = True,
) -> float | None:
    """Estimate API-equivalent cost in USD.

    ``input_tokens`` must NOT include cached tokens — collectors are
    responsible for stripping cached portions before storing, per each
    provider's API semantics (Anthropic: already separate; OpenAI:
    ``input_tokens`` is total, subtract ``cached_input_tokens``). Anthropic's
    optional 1-hour cache writes are a subset of ``cache_creation_tokens`` and
    are charged at the 1-hour prompt-cache multiplier when provided. Long-context
    rates are only correct for single-request usage; callers that only have
    hourly/session aggregates should pass ``apply_long_context=False``.
    """
    if (
        input_tokens <= 0
        and output_tokens <= 0
        and cache_read_tokens <= 0
        and cache_creation_tokens <= 0
        and cache_creation_1h_tokens <= 0
    ):
        return 0.0

    cache_creation_1h_tokens = max(0, min(cache_creation_1h_tokens, cache_creation_tokens))
    cache_creation_5m_tokens = cache_creation_tokens - cache_creation_1h_tokens
    pricing = None
    if apply_long_context:
        pricing = _long_context_prices(
            model,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
    pricing = pricing or get_pricing(model)
    if pricing is None:
        return None
    p_in, p_out, p_cr, p_cw = pricing
    cost = (
        input_tokens * p_in
        + output_tokens * p_out
        + cache_read_tokens * p_cr
        + cache_creation_5m_tokens * p_cw
        + cache_creation_1h_tokens * (p_in * 2.0)
    ) / 1_000_000
    return cost


def estimate_session_cost(session) -> float | None:
    """Estimate cost for a LiveSession object.

    LiveSession counters are session aggregates, so request-size rates cannot
    be inferred here.
    """
    return estimate_cost(
        model=session.model,
        input_tokens=session.input_tokens,
        output_tokens=session.output_tokens,
        cache_read_tokens=session.cache_read_tokens,
        cache_creation_tokens=session.cache_creation_tokens,
        apply_long_context=False,
    )
