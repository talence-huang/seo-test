# 02 — 研究總覽與範疇

> 版本：v0.4（2026-10-17）——研究版，範疇收斂
> 詳細設計：[04 研究迴圈與可觀察性](04-research-loop.md)、[05 學習機制與 Evals](05-memory-and-evals.md)、[06 失效模式與自治邊界](06-reliability-and-autonomy.md)
> 實作計畫：[03](03-implementation-plan.md)；需求對照：[00](00-requirements-traceability.md)；背景研究：[01 Hermes](01-hermes-architecture.md)

---

## 1. 研究定位

本專案是**研究性質**的實作：重點在設計並實作一個完整、**過程可被觀察**的自主研究 Agent 流程，而不是打造營運系統。成果以「流程是否完整、決策是否可見、進步是否可量化」來衡量。

### 研究問題

| # | 研究問題 | 對應需求 | 如何回答 |
|---|---|---|---|
| RQ1 | 一個用程式寫成、每一步都可觀察的研究迴圈，能否在有限預算下，從數萬頁網站中找到正確結論？ | A | SimWeb 情境中的結論正確率、抓取效率、完整 trace |
| RQ2 | 經驗回流能否讓第 2 次以後的決策**可量化地**優於第 1 次？ | B | Day 1 vs Day N 評測 + 消融實驗 |
| RQ3 | 失效處置與自治邊界能否由程式保證，而不依賴 LLM 自律？ | C | 失效模式測試、邊界測試 |

---

## 2. 範疇

### 2.1 範疇內

| 項目 | 說明 |
|---|---|
| 可觀察的研究迴圈 | 8 步驟狀態機；執行時即時輸出每一步；事後可檢視完整時間軸 |
| 成本控制 | URL 範本分群、評分、分階段預算、結束條件 |
| 假設與證據 | 可證偽假設、對照組驗證、程式化信心更新、引用驗證 |
| 學習機制 | **M1 經驗庫**（成功 / 失敗 / 人工糾偏）＋ **M2 URL 價值學習** |
| 評測 | SimWeb 模擬世界、6 個情境、Day 1 vs Day N、消融 |
| 自治邊界 | `PolicyGate` 三級（自主 / 需核准 / 禁止）、暫停與續跑 |
| 失效模式 | 反爬、幻覺、工具崩潰、學到錯誤經驗、預算失控、注入、LLM API 錯誤 |
| 研究成果 | `docs/07-research-findings.md`：結果、發現、限制 |

### 2.2 範疇外（明確不做）

| 項目 | 理由 |
|---|---|
| Hermes 執行環境整合（安裝、profile、cron、gateway、技能） | 屬營運外殼，不影響研究問題；改為**借用其學習迴圈的設計模式**（見第 4 節） |
| 真實資料來源（GSC、Wayback、代理 SERP、真實網站抓取） | 需帳號、會變動、無標準答案；研究一律在可重現的 SimWeb 中進行 |
| 長期監控、排程、週報、Playbook 到期重驗、前瞻預測結算 | 需要數週真實時間，不屬本次研究問題 |
| 其他學習機制（工具可靠度獨立模組、假設先驗校準） | 工具失敗經驗併入 M1；需求只要求至少一個機制 |
| 營運防護（kill switch、每日花費上限、健康檢查、機密掃描） | 營運需求，非研究重點 |
| 對外發布、多使用者、儀表板 | — |

> **範疇守則**：任何新想法先記入 `docs/07-research-findings.md` 的「未來工作」，不直接加入實作。

---

## 3. 架構決策

| # | 決策 | 理由 |
|---|---|---|
| D6 | 研究迴圈自建（Python 狀態機） | 需求 A 要求迴圈寫成程式且不能是黑盒子；自建才能讓每一步都可觀察 |
| D7 | 借用 Hermes 學習迴圈的**設計模式**，不整合其執行環境 | 研究重點是流程與學習機制本身；Hermes 的技能 / 記憶為自由文字，無法量化效用 |
| D8 | 以 SimWeb 模擬世界作為唯一研究環境 | 可植入標準答案、干擾項與反爬行為，結果可重現 |
| D9 | 自治邊界由程式強制（`PolicyGate`） | prompt 規則可被注入或忽略 |
| D10 | 不使用 LangGraph / CrewAI | 自建狀態機 + SQLite 檢查點即可，少一層框架，trace 完全可控 |

---

## 4. Hermes 設計模式的運用

研究 Hermes（見 [01](01-hermes-architecture.md)）後，借用其學習迴圈的四個概念，並改造成可量化的版本：

