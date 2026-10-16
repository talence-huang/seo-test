import pytest

from seo_intel.llm import CassetteMissError, FakeLLM, RecordingLLM, ReplayLLM
from tests.conftest import Verdict


def test_record_then_replay_is_identical(models, tmp_path):
    path = tmp_path / "cassette.jsonl"
    fake = FakeLLM(models, {"planner": [{"label": "x", "score": 0.5}, {"label": "y", "score": 0.6}]})
    recorder = RecordingLLM(fake, path)
    live_a = recorder.call("planner", system="s", user="same", schema=Verdict)
    live_b = recorder.call("planner", system="s", user="same", schema=Verdict)

    replay = ReplayLLM(path)
    again_a = replay.call("planner", system="s", user="same", schema=Verdict)
    again_b = replay.call("planner", system="s", user="same", schema=Verdict)

    assert (again_a.parsed, again_b.parsed) == (live_a.parsed, live_b.parsed)  # 相同提示依錄製順序
    assert again_a.meta == live_a.meta  # 成本與 token 也一併重現


def test_replay_miss_raises(models, tmp_path):
    path = tmp_path / "cassette.jsonl"
    RecordingLLM(FakeLLM(models, {"planner": [{"label": "x", "score": 0.5}]}), path).call(
        "planner", system="s", user="recorded", schema=Verdict
    )
    with pytest.raises(CassetteMissError):
        ReplayLLM(path).call("planner", system="s", user="changed prompt", schema=Verdict)
