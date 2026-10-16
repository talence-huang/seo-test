# 03 — 實作計畫（研究版）

> 範疇見 [02 §2](02-seo-intel-agent-design.md)。範疇外的想法一律記入 `docs/07-research-findings.md` 的「未來工作」，不加入實作。
> 順序原則：先建研究環境（SimWeb），再建迴圈——每個功能寫完都能立刻在有標準答案的環境中觀察與驗證。

---

## 總覽

| Phase | 主題 | 對應 | 完成定義 | 預估 |
|---|---|---|---|---|
| 0 | 骨架與 LLM client | — | CLI 可執行；一次真實 LLM 呼叫成功並記錄成本 | 0.5 天 |
| 1 | SimWeb 研究環境 | B 基礎 | 6 個情境可瀏覽，含反爬、陷阱、注入頁與標準答案 | 1.5 天 |
| 2 | 研究迴圈與可觀察性 | **A** | 8 步驟跑完一個情境，四種觀察方式都可用 | 3 天 |
| 3 | 假設、證據與報告 | **A** | S1 找到正確結論，所有主張皆可追溯 | 1.5 天 |
| 4 | 學習機制 | **B** | 同情境跑第 2 次，可在 trace 中看到經驗被套用且效率提升 | 1.5 天 |
| 5 | 自治邊界與失效處置 | **C** | 06 §3 測試矩陣全數通過 | 1 天 |
| 6 | 評測與研究成果收斂 | **B** | Day 1 vs Day N 報告 + `07-research-findings.md` | 1.5 天 |

```mermaid
flowchart LR
    P0[0 骨架] --> P1[1 SimWeb] --> P2[2 迴圈與觀察] --> P3[3 假設證據] --> P4[4 學習] --> P5[5 邊界與失效] --> P6[6 評測與收斂]
```

---

## Phase 0 — 骨架與 LLM client

| # | 任務 | 產出 |
|---|---|---|
| 0.1 | `git init`、`pyproject.toml`、`.gitignore`、`README.md` | 初始 commit |
| 0.2 | 依賴：`anthropic`、`httpx`、`pydantic`、`selectolax`、`typer`、`rich`、`pytest` | 鎖定版本 |
| 0.3 | SQLite schema（[02 §6](02-seo-intel-agent-design.md)） | `store/` |
| 0.4 | LLM client：`messages.parse()` 結構化輸出、prompt caching、`usage` 成本計量、錯誤處理、refusal fallback；`FakeLLM` 與錄製 / 重播 | `llm/client.py` |
| 0.5 | 設定檔：`models.yaml`、`budget.yaml`、`policy.yaml` | `config/` |

**完成定義**：`seo-intel --help` 可執行；一次真實呼叫成功並記錄 token 與成本；`FakeLLM` 可替換。

---

## Phase 1 — SimWeb 研究環境

| # | 任務 | 產出 |
|---|---|---|
| 1.1 | 世界產生器：依種子即時產生網站（範本、內部連結、`lastmod`） | `simweb/world.py` |
| 1.2 | robots.txt、sitemap index | `simweb/site.py` |
| 1.3 | 陷阱：分面參數、無限日曆、深分頁、近似重複、JS 空殼頁 | 同上 |
| 1.4 | 反爬：429（含 `Retry-After`）、403 路徑、隨機 5xx / 逾時、格式錯誤 HTML | `simweb/behaviors.py` |
| 1.5 | Prompt injection 頁面 | 同上 |
| 1.6 | 時間軸與因果事件；模擬 `rank_query`、`history` | `simweb/timeline.py`、`apis.py` |
| 1.7 | `httpx.MockTransport` 路由 | `simweb/transport.py` |
| 1.8 | 6 個情境（4 訓練 / 2 測試）；標準答案由植入事件**自動推導**（`simweb/truth.py`），不另外手寫，避免與事件不一致 | `evals/scenarios/*.yaml` |

**完成定義**：`seo-intel simweb inspect --scenario s1` 可列出網站規模、範本分布、植入事件、標準答案與植入效應可觀察性；單元測試確認同種子結果一致、反爬行為可觸發。

**實作結果（已完成）**：主要競品站皆約 1.9–2.1 萬頁；6 個情境的植入效應皆可在排名資料中觀察到，且對照組可分辨混淆因子；SimWeb 測試 34 個（全專案 77 個）通過。

---

## Phase 2 — 研究迴圈與可觀察性（需求 A）

