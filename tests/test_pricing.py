"""Tests for pricing module."""

import json
from unittest.mock import patch

from agentic_metric.pricing import (
    PRICING,
    _BUILTIN_PRICING,
    _DEFAULT_PRICING,
    _load_user_pricing,
    estimate_cost,
    get_all_pricing,
    get_pricing,
    remove_user_pricing,
    reset_all_user_pricing,
    set_user_pricing,
)


def _reset_cache():
    """Reset the user-pricing memo cache between tests."""
    import agentic_metric.pricing as p
    p._user_cache = None
    p._user_cache_mtime = -1.0


def test_known_model_pricing():
    p = get_pricing("claude-sonnet-4-6-20250101")
    assert p == (3.0, 15.0, 0.30, 3.75)


def test_unknown_model_fallback():
    p = get_pricing("unknown-model-xyz")
    assert p == _DEFAULT_PRICING


def test_family_fallback_claude():
    """Unknown claude-sonnet variant should fall back to sonnet family."""
    p = get_pricing("claude-sonnet-99")
    assert p == (3.0, 15.0, 0.30, 3.75)


def test_family_fallback_gpt5():
    """gpt-5.x not explicitly listed should fall back to gpt-5 family."""
    # gpt-5.9-preview won't match gpt-5.4 family, falls back to gpt-5 family
    p = get_pricing("gpt-5.9-preview")
    assert p == (1.75, 14.0, 0.175, 0.0)

    # gpt-5.4-foo should match gpt-5.4 family
    p = get_pricing("gpt-5.4-latest")
    assert p == (2.5, 15.0, 0.25, 0.0)


def test_longest_prefix_match():
    """gpt-5.4-mini must match its own entry, not gpt-5.4."""
    p = get_pricing("gpt-5.4-mini")
    assert p == (0.75, 4.5, 0.075, 0.0)
    p = get_pricing("gpt-5.4-mini-preview")  # prefix match on gpt-5.4-mini
    assert p == (0.75, 4.5, 0.075, 0.0)


def test_estimate_cost_zero():
    cost = estimate_cost("claude-sonnet-4-6")
    assert cost == 0.0


def test_estimate_cost_basic():
    cost = estimate_cost(
        "claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # 1M * 3.0/1M + 1M * 15.0/1M = 3.0 + 15.0 = 18.0
    assert abs(cost - 18.0) < 0.001


def test_estimate_cost_with_cache():
    cost = estimate_cost(
        "claude-opus-4-6",
        input_tokens=500_000,
        output_tokens=100_000,
        cache_read_tokens=2_000_000,
        cache_creation_tokens=200_000,
    )
    # 0.5M * 5.0 + 0.1M * 25.0 + 2M * 0.5 + 0.2M * 6.25
    # = 2.5 + 2.5 + 1.0 + 1.25 = 7.25
    assert abs(cost - 7.25) < 0.001


def test_all_models_have_four_values():
    for model, prices in PRICING.items():
        assert len(prices) == 4, f"{model} has {len(prices)} values"
        assert all(isinstance(p, (int, float)) for p in prices)


def test_user_pricing_override(tmp_path):
    """User pricing should take precedence over builtin."""
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(json.dumps({
        "claude-opus-4-6": [99.0, 99.0, 99.0, 99.0],
    }))

    _reset_cache()
    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        p = get_pricing("claude-opus-4-6")
        assert p == (99.0, 99.0, 99.0, 99.0)
    _reset_cache()


def test_alias_normalization_uses_canonical_pricing():
    p = get_pricing("claude-4.5-sonnet-thinking")
    assert p == (3.0, 15.0, 0.30, 3.75)


def test_user_pricing_override_is_exact_match(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(json.dumps({
        "gpt-5": [99.0, 99.0, 99.0, 99.0],
    }))

    _reset_cache()
    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        assert get_pricing("gpt-5") == (99.0, 99.0, 99.0, 99.0)
        assert get_pricing("gpt-5.4") == (2.5, 15.0, 0.25, 0.0)
    _reset_cache()


def test_user_custom_model(tmp_path):
    """User can add entirely new models."""
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(json.dumps({
        "my-custom-model": [1.0, 2.0, 0.1, 0.2],
    }))

    _reset_cache()
    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        p = get_pricing("my-custom-model")
        assert p == (1.0, 2.0, 0.1, 0.2)
    _reset_cache()


def test_set_and_remove_user_pricing(tmp_path):
    pricing_file = tmp_path / "pricing.json"

    _reset_cache()
    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        set_user_pricing("test-model", 1.0, 2.0, 0.3, 0.4)
        assert pricing_file.exists()

        user = _load_user_pricing()
        assert user["test-model"] == (1.0, 2.0, 0.3, 0.4)

        removed = remove_user_pricing("test-model")
        assert removed is True

        removed = remove_user_pricing("test-model")
        assert removed is False
    _reset_cache()


def test_reset_all_user_pricing(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(json.dumps({"m": [1, 2, 3, 4]}))

    _reset_cache()
    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        reset_all_user_pricing()
        assert not pricing_file.exists()
    _reset_cache()


def test_get_all_pricing_merges(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(json.dumps({
        "new-model": [1.0, 2.0, 0.1, 0.2],
    }))

    _reset_cache()
    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        merged = get_all_pricing()
        assert "claude-opus-4-7" in merged
        assert "new-model" in merged
        assert merged["new-model"] == (1.0, 2.0, 0.1, 0.2)
    _reset_cache()
