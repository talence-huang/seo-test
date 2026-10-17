import pytest

from seo_intel.llm import CallMeta, CassetteMissError, LLMResult, RecordingLLM, ReplayLLM
from tests.conftest import Verdict


class _Scripted:
    """最小的 LLM 替身：依序回傳固定輸出，只用來測試錄製與重播本身。"""

    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)

    def call(self, role, *, system, user, schema):
        return LLMResult(parsed=schema.model_validate(self.outputs.pop(0)),
                         meta=CallMeta(role=role, model="stub", usd=0.01))


def test_record_then_replay_is_identical(tmp_path):
    path = tmp_path / "cassette.jsonl"
    recorder = RecordingLLM(_Scripted([{"label": "x", "score": 0.5}, {"label": "y", "score": 0.6}]), path)
    live_a = recorder.call("planner", system="s", user="same", schema=Verdict)
    live_b = recorder.call("planner", system="s", user="same", schema=Verdict)

    replay = ReplayLLM(path)
    again_a = replay.call("planner", system="s", user="same", schema=Verdict)
    again_b = replay.call("planner", system="s", user="same", schema=Verdict)

    assert (again_a.parsed, again_b.parsed) == (live_a.parsed, live_b.parsed)  # 相同提示依錄製順序
    assert again_a.meta == live_a.meta  # 成本與 token 也一併重現


def test_replay_miss_raises(tmp_path):
    path = tmp_path / "cassette.jsonl"
    RecordingLLM(_Scripted([{"label": "x", "score": 0.5}]), path).call(
        "planner", system="s", user="recorded", schema=Verdict
    )
    with pytest.raises(CassetteMissError):
        ReplayLLM(path).call("planner", system="s", user="changed prompt", schema=Verdict)
