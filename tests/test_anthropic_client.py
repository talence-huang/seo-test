"""以假的 SDK 物件測試 AnthropicLLM：請求組裝、成本計量、stop_reason 與錯誤分類。不連網。"""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from seo_intel.llm import (
    AnthropicLLM,
    LLMFatalError,
    LLMRefusalError,
    LLMSchemaError,
    LLMTransientError,
    LLMTruncatedError,
)
from seo_intel.llm.anthropic_client import FALLBACK_BETA
from tests.conftest import Verdict

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _response(*, parsed=None, stop_reason="end_turn", model="claude-opus-5-5", iterations=None, stop_details=None):
    usage = SimpleNamespace(
        input_tokens=1_000, output_tokens=500, cache_read_input_tokens=6_000,
        cache_creation_input_tokens=0, iterations=iterations,
    )
    return SimpleNamespace(
        parsed_output=parsed, stop_reason=stop_reason, model=model, usage=usage,
        stop_details=stop_details, _request_id="req_test",
    )


class FakeSDK:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _llm(models, outcome):
    sdk = FakeSDK(outcome)
    return AnthropicLLM(models, client=sdk), sdk


def test_opus_request_has_fallback_cache_and_effort(models):
    llm, sdk = _llm(models, _response(parsed=Verdict(label="a", score=1)))
    llm.call("planner", system="stable system", user="volatile", schema=Verdict)
    req = sdk.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["betas"] == [FALLBACK_BETA] and req["fallbacks"] == "default"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["messages"] == [{"role": "user", "content": "volatile"}]
    assert req["output_config"] == {"effort": "medium"}
    assert req["output_format"] is Verdict


def test_haiku_request_has_no_fallback(models):
    llm, sdk = _llm(models, _response(parsed=Verdict(label="a", score=1), model="claude-haiku-5-5"))
    llm.call("extractor", system="s", user="u", schema=Verdict)
    req = sdk.requests[0]
    assert "betas" not in req and "fallbacks" not in req
    assert req["output_config"] == {"effort": "low"}


def test_success_meta_and_cost(models):
    llm, _ = _llm(models, _response(parsed=Verdict(label="ok", score=0.9)))
    result = llm.call("planner", system="s", user="u", schema=Verdict)
    assert result.parsed.label == "ok"
    assert result.meta.usage.cache_read_tokens == 6_000
    assert result.meta.usd == pytest.approx(0.0152)
    assert result.meta.request_id == "req_test"
    assert result.meta.served_by_fallback is False


def test_fallback_detected_and_priced_by_served_model(models):
    iterations = [SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")]
    llm, _ = _llm(models, _response(parsed=Verdict(label="ok", score=1), model="claude-opus-4-8", iterations=iterations))
    result = llm.call("planner", system="s", user="u", schema=Verdict)
    assert result.meta.served_by_fallback is True
    assert result.meta.model == "claude-opus-4-8"
    assert result.meta.usd == pytest.approx(0.0152)  # 未列價的模型退回以請求模型計價


def test_refusal_raises_with_category_and_cost(models):
    llm, _ = _llm(models, _response(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")))
    with pytest.raises(LLMRefusalError) as exc:
        llm.call("planner", system="s", user="u", schema=Verdict)
    assert exc.value.category == "cyber"
    assert exc.value.meta.usd > 0


def test_truncation_and_missing_output(models):
    llm, _ = _llm(models, _response(stop_reason="max_tokens"))
    with pytest.raises(LLMTruncatedError):
        llm.call("planner", system="s", user="u", schema=Verdict)
    llm, _ = _llm(models, _response(parsed=None))
    with pytest.raises(LLMSchemaError):
        llm.call("planner", system="s", user="u", schema=Verdict)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=REQUEST), body=None), LLMTransientError),
        (anthropic.InternalServerError("boom", response=httpx2.Response(503, request=REQUEST), body=None), LLMTransientError),
        (anthropic.BadRequestError("bad", response=httpx2.Response(400, request=REQUEST), body=None), LLMFatalError),
        (anthropic.AuthenticationError("key", response=httpx2.Response(401, request=REQUEST), body=None), LLMFatalError),
        (anthropic.APIConnectionError(request=REQUEST), LLMTransientError),
        (anthropic.APITimeoutError(request=REQUEST), LLMTransientError),
    ],
)
def test_sdk_errors_are_classified(models, error, expected):
    llm, _ = _llm(models, error)
    with pytest.raises(expected) as exc:
        llm.call("planner", system="s", user="u", schema=Verdict)
    assert exc.value.retryable is expected.retryable


def test_missing_credentials_points_to_env_file(models):
    llm, _ = _llm(models, TypeError("Could not resolve authentication method. Expected one of api_key"))
    with pytest.raises(LLMFatalError, match=r"\.env"):
        llm.call("planner", system="s", user="u", schema=Verdict)


def test_unrelated_type_error_is_not_swallowed(models):
    llm, _ = _llm(models, TypeError("unexpected keyword argument"))
    with pytest.raises(TypeError):
        llm.call("planner", system="s", user="u", schema=Verdict)


def test_unknown_role_is_fatal(models):
    llm, sdk = _llm(models, _response())
    with pytest.raises(LLMFatalError):
        llm.call("nobody", system="s", user="u", schema=Verdict)
    assert sdk.requests == []
