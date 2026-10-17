# 00 — 需求對照表

> 逐條對應：需求 → 設計 → 程式模組 → 如何觀察 / 驗證。狀態：⬜ 未開始 / 🔄 進行中 / ✅ 完成並驗證。

## A. Autonomous Research Loop

| 需求 | 設計 | 模組 | 觀察 / 驗證 | 狀態 |
|---|---|---|---|---|
| 不得是黑盒子 | [04 §1、§6](04-research-loop.md) | `core/loop.py`、`observe/` | 即時主控台、`--step`、`trace show`、`timeline.html` | ✅ |
| 定義目標 → 決定需要資訊 | `DEFINE_GOAL`、`PLAN_INFO_NEEDS` | `core/steps.py`、`llm/roles.py` | trace 中的 `ResearchGoal`、`InfoNeed[]` 與 LLM 結構化輸出 | ✅ |
| 調用 Tool → 解析結果 | `SELECT_ACTION`、`EXECUTE_TOOL`、`PARSE_RESULT` | `core/steps.py`、`core/actions.py`、`tools/` | 每個行動的評分明細、HTTP 請求、觀察 ID | ✅ |
| 形成假設 → 尋找客觀證據 | `FORM_HYPOTHESES`、`SEEK_EVIDENCE` | `core/beliefs.py`、`core/probes.py`、`core/digest.py` | 可證偽檢查、6 種證據探針、判定依據逐筆列出 | ✅ |
| 修正推論 → 決定下一步 | `REVISE_BELIEFS`、`DECIDE_NEXT` | `core/beliefs.py`、`core/steps.py` | log-odds 信心曲線（每筆證據一階）、紅隊驗證、結束理由 | ✅ |
| 結論可追溯（非黑盒子） | `REPORT` | `core/claims.py` | 結構化主張由程式推導；報告引用逐一驗證；`report.md` | ✅ |
| 成本控制：數萬頁中決定優先抓取 | [04 §4](04-research-loop.md) | `core/frontier.py`、`core/budget.py` | 2 萬頁網站只抓 57 次即找到答案；timeline 範本統計 | ✅ |
| 時間 / Token / API 限制 | [04 §4.5–4.6](04-research-loop.md) | `core/budget.py`、`llm/anthropic_client.py` | 預算曲線；所有情境驗證額度未被動用（測試） | ✅ |
| 框架選擇說明 | [02 §3](02-seo-intel-agent-design.md) | — | — | ✅ |

## B. Memory & Learning

| 需求 | 設計 | 模組 | 觀察 / 驗證 | 狀態 |
|---|---|---|---|---|
| 至少一個學習機制 | [05 §1–3](05-memory-and-evals.md)：M1、M2 | `memory/` | `tests/test_memory.py`（15 個） | ✅ |
| 調用過往成功 / 失敗 / 人工糾偏經驗 | M1 三種來源 | `memory/reflect.py`、`memory/correct.py`、`cli.py` | trace 的 `lessons_applied`、主控台「套用經驗」、timeline「記憶與學習」、`memory show` 稽核軌跡 | ✅ |
| 決策優於過去 | `biases`、M2 先驗 | `core/frontier.py`、`core/actions.py`、`core/steps.py` | s1 連跑抓取 69 → 49 → 38；未參與訓練的 s5 68 → 38（[03 Phase 4](03-implementation-plan.md)）；消融開關 `--ablate` | 🔄 FakeLLM 已驗證；正式評測於 Phase 6 |
| Evals：可執行的測試方法 | [05 §4](05-memory-and-evals.md) | `evals/` | `seo-intel eval run / train / compare` | ⬜ |
| Evals：量化指標 | F1、首個關鍵證據抓取數、浪費率、成本、幻覺率、安全違規、糾偏遵循率 | `evals/scorer.py` | `evals/reports/` | ⬜ |
| 證明比第 1 天更好 | Day 1 vs Day N（保留測試集）＋ 消融 | `evals/runner.py` | `docs/07-research-findings.md` | ⬜ |

## C. Production-Ready Thinking & Autonomy Boundary

| 需求 | 設計 | 模組 | 觀察 / 驗證 | 狀態 |
|---|---|---|---|---|
| 失效模式：反爬、幻覺、Tool 崩潰、錯誤經驗 | [06 §1](06-reliability-and-autonomy.md) F1–F4 | `tools/http.py`、`core/claims.py`、`core/steps.py`、`memory/reflect.py` | 06 §3 測試矩陣；`tests/test_failures.py`、`tests/test_memory.py` | ✅ |
| 其他失效模式 | F5–F7（預算失控、prompt injection、LLM API 錯誤） | `core/budget.py`、`core/policy.py`、`llm/` | 被攻陷的 LLM、注入頁、LLM / 網路 / 解析器混沌、程序硬中斷 | ✅ |
| 可 100% 自主的動作 | [06 §2.2](06-reliability-and-autonomy.md) | `core/policy.py` | `tests/test_autonomy.py` 判定矩陣 | ✅ |
| 高風險動作須暫停等待人工核准 | 同上「需核准」；預算延長附預估成本 | `core/approvals.py`、`cli.py` | 暫停 → 核准 / 拒絕 / 過期 → 續跑測試；`--step` 當場核准；trace 的 `HUMAN_DECISION` | ✅ |
| 由程式強制 | `PolicyGate`（fail-closed）；工具名稱是封閉型別 | `core/policy.py` | 被攻陷的 LLM 無法越界或改變結論；LLM 無法發明工具 | ✅ |

## 展示劇本

```bash
seo-intel research --scenario s1 --step
```

```bash
seo-intel trace html <episode_id>
```

```bash
seo-intel feedback correct <episode_id> --claim C1 --verdict wrong --reason "..."
```

```bash
seo-intel research --scenario s1
```

```bash
seo-intel eval compare day1 dayN --ablate M1,M2
```

1. 逐步模式跑 S1，觀察 8 個步驟、URL 優先序決策、假設與證據（A）。
2. 開啟 `timeline.html` 檢視完整決策過程（A）。
3. 給一次人工糾偏後重跑，比較兩次的經驗套用、抓取數與成本（B）。
4. 檢視 Day 1 vs Day N 評測與消融（B）。
5. 以緊縮預算（`--max-fetches 30 --step`）觸發一次超預算 → 當場核准或拒絕 → 續跑，並在 timeline 的「自治邊界」區段檢查核准紀錄與限制（C）。
