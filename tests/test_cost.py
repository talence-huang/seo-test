import pytest

from seo_intel.llm import TokenUsage, compute_usd


def test_opus_one_million_input(models):
    usd = compute_usd(models.pricing["claude-opus-5-5"], TokenUsage(input_tokens=1_000_000))
    assert usd == pytest.approx(4.00)


def test_mixed_usage_with_cache_read(models):
    usage = TokenUsage(input_tokens=1_000, output_tokens=500, cache_read_tokens=6_000)
    # (1000×4 + 500×20 + 6000×0.20) / 1e6
    assert compute_usd(models.pricing["claude-opus-5-5"], usage) == pytest.approx(0.0152)


def test_cache_write_priced(models):
    usage = TokenUsage(cache_write_tokens=10_000)
    assert compute_usd(models.pricing["claude-opus-5-5"], usage) == pytest.approx(0.05)


def test_haiku_long_context_rates(models):
    usage = TokenUsage(input_tokens=150_000, output_tokens=1_000)
    # 超過 100K：輸入 0.50、輸出 2.50
    assert compute_usd(models.pricing["claude-haiku-5-5"], usage) == pytest.approx(0.0775)


def test_haiku_short_context_rates(models):
    usage = TokenUsage(input_tokens=3_000, output_tokens=400)
    assert compute_usd(models.pricing["claude-haiku-5-5"], usage) == pytest.approx(0.0005)
