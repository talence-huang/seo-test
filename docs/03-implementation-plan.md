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

**實作結果（已完成）**：

| 項目 | 結果 |
|---|---|
| s1（真實 Opus 規劃器） | 找到 `shieldguide.sim/guide/{slug}`，變動類型與標準答案逐項吻合；抓取 57 / 300 次，LLM US$0.044，環境判定違規 0 |
| 6 個情境（FakeLLM） | 5 個找到正確範本；全部在預算內、環境判定違規 0 |
| 測試 | 全專案 103 個通過（Phase 2 新增 26 個） |

**研究發現（將收錄於 `07-research-findings.md`）**：

1. **背景變動會被誤認為策略變動（s3 失敗）**：planhive.sim 有 6,000 個標籤頁，本來就有一部分在期間內新增；「同範本累積 3 頁變動」的規則因此誤判。sitemap 盤點其實已顯示 `/integrations/` 期間內更新比例 42%、`/tag/` 約 6%，但 Phase 2 尚未用「比較競爭解釋」的方式使用這個證據 → 交由 Phase 3（假設與鑑別性證據）與 Phase 4（人工糾偏的學習）處理，並作為 Day 1 vs Day N 的對照案例。
2. **FakeLLM 測試通過，真實 LLM 卻失敗**：真實 Opus 會給 5 組關鍵字（含 `ltc insurance` 等變體），原本把所有關鍵字詞彙合併後計算重疊率，使主題頁被誤判為非主題頁——LLM 給得越完整，Agent 反而越差。修正為「涵蓋度最高的單一片語」，並新增模擬多關鍵字的回歸測試。教訓：確定性替身無法取代至少一次真實 LLM 的端對端驗證。
3. **浮點誤差造成環境判定的禮貌違規**：Agent 等到「上次請求 + 2 秒」才發出請求，浮點運算讓間隔變成 1.9999999 秒。修正為多等 1ms，伺服器端加極小容差。
4. **可觀察性本身抓到了問題**：上述第 2 點是從 trace 中「2,578 字的長照險指南被標為非主題頁」這一行看出來的；即時主控台與 timeline 的評分明細也讓「為什麼先抓這頁」可以被逐項檢查。

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

**實作結果（已完成）**：

| 項目 | 結果 |
|---|---|
| 6 個情境（FakeLLM） | **全部**得出與標準答案一致的主張（網站、範本、變動類型逐項吻合）；干擾項皆被推翻或判為無結論 |
| s3（Phase 2 失敗的情境） | 改正：競爭假設「標籤頁新增」被 SERP 排名網址（0/3）與更新爆發度（0.9 倍，背景變動）推翻 |
| s1、s3（真實 Opus） | 皆得出正確主張，信心 0.98 / 0.94；每個 episode LLM 約 US$0.31–0.32；環境判定違規 0；報告引用全部有效 |
| 測試 | 全專案 122 個通過 |

**研究發現（將收錄於 `07-research-findings.md`）**：

1. **以中位數檢驗時序會冤枉漸進式上線**（s6）：競品 7/1～7/31 逐步上線，中位日晚於排名起始日，被判「不可能是原因」。改用**最早**觀察到的變動日（觀察到的最早日期也只是真實最早日期的上限）。
2. **紅隊可以「用數量壓倒證據」**（真實 LLM，s3）：一次審查提出 6 項部分重複的反駁，把 0.97 打回 0.57。改為單次審查最多計入 2 項。
3. **用 frontier 排序選樣會高估變動率**：frontier 偏好「最近更新」的頁面，拿它來估計比例會系統性偏高（標籤頁被算成 86% 變動）。改為隨機抽樣，並以二項檢定比較背景率；樣本不足時明說「不足以區分」。
4. **無法判定的頁面不能算進分母**：沒有歷史快照的頁面原本被當成「已檢查、未變動」。
5. **LLM 失敗會造成無限重試**：假設提出者失敗時，判斷邏輯不斷要求重新形成假設，直到「步驟數上限」安全網才停下——安全網有效，但正確行為是有限次重試後以 `no_valid_hypotheses` 結束。
6. **真實 LLM 提出的競爭假設比確定性替身更有挑戰性**：真實 Opus 會主動提出「同站另一個範本」「全站性因素」等替代解釋，正好讓鑑別性證據發揮作用；以上第 2、3、4 點都只在真實 LLM 執行時才暴露。

---

## Phase 4 — 學習機制（需求 B）

