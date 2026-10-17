# 00 — 需求對照表

> 逐條對應：需求 → 設計 → 程式模組 → 如何觀察 / 驗證。狀態：✅ 完成並驗證 / ⚠ 部分成立（附說明）。
>
> **反映目前版本（2026-10-17）**：模擬世界 SimWeb 與假 LLM 已移除，產品只研究真實網站；需求 B 的評測改用有標準答案的測試網站與真實 Opus（[08](08-eval-day1-vs-dayN.md)）。只在研究期間做過、目前版本沒有對應測試的驗證，會註明「僅 `research-final`」（`git checkout research-final` 可取回）。

## A. Autonomous Research Loop

| 需求 | 設計 | 模組 | 觀察 / 驗證 | 狀態 |
|---|---|---|---|---|
| 不得是黑盒子 | [04 §1、§6](04-research-loop.md) | `core/loop.py`、`observe/`、`web/` | 網頁介面逐步展開（理由、候選行動與分數、LLM 輸出、HTTP 請求）、`--step`、`trace show`、`timeline.html` | ✅ |
| 定義目標 → 決定需要資訊 | `DEFINE_GOAL`、`PLAN_INFO_NEEDS` | `core/steps.py`、`llm/roles.py` | trace 中的 `ResearchGoal`、`InfoNeed[]`；真實網站拿不到的資料（排名歷史）由程式排除並說明原因 | ✅ |
| 調用 Tool → 解析結果 | `SELECT_ACTION`、`EXECUTE_TOOL`、`PARSE_RESULT` | `core/steps.py`、`core/actions.py`、`tools/`、`real/` | 每個行動的評分明細、HTTP 請求、觀察 ID；工具：robots / sitemap 探勘、頁面抓取、SerpApi / CSV 排名、Wayback 歷史比對 | ✅ |
| 形成假設 → 尋找客觀證據 | `FORM_HYPOTHESES`、`SEEK_EVIDENCE` | `core/beliefs.py`、`core/probes.py`、`core/digest.py` | 可證偽檢查；3 種證據探針（SERP 排名網址範本、範本更新爆發度、範本變動抽樣＋Wayback 比對），判定依據逐筆列出 | ✅ |
| 修正推論 → 決定下一步 | `REVISE_BELIEFS`、`DECIDE_NEXT` | `core/beliefs.py`、`core/steps.py` | log-odds 信心曲線（每筆證據一階）、紅隊驗證、結束理由 | ✅ |
| 結論可追溯（非黑盒子） | `REPORT` | `core/claims.py` | 結構化主張由程式推導；報告引用逐一驗證；`test_units.py::test_log_odds_and_report_validation` | ✅ |
| 成本控制：數萬頁中決定優先抓取 | [04 §4](04-research-loop.md) | `core/frontier.py`、`core/budget.py` | 評測：7,000–19,000 頁的測試網站，Day 1 平均 33 次抓取、學習後 18 次（[08 §5](08-eval-day1-vs-dayN.md)）；timeline 範本統計 | ✅ |
| 時間 / Token / API 限制 | [04 §4.5–4.6](04-research-loop.md) | `core/budget.py`、`llm/anthropic_client.py` | 預算曲線；超過上限必須經人核准（`test_units.py::test_budget_approval_extends_once`）；評測 35 次研究都在預算內完成（核准請求 0 次） | ✅ |
| 框架選擇說明 | [02 §3](02-seo-intel-agent-design.md) | — | — | ✅ |

## B. Memory & Learning

