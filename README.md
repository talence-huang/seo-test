# seo-intel

可觀察的自主 SEO 競爭情報研究 Agent（研究用途）。

研究目標：實作一個**用程式寫成、每一步都可觀察**的研究迴圈，在有限預算下從數萬頁網站中找出有證據支撐的結論；並驗證經驗回流能讓後續分析**可量化地**變好。研究環境為可重現的模擬世界 SimWeb，不連接真實資料源。

## 目前進度

| Phase | 內容 | 狀態 |
|---|---|---|
| 0 | 專案骨架、SQLite schema、LLM client（結構化輸出、成本計量、FakeLLM、錄製重播） | 完成（Haiku 與 Opus 角色皆已真實驗證） |
| 1 | SimWeb 研究環境（6 個情境、反爬行為、陷阱、注入頁、模擬排名與歷史、自動推導標準答案） | 完成 |
| 2 | 研究迴圈與可觀察性（狀態機、工具層、Frontier、預算、PolicyGate 基本版、即時主控台、trace、timeline、暫停續跑） | 完成 |
| 3–6 | 假設證據、學習、自治邊界、評測 | 未開始 |

## 安裝

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

真實 LLM 呼叫需要 Anthropic API 金鑰：在專案根目錄的 `.env` 填入 `ANTHROPIC_API_KEY=...`（若沒有 `.env`，從 `.env.example` 複製一份）。`.env` 不會被 commit；已存在的系統環境變數優先。可用 `seo-intel config show` 確認是否已讀到金鑰。

## 指令

```powershell
seo-intel version          # 版本與 schema 版本
seo-intel config show      # 目前生效的模型、價格、預算、自治邊界設定
seo-intel db init          # 建立 data/intel.db
seo-intel db info          # 資料表與筆數
seo-intel llm ping         # 一次真實 API 呼叫，顯示 token 與成本
seo-intel simweb list                    # 列出研究情境
seo-intel simweb inspect --scenario s1   # 情境細節、標準答案、植入效應可觀察性
seo-intel simweb fetch -s s1 <網址>      # 在模擬世界中抓取一個網址
seo-intel research --scenario s1 --step  # 逐步觀察研究迴圈（--llm fake 免費）
seo-intel research resume <episode>      # 從檢查點續跑
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
  cli.py           seo-intel 指令
src/simweb/        研究環境（獨立套件）：世界產生器、反爬行為、模擬排名與歷史 API
evals/scenarios/   6 個研究情境（4 訓練 / 2 測試）
tests/             單元測試
docs/              設計文件
```
