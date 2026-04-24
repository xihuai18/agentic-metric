"""Tests for pricing module."""

import json
from unittest.mock import patch

from agentic_metric.pricing import (
    PRICING,
    _BUILTIN_PRICING,
    _load_user_pricing,
    estimate_cost,
    estimate_session_cost,
    get_all_pricing,
    get_pricing,
    get_pricing_fingerprint,
    remove_user_pricing,
    reset_all_user_pricing,
    set_user_pricing,
)
from agentic_metric.models import LiveSession


def _reset_cache():
    """Reset the user-pricing memo cache between tests."""
    import agentic_metric.pricing as p
    p._user_cache = None
    p._user_cache_mtime = -1.0


def _patch_empty_user_pricing(tmp_path):
    """Isolate builtin pricing tests from the real local pricing.json."""
    _reset_cache()
    return patch("agentic_metric.pricing.PRICING_FILE", tmp_path / "pricing.json")


def test_known_model_pricing():
    p = get_pricing("claude-sonnet-4-6-20250101")
    assert p == (3.0, 15.0, 0.30, 3.75)


def test_unknown_model_fallback():
    p = get_pricing("unknown-model-xyz")
    assert p is None


def test_unknown_model_with_positive_usage_has_unknown_cost(caplog):
    import agentic_metric.pricing as p

    p._warned_models.clear()
    caplog.set_level("WARNING", logger="agentic_metric.pricing")

    assert estimate_cost("unknown-model-xyz", input_tokens=1) is None
    assert "Unknown model 'unknown-model-xyz'" in caplog.text


def test_no_family_fallback_claude():
    """Unknown Claude variants should stay unknown instead of inheriting a family price."""
    p = get_pricing("claude-sonnet-99")
    assert p is None


def test_no_family_fallback_gpt5():
    """gpt-5.x not explicitly listed should stay unknown."""
    p = get_pricing("gpt-5.9-preview")
    assert p is None

    p = get_pricing("gpt-5.2-preview")
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


def test_openai_cached_input_is_not_charged_twice(tmp_path):
    with _patch_empty_user_pricing(tmp_path):
        cost = estimate_cost(
            "gpt-5.1-codex",
            input_tokens=900_000,
            cache_read_tokens=100_000,
        )
        assert abs(cost - 1.1375) < 0.001
    _reset_cache()


def test_openai_long_context_is_model_specific(tmp_path):
    with _patch_empty_user_pricing(tmp_path):
        cost = estimate_cost(
            "gpt-5.4",
            input_tokens=272_001,
            output_tokens=1_000,
        )
        expected = (272_001 * 5.0 + 1_000 * 22.5) / 1_000_000
        assert abs(cost - expected) < 0.001

        mini_cost = estimate_cost(
            "gpt-5.4-mini",
            input_tokens=500_000,
            output_tokens=1_000,
        )
        expected_mini = (500_000 * 0.75 + 1_000 * 4.5) / 1_000_000
        assert abs(mini_cost - expected_mini) < 0.001

        pro_cost = estimate_cost(
            "gpt-5.4-pro",
            input_tokens=272_001,
            output_tokens=1_000,
        )
        assert pro_cost is None
    _reset_cache()


def test_openai_codex_fast_mode_is_model_specific(tmp_path):
    with _patch_empty_user_pricing(tmp_path):
        standard_55 = estimate_cost("gpt-5.5", input_tokens=1_000_000, output_tokens=1_000_000)
        fast_55 = estimate_cost(
            "gpt-5.5",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            service_tier="fast",
        )
        assert abs(standard_55 - 35.0) < 0.001
        assert abs(fast_55 - 87.5) < 0.001

        standard_54 = estimate_cost("gpt-5.4", input_tokens=100_000, output_tokens=100_000)
        fast_54 = estimate_cost(
            "gpt-5.4",
            input_tokens=100_000,
            output_tokens=100_000,
            service_tier="fast",
        )
        assert abs(standard_54 - 1.75) < 0.001
        assert abs(fast_54 - 3.5) < 0.001

        mini_fast = estimate_cost(
            "gpt-5.4-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            service_tier="fast",
        )
        assert abs(mini_fast - 5.25) < 0.001

        claude_standard = estimate_cost(
            "claude-opus-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        claude_fast = estimate_cost(
            "claude-opus-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            service_tier="fast",
        )
        assert abs(claude_standard - 30.0) < 0.001
        assert abs(claude_fast - 180.0) < 0.001
    _reset_cache()


def test_gpt_55_long_context_has_no_separate_surcharge(tmp_path):
    with _patch_empty_user_pricing(tmp_path):
        cost = estimate_cost(
            "gpt-5.5",
            input_tokens=500_000,
            output_tokens=1_000,
            cache_read_tokens=10_000,
        )
        expected = (500_000 * 5.0 + 1_000 * 30.0 + 10_000 * 0.50) / 1_000_000
        assert abs(cost - expected) < 0.001
    _reset_cache()


def test_aggregate_callers_can_disable_long_context_surcharge(tmp_path):
    with _patch_empty_user_pricing(tmp_path):
        cost = estimate_cost(
            "gpt-5.4",
            input_tokens=300_000,
            output_tokens=1_000,
            apply_long_context=False,
        )
        expected = (300_000 * 2.5 + 1_000 * 15.0) / 1_000_000
        assert abs(cost - expected) < 0.001
    _reset_cache()