| Hermes 概念 | 本研究的對應 | 改造重點 |
|---|---|---|
| Skills：從經驗自動產生程序性知識 | M1 經驗庫 | 經驗附帶**程式可直接套用的偏置**，而不只是文字 |
| Background Review：回合後自我回顧 | `REFLECT` 步驟 | 回顧結果以結構化格式產出，並引用該次 episode 的證據 |
| Curator：依使用頻率淘汰技能 | 經驗治理 | 改以「**效用**」淘汰，而非使用頻率 |
| write_approval：自我修改需審核 | 人工糾偏、自治邊界 | 人工糾偏立即生效且權重最高 |
| 凍結快照：記憶在 session 開始時載入 | episode 開始時載入記憶快照 | 方便 Day 1 / Day N 評測切換記憶版本 |

---

## 5. 架構

```
┌─────────────────────── 研究迴圈（自建 Python）────────────────────────┐
│  ResearchLoop（8 步驟狀態機）                                          │
│    ├─ PolicyGate（自治邊界）  ├─ BudgetLedger（預算）                   │
│    ├─ Frontier（URL 分群、評分）                                         │
│    ├─ 工具：robots · sitemap · fetch · extract · diff · history ·        │
│    │        rank_query · stats                                          │
│    ├─ 假設與證據引擎（log-odds 更新、引用驗證）                          │
│    └─ LLM 角色：Planner / Hypothesizer / Verifier（Opus 5.5）            │
│                 Extractor（Haiku 5.5）── 全部 structured outputs         │
├─────────────────────── 可觀察性 ──────────────────────────────────────┤
│  即時主控台輸出 · trace.jsonl · trace show · timeline.html · --step 模式 │
├─────────────────────── 學習 ─────────────────────────────────────────┤
│  M1 經驗庫 · M2 URL 價值學習 · 經驗治理                                 │
├─────────────────────── 儲存 ─────────────────────────────────────────┤
│  data/intel.db（SQLite）· runs/<episode>/ · 記憶快照                    │
└───────────────────────────────────────────────────────────────────────┘
┌─────────────────────── 研究環境與評測 ────────────────────────────────┐
│  SimWeb（模擬網站、反爬、模擬排名與歷史）· 評分器 · Day 1 vs Day N     │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 6. 資料模型（SQLite）

```
-- episode 與步驟
episodes(id, scenario, goal_json, status[running|waiting_approval|done|failed],
         budget_json, memory_snapshot, started_at, ended_at)
steps(id, episode_id, seq, step, decision_json, reason, budget_after_json, ts)
llm_calls(id, episode_id, step_id, role, model, input_tokens, output_tokens, cache_read_tokens, usd, output_json)
tool_calls(id, episode_id, step_id, tool, args_json, status, latency_ms, error)

-- 研究內容
observations(id, episode_id, tool_call_id, kind, data_json)
hypotheses(id, episode_id, category, statement, prediction, falsification, prior, posterior, status)
evidence(id, hypothesis_id, observation_id, direction, strength)
claims(id, episode_id, claim_json, confidence, evidence_ids_json, validated)

-- 學習
lessons(id, source, scope_json, trigger, lesson, biases_json, status, utility_json, provenance_json)  -- M1
template_stats(scope_key, template, alpha, beta)                                                      -- M2

-- 自治邊界
approvals(id, episode_id, action_json, reason, est_cost_json, status, expires_at)
feedback(id, episode_id, claim_id, verdict, reason, created_at)
```

---

## 7. 專案目錄結構

```
D:\code\seo\
├── README.md
├── pyproject.toml
├── config/
│   ├── models.yaml        # 模型 ID、effort、價格表
│   ├── budget.yaml        # 預算與階段比例
│   └── policy.yaml        # 自治邊界規則
├── src/seo_intel/
│   ├── core/              # loop, controller, state, steps/, frontier, budget, policy, beliefs
│   ├── llm/               # client（結構化輸出、成本計量、FakeLLM、錄製重播）, roles, schemas
│   ├── tools/             # robots, sitemap, fetch, extract, diff, history, rank_query, stats
│   ├── memory/            # lessons (M1), bandit (M2), governance
│   ├── observe/           # console（即時輸出）, trace, timeline（HTML）
│   ├── store/             # schema.sql, db.py
│   └── cli.py
├── simweb/                # 世界產生器、情境、反爬行為、模擬 API、MockTransport
├── evals/                 # scenarios/, scorer.py, runner.py, reports/
├── tests/
├── data/  runs/           # .gitignore
└── docs/
```

---

## 8. 決策紀錄

| # | 決策 | 結論 | 日期 |
|---|---|---|---|
| D1 | 執行環境 | Windows 原生 Python 3.14 | 2026-10-17 |
| D2 | 模型 | `claude-opus-5-5`（推理角色）、`claude-haiku-5-5`（萃取角色） | 2026-10-17 |
| D3 | 資料來源 | 僅 SimWeb 模擬世界（真實資料源列為範疇外） | 2026-10-17 |
| D4 | 成果輸出 | 本機檔案（trace、timeline.html、評測報告、研究成果文件） | 2026-10-17 |
| D5 | 監控目標 | 不需要（範疇外） | 2026-10-17 |
| D6–D10 | 架構 | 見第 3 節 | 2026-10-17 |