| # | 任務 | 產出 |
|---|---|---|
| 2.1 | `EpisodeState`、`Step`、`Controller.next_step` | `core/state.py`、`controller.py` |
| 2.2 | 工具包裝層：RobotsGuard、RateLimiter、Retry、CircuitBreaker、Cache、CostMeter | `tools/base.py` |
| 2.3 | 工具：robots、sitemap、fetch、extract、diff、history、rank_query、stats | `tools/` |
| 2.4 | Frontier：路徑正規化、範本分群、陷阱過濾、評分明細 | `core/frontier.py` |
| 2.5 | BudgetLedger：硬上限、階段預留 | `core/budget.py` |
| 2.6 | PolicyGate 基本版（robots、網域、預算） | `core/policy.py` |
| 2.7 | 步驟：DEFINE_GOAL、PLAN_INFO_NEEDS、SELECT_ACTION、EXECUTE_TOOL、PARSE_RESULT、DECIDE_NEXT；LLM 角色 prompt 與 schema | `core/steps/`、`llm/roles.py` |
| 2.8 | 結束條件：成功、預算、報酬遞減、迴圈偵測 | `core/controller.py` |
| 2.9 | **可觀察性**：即時主控台、`--step` 逐步模式、trace.jsonl、`trace show`、`timeline.html` | `observe/` |
| 2.10 | 檢查點與續跑 | `store/` |

**完成定義**：`seo-intel research --scenario s1 --step` 能逐步觀察 Agent 在 2 萬頁網站上分群抽樣、避開陷阱，在預算內結束；`timeline.html` 顯示完整決策過程。

---

## Phase 3 — 假設、證據與報告（需求 A）

| # | 任務 | 產出 |
|---|---|---|
| 3.1 | 假設模型與可證偽性檢查 | `core/beliefs.py` |
| 3.2 | FORM_HYPOTHESES、SEEK_EVIDENCE | `core/steps/` |
| 3.3 | REVISE_BELIEFS：log-odds 更新、矛盾偵測、紅隊驗證者 | `core/beliefs.py`、`llm/roles.py` |
| 3.4 | `stats`：對照組比較、事件前後比較 | `tools/stats.py` |
| 3.5 | REPORT：結構化主張、程式引用驗證、Markdown 報告 | `report/` |
| 3.6 | timeline 加入假設信心度曲線與證據連結 | `observe/` |

**完成定義**：S1 找出「競品 B /guide/ 改版造成位移」，排除干擾項，並用對照組處理混淆因子；報告中每個主張都能點回原始觀察。

---

## Phase 4 — 學習機制（需求 B）

| # | 任務 | 產出 |
|---|---|---|
| 4.1 | M2：Beta 後驗、範本原型先驗、獎勵計算 | `memory/bandit.py` |
| 4.2 | M1：經驗結構、檢索、文字注入、`biases` 套用 | `memory/lessons.py` |
| 4.3 | REFLECT：從成功 / 失敗產生候選經驗 | `core/steps/reflect.py` |
| 4.4 | `feedback correct` 人工糾偏 | `cli.py` |
| 4.5 | 經驗治理：候選 → 生效 → 淘汰、效用、快照 | `memory/governance.py` |
| 4.6 | 主控台與 timeline 顯示「套用了哪些經驗、改變了哪些分數」 | `observe/` |

**完成定義**：S1 連跑兩次，第 2 次 trace 顯示經驗被套用，且抓取數與成本下降；`test_m2_learning_curve.py` 通過。

---

## Phase 5 — 自治邊界與失效處置（需求 C）

| # | 任務 | 產出 |
|---|---|---|
| 5.1 | PolicyGate 完整規則（06 §2.2） | `core/policy.py` |
| 5.2 | 核准流程：`approvals list / approve / reject`、`research resume` | `cli.py` |
| 5.3 | 注入防護：資料區塊包裹、樣式掃描、Frontier 來源限制 | `core/`、`llm/` |
| 5.4 | 失效模式 F1–F7 測試與混沌測試 | `tests/` |

**完成定義**：06 §3 測試矩陣全數通過。

---

## Phase 6 — 評測與研究成果收斂（需求 B）

| # | 任務 | 產出 |
|---|---|---|
| 6.1 | 確定性評分器 | `evals/scorer.py` |
| 6.2 | Runner：`eval run / train / compare`、消融 | `evals/runner.py` |
| 6.3 | 免費確定性測試（05 §4.5） | `tests/` |
| 6.4 | 完整 Day 1 vs Day N 評測（約 US$40，需事先同意） | `evals/reports/` |
| 6.5 | **研究成果文件**：研究問題的回答、評測數據、觀察到的 Agent 行為、限制、未來工作 | `docs/07-research-findings.md` |

**完成定義**：`07-research-findings.md` 以數據回答 RQ1–RQ3，並誠實列出未達標項目與原因。

---

## 執行前需準備

| 項目 | 何時 | 說明 |
|---|---|---|
| Anthropic API key | Phase 0.4 | 環境變數 `ANTHROPIC_API_KEY` |
| 評測預算同意 | Phase 6.4 | 約 US$40（實測後更新） |
