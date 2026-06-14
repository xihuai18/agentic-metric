"""Provider usage normalization helpers.

Collectors should first normalize raw provider payloads into non-overlapping
token buckets:

- input_tokens: non-cached input only
- output_tokens: generated output
- cache_read_tokens: cached input reads
- cache_write_5m_tokens: 5-minute prompt-cache writes
- cache_write_1h_tokens: 1-hour prompt-cache writes

The database keeps ``cache_creation_tokens`` as the backward-compatible total
cache-write field and stores ``cache_creation_1h_tokens`` as the 1-hour split.
Billing derives 5-minute cache writes as ``total - 1h`` and never adds the
split fields on top of the total.

Provider APIs do not all expose the same raw semantics, so this module is the
single place that converts raw usage payloads into those buckets.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pricing import estimate_cost


@dataclass(frozen=True)
class TokenUsage:
    """Normalized, mutually-exclusive token buckets for one request/event."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def cache_creation_tokens(self) -> int:
        """Total prompt-cache write tokens."""
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def cache_creation_1h_tokens(self) -> int:
        """Backward-compatible name for 1-hour prompt-cache writes."""
        return self.cache_write_1h_tokens

    def as_bucket_tuple(self) -> tuple[int, int, int, int]:
        return (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_creation_tokens,
        )


def estimate_token_usage_cost(
    model: str,
    usage: TokenUsage,
    *,
    apply_long_context: bool = True,
) -> float | None:
    """Estimate cost for one normalized event."""
    return estimate_cost(
        model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        cache_creation_1h_tokens=usage.cache_write_1h_tokens,
        apply_long_context=apply_long_context,
    )


def normalize_anthropic_usage(usage: object) -> TokenUsage | None:
    """Normalize Anthropic/Claude usage fields.

    Anthropic already reports non-cached input, cache reads, and cache writes
    as separate fields.
    """
    if not isinstance(usage, dict):
        return None
    if not any(
        key in usage
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    ):
        return None
    cache_creation = usage.get("cache_creation", {})
    cache_creation_1h_tokens = 0
    if isinstance(cache_creation, dict):
        cache_creation_1h_tokens = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
    cache_creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    cache_write_1h_tokens = min(cache_creation_1h_tokens, cache_creation_tokens)
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_5m_tokens=cache_creation_tokens - cache_write_1h_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
    )


def normalize_openai_usage(usage: object) -> TokenUsage | None:
    """Normalize OpenAI/Codex-compatible usage fields.

    OpenAI reports ``input_tokens`` as total input, with
    ``cached_input_tokens`` as a subset. Some compatible gateways instead
    report ``input_tokens`` as non-cached input and add cached tokens
    separately. We detect the latter using ``total_tokens`` when available,
    and also guard the impossible subset shape ``cached > input``.
    """
    if not isinstance(usage, dict):
        return None
    if not any(k in usage for k in ("input_tokens", "output_tokens", "cached_input_tokens")):
        return None

    raw_input = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    return TokenUsage(
        input_tokens=openai_non_cached_input(
            usage,
            raw_input=raw_input,
            cached_input=cached,
            output_tokens=output,
        ),
        output_tokens=output,
        cache_read_tokens=cached,
        cache_write_5m_tokens=cache_create,
    )


def openai_non_cached_input(
    usage: dict,
    *,
    raw_input: int,
    cached_input: int,
    output_tokens: int,
) -> int:
    """Return non-cached input tokens for OpenAI-compatible usage."""
    if openai_input_tokens_are_separate(
        usage,
        raw_input=raw_input,
        cached_input=cached_input,
        output_tokens=output_tokens,
    ):
        return raw_input
    return max(raw_input - cached_input, 0)


def openai_input_tokens_are_separate(
    usage: dict,
    *,
    raw_input: int,
    cached_input: int,
    output_tokens: int,
) -> bool:
    """True when raw input already excludes cached input."""
    if cached_input <= 0:
        return False
    if raw_input < cached_input:
        return True
    total = usage.get("total_tokens")
    if total is None:
        return False
    try:
        total_tokens = int(total)
    except (TypeError, ValueError):
        return False
    return total_tokens == raw_input + cached_input + output_tokens
