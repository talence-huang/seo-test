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
