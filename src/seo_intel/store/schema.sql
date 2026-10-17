-- 資料模型（見 docs/02-seo-intel-agent-design.md §6）
-- 列舉值以 CHECK 約束強制，避免任何寫入路徑（包括 LLM 產生的內容）寫入非法狀態。

-- ========== episode 與步驟 ==========

CREATE TABLE IF NOT EXISTS episodes (
    id              TEXT PRIMARY KEY,
    scenario        TEXT,
    goal_json       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'waiting_approval', 'done', 'failed')),
    budget_json     TEXT NOT NULL,
    memory_snapshot TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id        TEXT NOT NULL REFERENCES episodes(id),
    seq               INTEGER NOT NULL,
    step              TEXT NOT NULL,
    decision_json     TEXT NOT NULL,
    reason            TEXT,
    budget_after_json TEXT NOT NULL,
    ts                TEXT NOT NULL,
    UNIQUE (episode_id, seq)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id        TEXT REFERENCES episodes(id),
    step_id           INTEGER REFERENCES steps(id),
    role              TEXT NOT NULL,
    model             TEXT NOT NULL,
    input_tokens      INTEGER NOT NULL,
    output_tokens     INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    usd               REAL NOT NULL,
    stop_reason       TEXT,
    served_by_fallback INTEGER NOT NULL DEFAULT 0,
    request_id        TEXT,
    latency_ms        INTEGER,
    output_json       TEXT NOT NULL,
    ts                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  TEXT NOT NULL REFERENCES episodes(id),
    step_id     INTEGER REFERENCES steps(id),
    tool        TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'error', 'denied', 'skipped')),
    latency_ms  INTEGER,
    error       TEXT,
    ts          TEXT NOT NULL
);

-- ========== 研究內容 ==========

CREATE TABLE IF NOT EXISTS observations (
    id            TEXT PRIMARY KEY,
    episode_id    TEXT NOT NULL REFERENCES episodes(id),
    tool_call_id  INTEGER REFERENCES tool_calls(id),
    kind          TEXT NOT NULL,
    data_json     TEXT NOT NULL,
    ts            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id            TEXT PRIMARY KEY,
    episode_id    TEXT NOT NULL REFERENCES episodes(id),
    category      TEXT NOT NULL,
    statement     TEXT NOT NULL,
    prediction    TEXT NOT NULL,
    falsification TEXT NOT NULL,
    prior         REAL NOT NULL CHECK (prior > 0 AND prior < 1),
    posterior     REAL NOT NULL CHECK (posterior > 0 AND posterior < 1),
    status        TEXT NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed', 'testing', 'supported', 'refuted', 'inconclusive', 'rejected')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id   TEXT NOT NULL REFERENCES hypotheses(id),
    observation_id  TEXT NOT NULL REFERENCES observations(id),
    direction       TEXT NOT NULL CHECK (direction IN ('for', 'against')),
    strength        TEXT NOT NULL CHECK (strength IN ('weak', 'moderate', 'strong')),
    note            TEXT,
    ts              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id                TEXT PRIMARY KEY,
    episode_id        TEXT NOT NULL REFERENCES episodes(id),
    claim_json        TEXT NOT NULL,
    confidence        REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_ids_json TEXT NOT NULL,
    validated         INTEGER NOT NULL DEFAULT 0
);

-- 學習用的經驗與範本統計存在獨立的 memory.db（見 src/seo_intel/memory/schema.sql）

-- ========== 自治邊界與回饋 ==========

CREATE TABLE IF NOT EXISTS approvals (
    id            TEXT PRIMARY KEY,
    episode_id    TEXT NOT NULL REFERENCES episodes(id),
    action_json   TEXT NOT NULL,
    reason        TEXT NOT NULL,
    est_cost_json TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'used')),
    note          TEXT,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  TEXT NOT NULL REFERENCES episodes(id),
    claim_id    TEXT NOT NULL REFERENCES claims(id),
    verdict     TEXT NOT NULL CHECK (verdict IN ('correct', 'wrong', 'partial')),
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_steps_episode ON steps(episode_id, seq);
CREATE INDEX IF NOT EXISTS idx_llm_calls_episode ON llm_calls(episode_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_episode ON tool_calls(episode_id);
CREATE INDEX IF NOT EXISTS idx_observations_episode ON observations(episode_id);
