"""真實 API 測試：會產生少量費用，預設不執行。以 `pytest -m live` 明確執行。"""

import pytest

from seo_intel.llm import AnthropicLLM
from tests.conftest import Verdict

pytestmark = pytest.mark.live


def test_extractor_round_trip(models):
    result = AnthropicLLM(models).call(
        "extractor",
        system="You label sentiment. Answer only in the requested JSON format.",
        user="Label this review and give a score from 0 to 1: 'The guide was clear and helpful.'",
        schema=Verdict,
    )
    assert result.parsed.label
    assert 0 <= result.parsed.score <= 1
    assert result.meta.usd > 0
    assert result.meta.usage.output_tokens > 0
