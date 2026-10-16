"""依 token 用量與價格表計算美元成本。"""

from __future__ import annotations

from seo_intel.config import ModelPricing
from seo_intel.llm.types import TokenUsage

PER_MILLION = 1_000_000


def compute_usd(pricing: ModelPricing, usage: TokenUsage) -> float:
    in_rate, out_rate = pricing.input, pricing.output
    total_input = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    lc = pricing.long_context
    if lc is not None and total_input > lc.threshold_tokens:
        # 長上下文只調整一般輸入與輸出單價；快取單價沿用設定值（近似）
        in_rate, out_rate = lc.input, lc.output
    usd = (
        usage.input_tokens * in_rate
        + usage.output_tokens * out_rate
        + usage.cache_read_tokens * pricing.cache_read
        + usage.cache_write_tokens * pricing.cache_write
    ) / PER_MILLION
    return round(usd, 8)
