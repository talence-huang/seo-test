# seo-intel

可觀察的 SEO 競爭情報研究 Agent：輸入競品網址與關鍵字，Agent 會自己規劃、抓取競品網站、比對頁面歷史版本、提出並檢驗假設，最後給出有證據支撐的結論——每一步的決策、理由與證據都看得到。

> 本專案原本是研究原型（在模擬世界 SimWeb 中以確定性的假 LLM 做評測）。2026-10-17 起移除了模擬世界、假 LLM 與評測工具，只保留研究真實網站的功能；研究期間的完整版本與數據保留在 git 標籤 `research-final`（見 [docs/07](docs/07-research-findings.md)）。

## 安裝

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

在專案根目錄的 `.env` 填入金鑰（若沒有 `.env`，從 `.env.example` 複製一份；`.env` 不會被 commit）：

| 金鑰 | 必要性 | 用途 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 必要 | Agent 的規劃、假設、紅隊驗證與報告使用真實 Opus（每次研究約 US$0.3） |
| `SERPAPI_API_KEY` | 選填 | 取得 Google 台灣的真實搜尋結果（[SerpApi](https://serpapi.com/)，按次計費） |

可用 `seo-intel config show` 確認是否已讀到金鑰。

## 網頁介面（最簡單的用法）

```powershell
seo-intel web
```

瀏覽器會自動打開 `http://127.0.0.1:8765/`（只接受本機連線；Ctrl+C 結束）。

| 分頁 | 可以做什麼 |
|---|---|
| 研究 | 輸入競品網址、關鍵字（每行一個）、研究期間與排名資料來源後開始研究；即時看到每一步（點一下展開理由、候選行動與分數、LLM 輸出、HTTP 請求）；逐步模式可按「下一步／連續執行／暫停並存檔」；結束後看結論、假設、限制、學到什麼，開啟完整時間軸，或糾正結論 |
| 紀錄與核准 | 所有研究紀錄；待核准的請求（附預估）可直接核准或拒絕，Agent 隨即續跑；暫停的研究可繼續 |
| 記憶 | 經驗庫（狀態、來源、套用成效、程式套用的偏置、稽核軌跡、淘汰）、人工糾偏紀錄、每次研究的成效、M2 範本價值 |

網頁與下列指令共用同一套檢查點、trace 與資料庫，兩者可以交替使用。

## 研究怎麼進行

```powershell
seo-intel research --site https://example.com --keyword 高山茶 --keyword 烏龍茶 --serp serpapi --max-fetches 80
```

| 資料 | 來源 | 說明 |
|---|---|---|
| 網站結構、頁面內容 | 直接抓取（robots.txt → sitemap，沒有 sitemap 時從首頁連結開始） | 只抓公開頁面、遵守 robots.txt 與每個網站 2 秒的抓取間隔 |
| 頁面改了什麼 | Wayback Machine 歷史快照（免費） | 部分頁面可能沒有快照 |
| 目前的搜尋排名 | `--serp serpapi`：SerpApi 取得 Google 台灣結果；`--serp csv --serp-csv 檔案`：從 Semrush、Ahrefs、Search Console 匯出的「關鍵字、排名、網址」；`--serp none`：不使用 | 不直接爬 Google（違反服務條款）；每次查到的排名都會存進 `data/serp_history.jsonl`，下次研究同一個關鍵字時就能算出名次變化 |
| 排名歷史 | 沒有 | 所以研究的是「競品在這些關鍵字做了什麼內容策略」，不是「我們為什麼掉排名」 |

- 真實網站沒有標準答案，結論的可信度來自證據本身（每個主張都由程式從證據推導，能點回原始觀察）。
- 高風險動作（預算用盡要延長、抓取任務以外的網域、封鎖率過高仍要繼續）會暫停等你核准。
- 研究的合法性與合規性由使用者負責，例如目標產業在當地是否合法、網站的使用條款。

## 指令

```powershell
seo-intel web                            # 本機網頁介面（--port 換埠、--no-open 不自動開瀏覽器）
seo-intel research --site <網址> -k <關鍵字> [--serp none|serpapi|csv] [--step]   # 研究競品網站
seo-intel research resume <episode>      # 從檢查點續跑（沿用原本的記憶與預算設定；先套用人工核准的決定）
seo-intel approvals list                 # 待核准的請求（--all 含已決定的）
seo-intel approvals approve <A-id|episode> --note "..."   # 核准（一次性、24 小時內有效）
seo-intel approvals reject <A-id|episode> --note "..."    # 拒絕：改走降級路徑並寫入報告限制
seo-intel feedback correct <episode> --claim C1 --verdict wrong --reason "..."   # 糾正結論（--no-llm 只保存理由原文）
seo-intel feedback list                  # 糾偏紀錄
seo-intel memory list                    # 經驗庫：狀態、來源、支持數、套用成效（--all 含已淘汰）
seo-intel memory show L-1                # 一條經驗的內容與稽核軌跡
seo-intel memory stats                   # M2 範本價值與每次研究的成效
seo-intel memory snapshot <檔案>         # 備份記憶
seo-intel memory retire L-1 --reason ... # 人工淘汰經驗
seo-intel trace list                     # 列出研究紀錄
seo-intel trace show <episode>           # 終端機重播完整時間軸
seo-intel trace html <episode>           # 產生 runs/<episode>/timeline.html
seo-intel config show                    # 目前生效的模型、價格、預算、自治邊界設定
seo-intel llm ping                       # 一次真實 API 呼叫，顯示 token 與成本
seo-intel db init / db info              # 建立 data/intel.db、查看資料表與筆數
```

## 測試

```powershell
pytest              # 單元測試（不連網、不呼叫 LLM、不花費；含評測流程本身的測試）
pytest -m live      # 端對端研究與 API 測試（呼叫真實 Opus，少量費用；網站以模擬傳輸代替）
```

## 評測：Agent 有沒有比第 1 天更好

用 8 個由程式產生、有標準答案的測試網站（4 個訓練、4 個測試），比較「空記憶」（Day 1）與「學過之後」（Day N）的正確率、抓取次數與浪費的請求；同時植入一條錯誤經驗，看它多久被淘汰。網站、Wayback 與 SerpApi 都以模擬傳輸代替，不碰任何真實網站；Agent 本身照常使用真實 Opus。設計與結果見 [docs/08](docs/08-eval-day1-vs-dayN.md)。

```powershell
python -m evals run --max-usd 15                 # 完整評測（35 次研究，約 US$12，約 1 小時；超過上限即停止）
python -m evals replay evals/reports/<名稱>       # 用錄製的 LLM 回應重跑並比對結果（不需要金鑰、不花錢）
python -m evals one s1                           # 只研究一個測試網站一次（約 US$0.35）
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
| [07 研究成果](docs/07-research-findings.md) | 研究期間（模擬世界）的數據、發現、限制與未來工作 |
| [08 Day 1 vs Day N 評測](docs/08-eval-day1-vs-dayN.md) | 測試網站評測（真實 Opus）的設計與結果 |
| [09 Coding Agent 協作紀錄](docs/09-development-log.md) | 從需求到實作的關鍵節點、指令、變更與發現 |

docs/01–05、07 記錄的是研究期間的設計與成果，其中提到的 SimWeb、FakeLLM 與舊評測工具已移除，可從標籤 `research-final` 取回；docs/00、06、08 反映目前版本。

## 專案結構

```
config/            模型、價格、預算、自治邊界設定
src/seo_intel/
  config.py        設定載入與驗證
  store/           SQLite schema 與存取層
  llm/             Anthropic client、成本計量、錄製重播、角色 prompt
  core/            研究迴圈：狀態機、步驟、行動候選、Frontier、預算、PolicyGate、核准、檢查點
  tools/           HTTP 包裝層、萃取、差異、分詞、歷史快照比對、網站探勘
  real/            研究環境：SERP 來源（SerpApi / CSV）、Wayback 歷史、真實時鐘與任務建立
  memory/          記憶：經驗庫（M1）、URL 價值先驗（M2）、REFLECT 規則、人工糾偏、治理（data/memory.db）
  observe/         即時主控台、trace、資料庫紀錄、timeline.html
  web/             本機網頁介面（標準庫 HTTP 伺服器 + 單頁前端，無額外套件）
  cli.py           seo-intel 指令
evals/             Day 1 vs Day N 評測：測試網站產生器、評分器、評測流程、報告（python -m evals）
tests/             單元測試（tests/realsite.py 是以模擬傳輸代替真實網站的測試資料）
docs/              設計文件
```