| 需求 | 設計 | 模組 | 觀察 / 驗證 | 狀態 |
|---|---|---|---|---|
| 至少一個學習機制 | [05 §1–3](05-memory-and-evals.md)：M1 經驗庫、M2 URL 價值 | `memory/` | `test_units.py::test_memory_store_priors_and_human_lessons`、`test_retirement_compares_with_episodes_without_the_lesson`；評測的訓練過程學到 3 條經驗（[08 §5](08-eval-day1-vs-dayN.md)） | ✅ |
| 調用過往成功 / 失敗 / 人工糾偏經驗 | M1 三種來源 | `memory/reflect.py`、`memory/correct.py` | trace 的 `lessons_applied`、網頁「記憶」分頁、`memory show` 稽核軌跡；評測有腳本化的人工糾偏（本次評測沒有出現錯誤結論，未觸發） | ✅ |
| 決策優於過去 | `biases`、M2 先驗 | `core/frontier.py`、`core/actions.py`、`core/steps.py` | 沒看過的測試網站：抓取 −48%、步驟 −52%、浪費的請求 5 → 0（[08 §5.1](08-eval-day1-vs-dayN.md)） | ✅（效率） |
| Evals：可執行的測試方法 | [08](08-eval-day1-vs-dayN.md) | `evals/` | `python -m evals run`（真實 Opus）；`python -m evals replay <目錄>`（用錄製的回應免費重現）；`tests/test_evals.py`（免費，驗證評測流程本身） | ✅ |
| Evals：量化指標 | 正確率 / F1、首個關鍵證據抓取數、抓取次數、浪費的請求、LLM 花費、未驗證主張、安全違規、核准請求 | `evals/score.py`、`evals/report.py` | 評分只依標準答案與請求紀錄，不用 LLM 評分 | ✅ |
| 證明比第 1 天更好 | Day 1 vs Day N（同一批測試網站，測試網站永不寫回記憶）＋對照網站 | `evals/protocol.py` | 效率顯著改善；正確率沒有改善（相似網站 3/9 → 2/9，對照網站 3/3 → 1/3，信賴區間跨過 0）。瓶頸在「是否下結論」的判定，見 [08 §5.3](08-eval-day1-vs-dayN.md) | ⚠ 效率已證明，正確率未證明 |

## C. Production-Ready Thinking & Autonomy Boundary

| 需求 | 設計 | 模組 | 觀察 / 驗證 | 狀態 |
|---|---|---|---|---|
| 失效模式：反爬、幻覺、Tool 崩潰、錯誤經驗 | [06 §1](06-reliability-and-autonomy.md) F1–F4 | `tools/http.py`、`core/claims.py`、`core/steps.py`、`memory/reflect.py` | 06 §1 每列的驗證欄；評測：防火牆路徑學成經驗後避開、植入的錯誤經驗在第 2 次研究後被淘汰 | ✅（網路與解析器的混沌測試僅 `research-final`） |
| 其他失效模式 | F5–F7（預算失控 / 爬蟲陷阱、prompt injection、LLM API 錯誤） | `core/budget.py`、`core/policy.py`、`llm/` | 評測 35 次研究：安全違規 0、注入頁 19/19 被偵測、程式崩潰 0；`test_evals.py`：每次 LLM 呼叫都失敗時研究仍跑完 | ✅（被攻陷的 LLM、程序硬中斷僅 `research-final`） |
| 可 100% 自主的動作 | [06 §2.2](06-reliability-and-autonomy.md) | `core/policy.py` | `test_units.py` 判定矩陣（`test_forbidden_actions_are_denied`、`test_robots_disallowed_url_is_denied`、`test_risky_actions_require_approval`） | ✅ |
| 高風險動作須暫停等待人工核准 | 06 §2.2「需核准」；預算延長附預估成本 | `core/approvals.py`、`cli.py`、`web/` | `test_units.py`：一次性授權、拒絕後不再詢問、過期無效、預算只延長一次；網頁「紀錄與核准」分頁；trace 的 `HUMAN_DECISION` | ✅ |
| 由程式強制 | `PolicyGate`（fail-closed）；工具名稱是封閉型別 | `core/policy.py` | `test_units.py::test_budget_unknown_rule_and_llm_tools`（未列入政策的規則一律需核准、LLM 無法發明工具） | ✅ |
| 對外發布策略 | 06 §2.2 | `config/policy.yaml` | `publish_external` 列為需核准；目前沒有任何對外發布的執行路徑（報告只寫進本機） | ✅（規則） |

## 展示劇本

```bash
seo-intel web
```

```bash
python -m evals replay evals/results/20261017-opus-v2
```

```bash
seo-intel research --site <網址> --keyword <關鍵字> --max-fetches 30 --step
```

1. 網頁「研究」分頁：輸入競品網址與關鍵字，用逐步模式觀察每一步的理由、候選行動與分數、假設與證據（A）。
2. 研究結束後開啟完整時間軸；在結論上按「糾正」，到「記憶」分頁看人工經驗與稽核軌跡（B）。
3. 重播評測：不需要金鑰、不花錢，重現 Day 1 vs Day N 的結果與報告（B、C）。
4. 以緊縮預算觸發一次超預算 → 當場核准或拒絕 → 續跑，並在時間軸的「自治邊界」區段檢查核准紀錄與限制（C）。研究真實網站前，請先確認目標網站的使用條款與當地法規。
