import shutil

import pytest
import yaml
from pydantic import ValidationError

from seo_intel.config import PROJECT_ROOT, load_settings


def test_project_config_loads(settings):
    assert {"planner", "hypothesizer", "verifier", "extractor"} <= settings.models.roles.keys()
    assert settings.models.roles["extractor"].fallback is False
    assert settings.models.roles["planner"].fallback is True
    assert "ignore_robots" in settings.policy.forbidden
    assert "exceed_budget" in settings.policy.require_approval


@pytest.fixture
def config_copy(tmp_path):
    shutil.copytree(PROJECT_ROOT / "config", tmp_path, dirs_exist_ok=True)
    return tmp_path


def _edit(path, mutate):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_phase_shares_must_sum_to_one(config_copy):
    _edit(config_copy / "budget.yaml", lambda d: d["phases"].update(verification=0.5))
    with pytest.raises(ValidationError, match="總和必須為 1"):
        load_settings(config_copy)


def test_every_role_model_must_be_priced(config_copy):
    _edit(config_copy / "models.yaml", lambda d: d["roles"]["planner"].update(model="claude-unknown"))
    with pytest.raises(ValidationError, match="缺少價格設定"):
        load_settings(config_copy)


def test_action_cannot_be_both_approval_and_forbidden(config_copy):
    _edit(config_copy / "policy.yaml", lambda d: d["forbidden"].append("bulk_crawl"))
    with pytest.raises(ValidationError, match="不能同時"):
        load_settings(config_copy)


def test_unknown_keys_are_rejected(config_copy):
    _edit(config_copy / "budget.yaml", lambda d: d.update(max_fetchs=10))
    with pytest.raises(ValidationError):
        load_settings(config_copy)
