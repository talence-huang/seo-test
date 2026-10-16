import pytest

from seo_intel.llm import FakeLLM, LLMFatalError, LLMSchemaError, LLMTransientError
from tests.conftest import Verdict


def test_scripted_list_in_order(models):
    llm = FakeLLM(models, {"planner": [{"label": "a", "score": 0.1}, Verdict(label="b", score=0.2)]})
    first = llm.call("planner", system="sys", user="u1", schema=Verdict)
    second = llm.call("planner", system="sys", user="u2", schema=Verdict)
    assert (first.parsed.label, second.parsed.label) == ("a", "b")
    assert first.meta.model == "claude-opus-5-5"
    assert first.meta.usd > 0
    assert [c["user"] for c in llm.calls] == ["u1", "u2"]


def test_callable_responder_sees_prompt(models):
    llm = FakeLLM(models, {"extractor": lambda system, user, schema: {"label": user.upper(), "score": 1}})
    result = llm.call("extractor", system="s", user="page", schema=Verdict)
    assert result.parsed.label == "PAGE"
    assert result.meta.model == "claude-haiku-5-5"


def test_scripted_exception_is_raised(models):
    llm = FakeLLM(models, {"planner": [LLMTransientError("429")]})
    with pytest.raises(LLMTransientError):
        llm.call("planner", system="s", user="u", schema=Verdict)


def test_schema_violation_carries_cost(models):
    llm = FakeLLM(models, {"planner": [{"label": "a"}]})
    with pytest.raises(LLMSchemaError) as exc:
        llm.call("planner", system="s", user="u", schema=Verdict)
    assert exc.value.meta is not None and exc.value.meta.usd > 0


def test_unknown_role_and_exhausted_script(models):
    llm = FakeLLM(models, {"planner": []})
    with pytest.raises(LLMFatalError, match="未定義"):
        llm.call("nobody", system="s", user="u", schema=Verdict)
    with pytest.raises(LLMFatalError, match="用完"):
        llm.call("planner", system="s", user="u", schema=Verdict)
