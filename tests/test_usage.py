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
