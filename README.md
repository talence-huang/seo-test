# seo-intel

可觀察的自主 SEO 競爭情報研究 Agent（研究用途）。

研究目標：實作一個**用程式寫成、每一步都可觀察**的研究迴圈，在有限預算下從數萬頁網站中找出有證據支撐的結論；並驗證經驗回流能讓後續分析**可量化地**變好。研究環境為可重現的模擬世界 SimWeb，不連接真實資料源。

## 目前進度

| Phase | 內容 | 狀態 |
|---|---|---|
| 0 | 專案骨架、SQLite schema、LLM client（結構化輸出、成本計量、FakeLLM、錄製重播） | 完成（Haiku 與 Opus 角色皆已真實驗證） |
| 1 | SimWeb 研究環境（6 個情境、反爬行為、陷阱、注入頁、模擬排名與歷史、自動推導標準答案） | 完成 |
| 2 | 研究迴圈與可觀察性（狀態機、工具層、Frontier、預算、PolicyGate 基本版、即時主控台、trace、timeline、暫停續跑） | 完成 |
| 3 | 假設、證據與報告（可證偽假設、6 種證據探針、log-odds 信心、紅隊驗證、結構化主張、引用驗證） | 完成 |
| 4 | 學習機制（M1 經驗庫、M2 URL 價值、REFLECT、人工糾偏、經驗治理） | 完成（FakeLLM 驗證：s1 連跑抓取 69 → 49 → 38） |
| 5 | 自治邊界與失效處置（完整 PolicyGate、人工核准流程、注入防護、混沌測試） | 完成（被攻陷的 LLM 也無法讓 Agent 越界或改變結論） |
| 6 | 評測與研究成果收斂（確定性評分、Day 1 / Day N 協定、消融、緊縮預算、[研究成果](docs/07-research-findings.md)） | 完成（FakeLLM：學習後抓取 −25%；真實 LLM 評測列為未來工作） |

## 安裝

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

真實 LLM 呼叫需要 Anthropic API 金鑰：在專案根目錄的 `.env` 填入 `ANTHROPIC_API_KEY=...`（若沒有 `.env`，從 `.env.example` 複製一份）。`.env` 不會被 commit；已存在的系統環境變數優先。可用 `seo-intel config show` 確認是否已讀到金鑰。

## 網頁介面（最簡單的用法）

```powershell
seo-intel web
```

瀏覽器會自動打開 `http://127.0.0.1:8765/`（只接受本機連線；Ctrl+C 結束）。

| 分頁 | 可以做什麼 |
|---|---|
| 研究 | 選情境、LLM、記憶、預算與世界種子後開始研究；即時看到每一步（點一下展開理由、候選行動與分數、LLM 輸出、HTTP 請求）；逐步模式可按「下一步／連續執行／暫停並存檔」；結束後看結論、假設、限制、學到什麼，開啟完整時間軸，或糾正結論 |
| 紀錄與核准 | 所有研究紀錄；待核准的請求（附預估）可直接核准或拒絕，Agent 隨即續跑；暫停的研究可繼續 |
| 記憶 | 經驗庫（狀態、來源、套用成效、程式套用的偏置、稽核軌跡、淘汰）、人工糾偏紀錄、每次研究的成效、M2 範本價值 |
| 評測 | 執行 Day 1 → 訓練 → Day N → 消融 → 緊縮預算，並瀏覽評測報告 |

網頁與下列指令共用同一套檢查點、trace 與資料庫，兩者可以交替使用。

## 指令