| # | 任務 | 產出 |
|---|---|---|
| 4.1 | 記憶資料庫（經驗、範本價值、獎勵紀錄、episode 成效、糾偏紀錄、稽核軌跡） | `memory/schema.sql`、`memory/store.py` |
| 4.2 | M2：三層 Beta 先驗（同網站範本 → 同類路徑 → 範本特徵）、獎勵計算 | `memory/priors.py`、`core/frontier.py` |
| 4.3 | M1：經驗結構、固定詞彙偏置、載入與套用、套用紀錄 | `memory/models.py`、`memory/session.py`、`core/actions.py`、`core/steps.py` |
| 4.4 | REFLECT：以確定性規則從成功 / 失敗萃取候選經驗、更新 M2 | `memory/reflect.py`、`core/steps.py` |
| 4.5 | 人工糾偏：撤回學習、reflector 角色把理由轉成經驗 | `memory/correct.py`、`llm/roles.py`、`cli.py` |
| 4.6 | 經驗治理：候選 → 生效 → 淘汰、衝突偵測、人工經驗只標記不淘汰、快照 | `memory/reflect.py`、`cli.py` |
| 4.7 | 主控台與 timeline 顯示「套用了哪些經驗、在哪裡套用、學到什麼」 | `observe/` |
| 4.8 | CLI：`research --memory / --no-memory / --ablate`、`memory list / show / stats / snapshot / retire`、`feedback correct / list` | `cli.py`、`core/runner.py` |

**完成定義**：S1 連跑，後續 episode 的 trace 顯示經驗被套用，且抓取數與成本下降；`tests/test_memory.py` 通過。

**實作結果（已完成，FakeLLM，數字可重現）**：

| 實驗 | 結果 |
|---|---|
| s1 連跑三次（共用記憶） | 抓取 69 → 49 → 38、API 79 → 65 → 54、步驟 348 → 228 → 148；結論皆正確。第 2 次只有 M2 先驗起作用，第 3 次 M1 經驗 L-1 生效 |
| Day 1 vs Day N（s1–s4 各訓練 2 次；s5、s6 不參與訓練） | s5 抓取 68 → 38、封鎖 2 → 0；s6 抓取 22 → 17；LLM 估算成本 −6% / −17%；結論皆正確 |
| 訓練集本身（無記憶 → 有記憶） | s2 19 → 14、s3 48 → 43、s4 52 → 22 |
| WAF 失敗經驗（s5 連跑三次） | 第 1 次成為候選、第 2 次生效、第 3 次避開 `/reviews/`：封鎖 2 → 2 → 0、抓取 68 → 52 → 37 |
| 被污染的經驗（人為植入「避開 /guide/」，實際上答案就在 /guide/） | 2 次與證據衝突後自動淘汰；結論全程正確，淘汰後抓取回到 38、23 |
| 錯誤的人工糾偏（把正確主張判為錯） | 撤回 M2 命中、撤回成功經驗的支持（L-1 退回候選、L-2 淘汰）；人工經驗被遵守，與證據衝突時只標記「建議人工檢視」、不自行淘汰；代價是抓取 49 → 63 / 73 |
| 測試 | 全專案 138 個通過（新增 15 個記憶測試：偏置驗證、快照、先驗退縮、學習曲線、消融、WAF 經驗、污染經驗淘汰、人工經驗不淘汰、糾偏回溯與遵守、無效偏置丟棄） |

**研究發現（將收錄於 `07-research-findings.md`）**：

1. **經驗只改「順序」、不改「數量」，就換不到效率**：第一版 `sampling_focus` 只是讓訊號範本先抽樣，其餘範本照樣普查；s1 第 3 次套用經驗後反而比第 2 次多抓（65 vs 49）。改成「訊號範本已有足夠證據候選時，跳過其餘範本的普查」後降到 38。
2. **跳過普查後，探索加分會把預算拉回被跳過的範本**：Day N 的 s5 在定向抓取階段抓了 14 頁 `/reviews/`（正是 WAF 範本），因為沒抽樣過的範本有最高的探索加分。改為跳過普查後只在已知範本內定向抓取：54 → 38、封鎖歸零。
3. **錯誤經驗的代價主要是成本，不是結論**：污染經驗與錯誤的人工糾偏都沒有改變結論（答案有 SERP、更新爆發、抽樣等多重證據），但會增加抓取。錯誤的人工經驗與學到的 `sampling_focus` 疊加時，定向抓取把 104 次抓取花在持續更新、每頁都像證據的標籤頁，總抓取 151 次。這暴露的是與記憶無關的成本控制漏洞（定向抓取沒有單一範本上限），加上上限 15 頁後降到 73。
4. **跨站遷移會產生負遷移**：s5 的答案範本 `summitgear.sim/category/{slug}` 從「同類路徑 category/{slug}」拿到 0.00 的先驗（訓練情境中的 category 都是干擾項），因此只抽 1 頁；靠 SERP 訊號才沒有錯過。階層式先驗與強度上限能降低但無法消除這個風險。
5. **立即判定免費證據時，有些探針需要等資料**：讓付費探針規劃者先看到免費證據後，「時序」探針在付費抽樣補齊改版日期前就被判為資料不足而作廢（既有回歸測試抓到 s3 退化）。改為資料不足時延到 REVISE 再判定。
6. **不是每個情境都能省**：s3 的成本主要在驗證階段，競爭假設必須靠付費抽樣才分得出高下；「跳過付費探針」的經驗因條件不成立而正確地沒有生效（48 → 43）。

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
