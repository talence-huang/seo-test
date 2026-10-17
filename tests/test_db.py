import json
import sqlite3

import pytest

from seo_intel.store import SCHEMA_VERSION, Database
from seo_intel.store.db import utc_now

EXPECTED_TABLES = {
    "approvals", "claims", "episodes", "evidence", "feedback", "hypotheses",
    "llm_calls", "observations", "steps", "tool_calls",
}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "intel.db")
    yield d
    d.close()


def _episode(db, eid="E-1", status="running"):
    db.insert("episodes", {
        "id": eid, "scenario": "s1", "goal_json": {"q": "why"}, "status": status,
        "budget_json": {"max_fetches": 300}, "started_at": utc_now(),
    })


def test_schema_created(db):
    assert set(db.tables()) == EXPECTED_TABLES
    assert db.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
    assert db.query("PRAGMA foreign_keys")[0][0] == 1


def test_reopen_is_idempotent(tmp_path):
    path = tmp_path / "intel.db"
    Database(path).close()
    again = Database(path)
    assert set(again.tables()) == EXPECTED_TABLES
    again.close()


def test_v1_database_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE lessons (id TEXT); CREATE TABLE template_stats (x TEXT); PRAGMA user_version = 1;")
    conn.close()
    db = Database(path)
    assert "lessons" not in db.tables() and "template_stats" not in db.tables()
    assert db.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
    db.close()


def test_json_columns_are_canonical(db):
    db.insert("episodes", {
        "id": "E-1", "goal_json": {"b": 2, "a": 1}, "budget_json": {}, "started_at": utc_now(),
    })
    stored = db.query("SELECT goal_json FROM episodes WHERE id = 'E-1'")[0]["goal_json"]
    assert stored == json.dumps({"a": 1, "b": 2})


def test_invalid_status_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        _episode(db, status="almost_done")


def test_hypothesis_probability_bounds_enforced(db):
    _episode(db)
    row = {
        "id": "H-1", "episode_id": "E-1", "category": "content_depth", "statement": "s",
        "prediction": "p", "falsification": "f", "prior": 0.4, "posterior": 1.0,
        "created_at": utc_now(), "updated_at": utc_now(),
    }
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("hypotheses", row)


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("steps", {
            "episode_id": "E-missing", "seq": 1, "step": "DEFINE_GOAL",
            "decision_json": {}, "budget_after_json": {}, "ts": utc_now(),
        })


def test_failed_insert_rolls_back(db):
    _episode(db)
    with pytest.raises(sqlite3.IntegrityError):
        _episode(db)  # 主鍵重複
    assert len(db.query("SELECT id FROM episodes")) == 1
