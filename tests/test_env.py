import os

import pytest

from seo_intel.env import load_env_file

KEYS = ["SEO_TEST_A", "SEO_TEST_B", "SEO_TEST_C", "SEO_TEST_EMPTY", "SEO_TEST_EXISTING"]


@pytest.fixture(autouse=True)
def _clean_env():
    saved = {k: os.environ.pop(k, None) for k in KEYS}
    yield
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


def test_parses_comments_quotes_export_and_bom(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "﻿# comment\n\nSEO_TEST_A=plain\nexport SEO_TEST_B=\"quoted value\"\nSEO_TEST_C='single'\nnot a pair\n",
        encoding="utf-8",
    )
    assert load_env_file(env) == ["SEO_TEST_A", "SEO_TEST_B", "SEO_TEST_C"]
    assert os.environ["SEO_TEST_A"] == "plain"
    assert os.environ["SEO_TEST_B"] == "quoted value"
    assert os.environ["SEO_TEST_C"] == "single"


def test_empty_value_means_not_filled(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SEO_TEST_EMPTY=\n", encoding="utf-8")
    assert load_env_file(env) == []
    assert "SEO_TEST_EMPTY" not in os.environ


def test_existing_environment_wins(tmp_path):
    os.environ["SEO_TEST_EXISTING"] = "from-system"
    env = tmp_path / ".env"
    env.write_text("SEO_TEST_EXISTING=from-file\n", encoding="utf-8")
    assert load_env_file(env) == []
    assert os.environ["SEO_TEST_EXISTING"] == "from-system"


def test_missing_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == []
