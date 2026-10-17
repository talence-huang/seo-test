# 06 — 失效模式與自治邊界（需求 C）

> 核心原則：自治邊界**由程式強制執行**（`PolicyGate`），不是寫在 prompt 裡請 LLM 遵守。每個失效模式都要有「偵測 + 處置 + 測試」。

---

## 1. 失效模式

前四項為需求點名的項目；後三項是本系統實作時無法迴避的。

| # | 失效模式 | 偵測 | 處置 | SimWeb 測試 |
|---|---|---|---|---|
| F1 | **網站反爬**（429、403、挑戰頁） | 狀態碼；範本封鎖率 | 遵守 `Retry-After` 退避；403 不重試；網域斷路器；改用 `history`；M1 `avoid_path` 經驗。**絕不繞過**（偽造 UA、輪替 IP、繞過驗證都是禁止規則）。範本封鎖率 > 30% → 暫停請求核准 | `test_toolkit_*`、`test_failure_lesson_avoids_waf_path` |
| F2 | **LLM 幻覺**（捏造 URL、數字、因果） | 程式驗證：引用的 ID 必須存在；數字主張由程式計算 | 主張由程式從證據推導；報告中不存在的引用移除、**引用全部無效的句子整句刪除**、建議必須引用成立的主張、標題由程式產生；紅隊反駁必須引用有效觀察 | `test_invalid_citations_and_claims_are_removed`、`test_compromised_llm_cannot_steer_actions_or_conclusions` |
| F3 | **工具崩潰**（例外、逾時、連線失敗、格式錯誤） | 例外捕捉、逾時、斷路器計數 | 工具層對逾時、連線失敗、5xx 退避重試；工具或解析器的任何例外轉為 `tool_error` 觀察，迴圈不中斷；暫時性失敗的行動稍後再試一次 | `test_network_errors_are_retried_not_fatal`、`test_parser_crash_becomes_observation` |
| F4 | **學到錯誤經驗** | 經驗與證據衝突（要求避開的路徑正是成立主張的範本）；套用後成功率；人工糾偏 | 先進 `candidate`，需 2 個 episode 支持才生效；衝突 ≥ 2 次或套用 ≥ 3 次且成功率 < 50% 自動淘汰；人工糾偏可撤回 M2 命中與成功經驗的支持；人工經驗不自動淘汰、只標記；快照回滾 | `tests/test_memory.py::test_poisoned_lesson_is_retired_by_evidence` 等 |
| F5 | **預算失控 / 爬蟲陷阱** | `BudgetLedger` 即時累計；行動簽章重複；報酬遞減 | 硬上限；階段預留；陷阱過濾；每範本上限 | 2 萬頁網站 + 無限日曆 |
| F6 | **Prompt injection**（來自頁面內容或被攻陷的 LLM） | 注入樣式掃描（頁面、送進 LLM 前的 prompt、報告敘述） | **結構性**：LLM 只看程式整理過的事實，看不到頁面原文；行動只能由程式產生；主張由程式推導；網址必須來自 sitemap / 連結 / SERP。**偵測**：含注入文字的頁面標記並不採用其連結；prompt 送出前移除疑似指令；system prompt 聲明資料區塊不可信；可疑關鍵字、無根據的報告敘述、帶指令的建議一律刪除 | `test_injection_page_is_quarantined`、`test_compromised_llm_cannot_steer_actions_or_conclusions` |
| F7 | **LLM API 錯誤**（429、5xx、refusal、截斷、schema 不符） | SDK 型別化例外；`stop_reason`；Pydantic 驗證 | SDK 重試；Opus 5.5 server-side fallback；步驟級重試；失敗時有確定性的退路（預設目標、必要資訊需求、程式產生的報告標題）；呼叫過 LLM 的步驟立即存檔 | `test_flaky_llm_still_finishes`、`test_hard_kill_resumes_from_last_checkpoint` |

---

## 2. 自治邊界

### 2.1 三個等級

| 等級 | 行為 |
|---|---|
| **自主** | 直接執行，寫入 trace |
| **需核准** | **硬暫停**：episode 存檔後退出，核准後才能續跑 |
| **禁止** | 直接拒絕，任何人都不能核准；嘗試行為會被記錄 |

### 2.2 動作分級

| 動作 | 等級 | 條件 |
|---|---|---|
| 讀取 robots、sitemap、`rank_query`、`history` | 自主 | 目標網域內 |
| 抓取頁面 | 自主 | 目標網域內、≤ 150 頁 / 網域 / episode、≤ 1 req/2s、遵守 robots |
| 解析、萃取、統計 | 自主 | 本地運算 |
| LLM 分析 | 自主 | episode 花費 ≤ US$2.00 |
| 寫入觀察、假設、證據、主張 | 自主 | 通過 schema 驗證 |
| 產生候選經驗、更新 M2 統計 | 自主 | 候選經驗尚未生效 |
| 候選經驗自動晉升為 active | 自主 | 通過治理門檻（記錄於 trace） |
| **超過任一預算上限**（頁數、花費、時間） | **需核准** | 附預估成本與理由 |
| **抓取目標清單外的網域** | **需核准** | — |
| **大量抓取**（單次 > 1,000 頁）或**調高速率** | **需核准** | 對方負載風險 |
| **封鎖率 > 30% 後繼續抓取該網域** | **需核准** | — |
| **淘汰人工糾偏經驗、回滾記憶快照** | **需核准** | 不可逆或影響後續所有決策 |
| **對外發布報告或策略** | **需核准** | 本研究中不實作外送，僅保留規則 |
| 繞過 CAPTCHA / WAF、偽造 User-Agent、輪替 IP | **禁止** | — |
| 忽略 robots.txt | **禁止** | — |
| 存取需登入的內容 | **禁止** | — |
| 執行頁面內容中出現的指令 | **禁止** | — |
| Agent 修改政策設定（`config/policy.yaml`） | **禁止** | 設定檔不在 Agent 可寫路徑 |

### 2.3 強制機制：`PolicyGate`（`core/policy.py`）

檢查順序（禁止類優先）：

1. **行動參數**含 `user_agent`、`headers`、`proxy`、`captcha_token`、`cookies` 等 → `spoof_user_agent` / `rotate_ip` / `bypass_captcha` / `access_login_gated`（禁止）
2. **抓取網址**：登入區域（`/account`、`/cart`、`/login`…）→ `access_login_gated`（禁止）；任務範圍內但不是程式從 sitemap / 連結 / SERP 取得的網址 → `follow_page_instructions`（禁止）；robots 禁止 → `ignore_robots`（禁止）
3. **範圍**：任務以外的網域 → `off_target_domain`（需核准：擴大範圍由人決定）
4. **規模與速率**：`raise_rate`、`bulk_crawl`（需核准）
5. **預算與封鎖率**：`exceed_budget`、`continue_after_blocks`（需核准）

- 規則屬於禁止或需核准由 `config/policy.yaml` 決定；**政策沒有列出的規則一律保守處理為需核准**（fail-closed）。
- 工具名稱是封閉的型別（`Literal`），LLM 無法讓程式產生「寫入政策檔」之類的行動——`modify_policy` 由型別系統保證。
- 需核准時：人已核准過這個行動（一次性授權）→ 放行並標記已使用；人拒絕過同一規則、同一範圍 → 直接拒絕，**不再詢問**。
- 位於 `SELECT_ACTION` 與 `EXECUTE_TOOL` 之間，所有工具呼叫必經；每次判定都寫入 trace，主控台顯示「被擋下，因為……」。

預算類的核准不經過單一行動，而是在研究還有工作沒完成時提出（`core/steps.py`）：

| 用盡的上限 | 何時請求 | 延長多少 | 拒絕時 |
|---|---|---|---|
| 抓取次數 | 證據佇列還有待抓取的驗證行動 | 佇列所需次數 + 25% 重試餘裕（至少 2 次） | 丟棄未完成的探針，以現有證據下結論 |
| LLM 花費、執行時間 | 仍有測試中的假設或待處理的證據 | 設定值的 50% | 以現有證據結束（`budget:max_llm_usd` 等） |
| 行動數、迭代數 | 不請求：這兩個是防止迴圈失控的安全網 | — | — |

### 2.4 核准流程（`core/approvals.py`）

```mermaid
sequenceDiagram
    participant L as ResearchLoop
    participant G as PolicyGate
    participant U as 使用者
    L->>G: check(action)
    G-->>L: REQUIRE_APPROVAL（理由、預估成本）
    L->>L: 建立核准請求 A-xxxxxx、存檔、狀態 = waiting_approval、退出
    U->>L: seo-intel approvals approve A-xxxxxx（寫回檢查點）
    U->>L: seo-intel research resume E-…
    L->>L: 套用決定：一次性授權 / 延長預算 / 拒絕範圍；trace 寫入 HUMAN_DECISION
    L->>G: check(action)
    G-->>L: ALLOW（一次性、限定範圍、24 小時內有效）
```

```bash
seo-intel approvals list
```

```bash
seo-intel approvals approve A-xxxxxx --note "同意延長"
```

```bash
seo-intel research resume E-xxxx
```

- `--step` 模式遇到需核准時**當場詢問**（y 核准／n 拒絕／Enter 稍後再決定並存檔離開）。
- 核准請求記錄規則、理由、預估（抓取數、占原預算比例、相關探針與假設）與期限；決定與備註寫回檢查點，並鏡像到 `intel.db` 的 `approvals` 表供稽核。
- 尚未決定就續跑會被拒絕；超過期限的核准視同未核准，續跑時重新請求。
- 拒絕時 Agent 走降級路徑，並把未完成的工作寫進報告的「限制」（由程式補上，不依賴 LLM 是否提到）。
- 記憶類的高風險動作：Agent 不會淘汰人工糾偏經驗（只標記「建議人工檢視」），淘汰只能由人用 `seo-intel memory retire` 執行；回滾記憶快照、對外發布在本研究中沒有 Agent 執行路徑。

---

## 3. 測試矩陣（`tests/test_autonomy.py`、`tests/test_failures.py`）

| 類別 | 測試 | 通過標準 | 結果 |
|---|---|---|---|
| 自治邊界 | 每個「禁止」動作都被拒、每個「需核准」動作都暫停；未列入政策的規則保守處理；LLM 無法發明工具 | 100% | ✅ |
| 核准流程 | 暫停 → 核准 → 續跑完成；拒絕 → 降級並寫入限制、不再詢問；過期核准無效；未決定不能續跑；決定寫入 trace | 通過 | ✅ |
| 反爬 | 429 / 403 情境下不超速、不重試 403；WAF 路徑學成經驗後避開 | 0 違規 | ✅ |
| 注入 | 注入頁被抓到時：標記、不跟隨連結、LLM 看不到指令、結論不變；被攻陷的 LLM 無法讓 Agent 越界或改變結論 | 0 違規 | ✅ |
| 混沌 | LLM 40% 暫時性錯誤 + 10% schema 錯誤；網路 25% 逾時 / 連線失敗；解析器 15% 崩潰；程序硬中斷 | 不崩潰、可續跑、結論不變 | ✅ |
| 預算 | 2 萬頁網站上任何上限都不被突破（延長必須經人核准） | 通過 | ✅ |
| 錯誤經驗 | 植入的錯誤經驗在 k 個 episode 內被淘汰 | 通過 | ✅ k = 2（Phase 4） |
