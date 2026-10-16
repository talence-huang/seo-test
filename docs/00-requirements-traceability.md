# 00 — 需求對照表

> 逐條對應：需求 → 設計 → 程式模組 → 如何觀察 / 驗證。狀態：⬜ 未開始 / 🔄 進行中 / ✅ 完成並驗證。

## A. Autonomous Research Loop

| 需求 | 設計 | 模組 | 觀察 / 驗證 | 狀態 |
|---|---|---|---|---|
| 不得是黑盒子 | [04 §1、§6](04-research-loop.md) | `core/loop.py`、`observe/` | 即時主控台、`--step`、`timeline.html` | ⬜ |
| 定義目標 → 決定需要資訊 | `DEFINE_GOAL`、`PLAN_INFO_NEEDS` | `core/steps/` | trace 中的 `ResearchGoal`、`InfoNeed[]` | ⬜ |
| 調用 Tool → 解析結果 | `SELECT_ACTION`、`EXECUTE_TOOL`、`PARSE_RESULT` | `core/steps/`、`tools/` | 每個行動的評分明細與觀察 ID | ⬜ |
| 形成假設 → 尋找客觀證據 | `FORM_HYPOTHESES`、`SEEK_EVIDENCE` | `core/beliefs.py` | 假設、鑑別行動 | ⬜ |
| 修正推論 → 決定下一步 | `REVISE_BELIEFS`、`DECIDE_NEXT` | `core/beliefs.py`、`controller.py` | 信心度曲線、結束理由 | ⬜ |
| 成本控制：數萬頁中決定優先抓取 | [04 §4](04-research-loop.md) | `core/frontier.py`、`budget.py` | 2 萬頁 SimWeb 網站；浪費率指標 | ⬜ |
| 時間 / Token / API 限制 | [04 §4.5–4.6](04-research-loop.md) | `core/budget.py`、`llm/client.py` | 預算曲線；上限不可突破測試 | ⬜ |
| 框架選擇說明 | [02 §3](02-seo-intel-agent-design.md) | — | — | ✅ |

## B. Memory & Learning

| 需求 | 設計 | 模組 | 觀察 / 驗證 | 狀態 |
|---|---|---|---|---|
| 至少一個學習機制 | [05 §1–3](05-memory-and-evals.md)：M1、M2 | `memory/` | 單元測試 | ⬜ |
| 調用過往成功 / 失敗 / 人工糾偏經驗 | M1 三種來源 | `memory/lessons.py`、`cli.py` | trace 的 `lessons_applied` | ⬜ |
| 決策優於過去 | `biases`、M2 後驗 | `core/frontier.py` | 同情境兩次 trace 對照；消融 | ⬜ |
| Evals：可執行的測試方法 | [05 §4](05-memory-and-evals.md) | `evals/` | `seo-intel eval run / train / compare` | ⬜ |
| Evals：量化指標 | F1、首個關鍵證據抓取數、浪費率、成本、幻覺率、安全違規、糾偏遵循率 | `evals/scorer.py` | `evals/reports/` | ⬜ |
| 證明比第 1 天更好 | Day 1 vs Day N（保留測試集）＋ 消融 | `evals/runner.py` | `docs/07-research-findings.md` | ⬜ |

## C. Production-Ready Thinking & Autonomy Boundary

| 需求 | 設計 | 模組 | 觀察 / 驗證 | 狀態 |
|---|---|---|---|---|
| 失效模式：反爬、幻覺、Tool 崩潰、錯誤經驗 | [06 §1](06-reliability-and-autonomy.md) F1–F4 | `tools/base.py`、`report/`、`memory/governance.py` | 06 §3 測試矩陣 | ⬜ |
| 其他失效模式 | F5–F7 | 各模組 | 同上 | ⬜ |
| 可 100% 自主的動作 | [06 §2.2](06-reliability-and-autonomy.md) | `core/policy.py` | 測試矩陣 | ⬜ |
| 高風險動作須暫停等待人工核准 | 同上「需核准」 | `core/policy.py`、`cli.py` | 暫停 → 核准 → 續跑測試；主控台顯示擋下理由 | ⬜ |
| 由程式強制 | `PolicyGate` | `core/policy.py` | LLM 輸出無法改變判定的測試 | ⬜ |

## 展示劇本

```bash
seo-intel research --scenario s1 --step
```

```bash
seo-intel trace html <episode_id>
```

```bash
seo-intel feedback correct --episode <episode_id> --claim <claim_id> --verdict wrong --reason "..."
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
5. 觸發一次超預算 → 暫停 → 核准 → 續跑，並確認注入頁被忽略（C）。
