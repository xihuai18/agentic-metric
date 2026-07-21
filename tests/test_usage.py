from unittest.mock import patch

from agentic_metric.usage import normalize_anthropic_usage, normalize_openai_usage


def test_anthropic_usage_clamps_dirty_negative_token_fields():
    usage = normalize_anthropic_usage({
        "input_tokens": -10,
        "output_tokens": -20,
        "cache_read_input_tokens": -30,
        "cache_creation_input_tokens": -40,
        "cache_creation": {"ephemeral_1h_input_tokens": 25},
    })

    assert usage is not None
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_5m_tokens == 0
    assert usage.cache_write_1h_tokens == 0


def test_openai_usage_clamps_dirty_negative_token_fields():
    usage = normalize_openai_usage({
        "input_tokens": -10,
        "output_tokens": -20,
        "cached_input_tokens": -30,
        "cache_creation_input_tokens": -40,
    })

    assert usage is not None
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_5m_tokens == 0
    assert usage.cache_write_1h_tokens == 0


def test_openai_usage_moves_codex_cache_write_subset_out_of_input():
    """Codex GPT-5.6+ cache_write_input_tokens is a subset of input_tokens."""
    usage = normalize_openai_usage({
        "input_tokens": 1_000,
        "cached_input_tokens": 200,
        "output_tokens": 50,
        "cache_write_input_tokens": 300,
        "total_tokens": 1_050,
    })

    assert usage is not None
    assert usage.input_tokens == 500  # 1000 - 200 cached - 300 written
    assert usage.cache_read_tokens == 200
    assert usage.cache_write_5m_tokens == 300
    assert usage.output_tokens == 50


def test_openai_usage_keeps_unbilled_cache_writes_in_input(tmp_path):
    """Pre-GPT-5.6 models have no cache-write fee; writes stay as input."""
    import agentic_metric.pricing as pricing

    pricing._user_cache = None
    pricing._user_cache_mtime = -1.0
    raw = {
        "input_tokens": 1_000,
        "cached_input_tokens": 200,
        "output_tokens": 50,
        "cache_write_input_tokens": 300,
        "total_tokens": 1_050,
    }
    with patch("agentic_metric.pricing.PRICING_FILE", tmp_path / "pricing.json"):
        usage = normalize_openai_usage(dict(raw), model="gpt-5.5")
        billed = normalize_openai_usage(dict(raw), model="gpt-5.6-sol")
    pricing._user_cache = None
    pricing._user_cache_mtime = -1.0

    assert usage is not None
    assert usage.input_tokens == 800  # writes not moved out
    assert usage.cache_read_tokens == 200
    assert usage.cache_write_5m_tokens == 0

    assert billed is not None
    assert billed.input_tokens == 500
    assert billed.cache_write_5m_tokens == 300


def test_openai_usage_prefers_subset_cache_write_key_over_separate_shape():
    usage = normalize_openai_usage({
        "input_tokens": 1_000,
        "cached_input_tokens": 0,
        "output_tokens": 10,
        "cache_write_input_tokens": 400,
        "cache_creation_input_tokens": 400,
    })

    assert usage is not None
    assert usage.input_tokens == 600
    assert usage.cache_write_5m_tokens == 400


def test_openai_usage_dual_key_unbilled_model_counts_writes_once(tmp_path):
    """Dual-key payload on an unbilled model must not double count writes."""
    import agentic_metric.pricing as pricing

    pricing._user_cache = None
    pricing._user_cache_mtime = -1.0
    with patch("agentic_metric.pricing.PRICING_FILE", tmp_path / "pricing.json"):
        usage = normalize_openai_usage({
            "input_tokens": 1_000,
            "cached_input_tokens": 0,
            "output_tokens": 10,
            "cache_write_input_tokens": 400,
            "cache_creation_input_tokens": 400,
        }, model="gpt-5.5")
    pricing._user_cache = None
    pricing._user_cache_mtime = -1.0

    assert usage is not None
    assert usage.input_tokens == 1_000  # subset stays in input
    assert usage.cache_write_5m_tokens == 0  # separate copy not added on top


def test_openai_usage_ignores_anthropic_cache_creation_1h_shape():
    usage = normalize_openai_usage({
        "input_tokens": 100,
        "output_tokens": 10,
        "cached_input_tokens": 20,
        "cache_creation_input_tokens": 40,
        "cache_creation_1h_input_tokens": 15,
        "cache_creation": {"ephemeral_1h_input_tokens": 30},
    })

    assert usage is not None
    assert usage.input_tokens == 80
    assert usage.output_tokens == 10
    assert usage.cache_read_tokens == 20
    assert usage.cache_write_5m_tokens == 40
    assert usage.cache_write_1h_tokens == 0