def test_live_session_estimate_does_not_apply_long_context_surcharge(tmp_path):
    with _patch_empty_user_pricing(tmp_path):
        session = LiveSession(
            session_id="s",
            agent_type="codex",
            project_path="/tmp/project",
            model="gpt-5.4",
            input_tokens=300_000,
            output_tokens=1_000,
        )
        cost = estimate_session_cost(session)
        expected = (300_000 * 2.5 + 1_000 * 15.0) / 1_000_000
        assert abs(cost - expected) < 0.001
    _reset_cache()


def test_gemini_pro_long_context_is_model_specific(tmp_path):
    with _patch_empty_user_pricing(tmp_path):
        cost = estimate_cost(
            "gemini-3.1-pro-preview",
            input_tokens=200_001,
            output_tokens=1_000,
            cache_read_tokens=10_000,
        )
        expected = (200_001 * 4.0 + 1_000 * 18.0 + 10_000 * 0.40) / 1_000_000
        assert abs(cost - expected) < 0.001
    _reset_cache()


def test_claude_cache_creation_1h_and_sonnet_long_context(tmp_path):
    with _patch_empty_user_pricing(tmp_path):
        cost = estimate_cost(
            "claude-sonnet-4-20250514",
            input_tokens=1,
            cache_read_tokens=200_000,
            cache_creation_tokens=100,
            cache_creation_1h_tokens=100,
        )
        expected = (1 * 6.0 + 200_000 * 0.60 + 100 * 12.0) / 1_000_000
        assert abs(cost - expected) < 0.001

        sonnet_46_cost = estimate_cost(
            "claude-sonnet-4-6",
            input_tokens=200_001,
            output_tokens=1_000,
        )
        expected_46 = (200_001 * 3.0 + 1_000 * 15.0) / 1_000_000
        assert abs(sonnet_46_cost - expected_46) < 0.001
    _reset_cache()


def test_estimate_cost_zero_usage_unknown_model_is_silent(caplog):
    import agentic_metric.pricing as p

    p._warned_models.clear()
    caplog.set_level("WARNING", logger="agentic_metric.pricing")

    assert estimate_cost("unknown-model-xyz") == 0.0
    assert "Unknown model" not in caplog.text


def test_synthetic_model_is_non_billable(caplog):
    import agentic_metric.pricing as p

    p._warned_models.clear()
    caplog.set_level("WARNING", logger="agentic_metric.pricing")

    assert estimate_cost("<synthetic>", input_tokens=1_000_000, output_tokens=1_000_000) == 0.0
    assert "Unknown model" not in caplog.text


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


def test_gpt_5_1_codex_max_alias_is_known(caplog):
    import agentic_metric.pricing as p

    p._warned_models.clear()
    caplog.set_level("WARNING", logger="agentic_metric.pricing")

    assert get_pricing("gpt-5.1-codex-max") == p._BUILTIN_PRICING["gpt-5.1-codex-max"]
    assert "Unknown model" not in caplog.text


def test_codex_auto_review_uses_gpt_5_3_codex_pricing(caplog):
    import agentic_metric.pricing as p

    p._warned_models.clear()
    caplog.set_level("WARNING", logger="agentic_metric.pricing")

    assert get_pricing("codex-auto-review") == p._BUILTIN_PRICING["gpt-5.3-codex"]
    cost = estimate_cost(
        "codex-auto-review",
        input_tokens=11_451,
        output_tokens=177,
        cache_read_tokens=24_064,
    )
    expected = (11_451 * 1.75 + 177 * 14.0 + 24_064 * 0.175) / 1_000_000
    assert abs(cost - expected) < 1e-12
    assert "Unknown model" not in caplog.text


def test_pricing_fingerprint_includes_lookup_rules(tmp_path):
    pricing_file = tmp_path / "pricing.json"

    _reset_cache()
    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        base = get_pricing_fingerprint()
        with patch("agentic_metric.pricing._MODEL_ALIASES", {"alias-model": "claude-sonnet-4-6"}):
            assert get_pricing_fingerprint() != base
        with patch("agentic_metric.pricing._LONG_CONTEXT_TIERS", []):
            assert get_pricing_fingerprint() != base
        with patch("agentic_metric.pricing._SERVICE_TIER_MULTIPLIERS", []):
            assert get_pricing_fingerprint() != base
        with patch("agentic_metric.pricing._UNKNOWN_MODEL_PREFIXES", ("gpt-5-pro", "custom-pro")):
            assert get_pricing_fingerprint() != base
        with patch("agentic_metric.pricing._NON_BILLABLE_MODELS", {"<synthetic>", "<internal>"}):
            assert get_pricing_fingerprint() != base
    _reset_cache()


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


def test_user_pricing_override_takes_precedence_over_long_context(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(json.dumps({
        "gpt-5.4": [1.0, 2.0, 0.1, 0.0],
    }))

    _reset_cache()
    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        cost = estimate_cost(
            "gpt-5.4",
            input_tokens=300_000,
            output_tokens=1_000,
            cache_read_tokens=10_000,
        )
        expected = (300_000 * 1.0 + 1_000 * 2.0 + 10_000 * 0.1) / 1_000_000
        assert abs(cost - expected) < 0.001
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


def test_user_custom_model_can_price_unknown_prefix(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(json.dumps({
        "gpt-5.4-pro": [60.0, 270.0, 6.0, 0.0],
    }))

    _reset_cache()
    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        p = get_pricing("gpt-5.4-pro")
        assert p == (60.0, 270.0, 6.0, 0.0)
        cost = estimate_cost("gpt-5.4-pro", input_tokens=1_000, output_tokens=1_000)
        assert abs(cost - 0.33) < 0.001
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
