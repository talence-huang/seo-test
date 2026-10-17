-- 跨 episode 的記憶（獨立於 intel.db，方便 Day 1 / Day N 快照切換與重現）

CREATE TABLE IF NOT EXISTS lessons (                 -- M1 經驗庫
    id               TEXT PRIMARY KEY,
    signature        TEXT NOT NULL UNIQUE,           -- 相同經驗的去重鍵
    source           TEXT NOT NULL CHECK (source IN ('success', 'failure', 'human_correction')),
    scope_json       TEXT NOT NULL,
    trigger          TEXT NOT NULL,
    lesson           TEXT NOT NULL,
    biases_json      TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'retired')),
    support          INTEGER NOT NULL DEFAULT 1,     -- 支持此經驗的不同 episode 數
    applied          INTEGER NOT NULL DEFAULT 0,     -- 被套用的 episode 數
    applied_success  INTEGER NOT NULL DEFAULT 0,     -- 套用後成功的 episode 數
    conflicts        INTEGER NOT NULL DEFAULT 0,     -- 與證據衝突的次數
    provenance_json  TEXT NOT NULL,
    retired_reason   TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS template_stats (          -- M2 URL 價值（Beta 分布）
    scope       TEXT NOT NULL,                       -- site / path / feat
    key         TEXT NOT NULL,
    alpha       REAL NOT NULL CHECK (alpha >= 0),
    beta        REAL NOT NULL CHECK (beta >= 0),
    episodes    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS reward_log (              -- 每個 episode 給 M2 的獎勵（人工糾偏時可精確撤銷）
    episode_id  TEXT NOT NULL,
    scope       TEXT NOT NULL,
    key         TEXT NOT NULL,
    template    TEXT NOT NULL,                            -- 獎勵來自哪個範本（同一個特徵鍵可能涵蓋多個範本）
    hits        REAL NOT NULL,
    misses      REAL NOT NULL,
    PRIMARY KEY (episode_id, scope, key, template)
);

CREATE TABLE IF NOT EXISTS episode_outcomes (
    episode_id      TEXT PRIMARY KEY,
    scenario        TEXT,
    goal_type       TEXT,
    success         INTEGER NOT NULL,
    end_reason      TEXT,
    fetches         INTEGER NOT NULL,
    api_calls       INTEGER NOT NULL,
    llm_usd         REAL NOT NULL,
    lessons_json    TEXT NOT NULL,                   -- 本 episode 套用的經驗
    corrected       INTEGER NOT NULL DEFAULT 0,      -- 是否被人工糾偏為錯誤
    ts              TEXT NOT NULL,
    notes_json      TEXT NOT NULL DEFAULT '[]'       -- 未成功的原因（研究無結論時由程式說明）
);

CREATE TABLE IF NOT EXISTS lesson_events (           -- 經驗的稽核軌跡（append-only）
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id   TEXT NOT NULL,
    episode_id  TEXT,
    event       TEXT NOT NULL,                       -- created / supported / activated / applied / outcome / conflict / retired
    detail      TEXT,
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections (             -- 人工糾偏紀錄（記憶被改動的原因）
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  TEXT NOT NULL,
    claim_id    TEXT NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('correct', 'wrong', 'partial')),
    reason      TEXT NOT NULL,
    lesson_id   TEXT,
    ts          TEXT NOT NULL
);
