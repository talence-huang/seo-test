import pytest
from pydantic import BaseModel

from seo_intel.config import Settings, load_settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session")
def models(settings):
    return settings.models


class Verdict(BaseModel):
    """測試用的小型結構化輸出。"""

    label: str
    score: float


class TestClock:
    """測試用時鐘：sleep 只撥快時間，不真的等待（抓取間隔與重試退避在測試中瞬間完成）。"""

    __test__ = False

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)