```powershell
seo-intel version          # 版本與 schema 版本
seo-intel web              # 本機網頁介面（--port 換埠、--no-open 不自動開瀏覽器）
seo-intel config show      # 目前生效的模型、價格、預算、自治邊界設定
seo-intel db init          # 建立 data/intel.db
seo-intel db info          # 資料表與筆數
seo-intel llm ping         # 一次真實 API 呼叫，顯示 token 與成本
seo-intel simweb list                    # 列出研究情境
seo-intel simweb inspect --scenario s1   # 情境細節、標準答案、植入效應可觀察性
seo-intel simweb fetch -s s1 <網址>      # 在模擬世界中抓取一個網址
seo-intel research --scenario s1 --step  # 逐步觀察研究迴圈（--llm fake 免費）
seo-intel research resume <episode>      # 從檢查點續跑（沿用原本的記憶與預算設定；先套用人工核准的決定）
seo-intel research -s s1 --no-memory     # 不用記憶（Day 1）；--memory <檔案> 指定記憶、--ablate M1,M2 消融
seo-intel research -s s1 --max-fetches 30 --step   # 緊縮預算：預算用盡時暫停請求核准（--step 可當場決定）
seo-intel approvals list                 # 待核准的請求（--all 含已決定的）
seo-intel approvals approve <A-id|episode> --note "..."   # 核准（一次性、24 小時內有效）
seo-intel approvals reject <A-id|episode> --note "..."    # 拒絕：改走降級路徑並寫入報告限制
seo-intel eval protocol --llm fake         # 評測：Day 1 → 訓練 → Day N → 消融 → 緊縮預算（--test-offset 換新種子）
seo-intel eval protocol --llm live --quick --max-usd 8   # 真實 LLM 評測（必須給花費上限）
seo-intel memory list                    # 經驗庫：狀態、來源、支持數、套用成效（--all 含已淘汰）
seo-intel memory show L-1                # 一條經驗的內容與稽核軌跡
seo-intel memory stats                   # M2 範本價值與 episode 成效
seo-intel memory snapshot <檔案>         # 備份記憶
seo-intel memory retire L-1 --reason ... # 人工淘汰經驗
seo-intel feedback correct <episode> --claim C1 --verdict wrong --reason "..."   # 人工糾偏
seo-intel feedback list                  # 糾偏紀錄
seo-intel trace list                     # 列出 episode
seo-intel trace show <episode>           # 終端機重播完整時間軸
seo-intel trace html <episode>           # 產生 runs/<episode>/timeline.html
```

## 測試

```powershell
pytest              # 單元測試（不連網、不花費）
pytest -m live      # 真實 API 測試（少量費用）
```

## 文件

| 文件 | 內容 |
|---|---|
| [00 需求對照表](docs/00-requirements-traceability.md) | 需求 → 設計 → 模組 → 驗證 |
| [01 Hermes 架構研究](docs/01-hermes-architecture.md) | 背景研究 |
| [02 研究總覽與範疇](docs/02-seo-intel-agent-design.md) | 研究問題、範疇、架構 |
| [03 實作計畫](docs/03-implementation-plan.md) | Phase 0–6 |
| [04 研究迴圈與可觀察性](docs/04-research-loop.md) | 需求 A |
| [05 學習機制與 Evals](docs/05-memory-and-evals.md) | 需求 B |
| [06 失效模式與自治邊界](docs/06-reliability-and-autonomy.md) | 需求 C |
| [07 研究成果](docs/07-research-findings.md) | RQ1–RQ3 的數據、發現、限制與未來工作 |

## 專案結構

```
config/            模型、價格、預算、自治邊界設定
src/seo_intel/
  config.py        設定載入與驗證
  store/           SQLite schema 與存取層
  llm/             Anthropic client、成本計量、FakeLLM、錄製重播、角色 prompt
  core/            研究迴圈：狀態機、步驟、行動候選、Frontier、預算、PolicyGate、檢查點
  tools/           HTTP 包裝層、萃取、差異、排名、歷史快照、網站探勘
  observe/         即時主控台、trace、資料庫紀錄、timeline.html
  web/             本機網頁介面（標準庫 HTTP 伺服器 + 單頁前端，無額外套件）
  evals/           評測：確定性評分器、Day 1 / Day N 協定、報告
  memory/          記憶：經驗庫（M1）、URL 價值先驗（M2）、REFLECT 規則、人工糾偏、治理（data/memory.db）
  cli.py           seo-intel 指令
src/simweb/        研究環境（獨立套件）：世界產生器、反爬行為、模擬排名與歷史 API
evals/scenarios/   6 個研究情境（4 訓練 / 2 測試）
evals/reports/     評測輸出（不納入版控）
tests/             單元測試
docs/              設計文件
```
