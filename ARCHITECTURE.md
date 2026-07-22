# Survey Co-Pilot — 系統架構全書

> 整理時間 2026-07-15 · 分支 `dev` / `main`（已同步，commit `73a18c2`）· 34/34 測試通過

本文合併兩份文件：**現有系統架構**（實際程式碼現況）與**目標參考架構**（撇開現有程式碼跟教授原始建議、單純從 PLS-SEM 方法論文獻重新設計出的理想架構），並在最後給出把「現況」推進到「目標」的**單一份、有先後順序**的開發路線圖。

---

## 目錄

1. [現有系統架構（現況）](#一現有系統架構現況)
2. [目標架構的三個設計原則](#二目標架構的三個設計原則)
3. [目標架構：六層設計（L0–L6）](#三目標架構六層設計l0l6)
4. [現況 vs 目標：差異總表](#四現況-vs-目標差異總表)
5. [統一開發路線圖](#五統一開發路線圖)
6. [跨類型窮舉比較引擎：設計演進紀錄](#六跨類型窮舉比較引擎設計演進紀錄)
7. [參考文獻](#七參考文獻)
8. [範疇確認與技術棧決策（2026-07-20 更新）](#八範疇確認與技術棧決策2026-07-20-更新)
9. [Phase 1 統計框架完成紀錄（2026-07-22 更新）](#九phase-1-統計框架完成紀錄2026-07-22-更新)

---

## 一、現有系統架構（現況）

目前是一個 FastAPI 後端服務（無前端），核心是一套 PLS-SEM 統計引擎，外加兩層自動最佳化迴圈。

### 技術棧

FastAPI + Uvicorn 提供 API；`pandas` / `numpy` / `statsmodels` / `pingouin` / `factor_analyzer` / `scipy` 做統計運算；資料目前存在記憶體中的 `SESSION` dict（沒有資料庫）；GitHub Actions 跑 `pytest`；Render 免費方案部署（閒置會休眠，冷啟動約 50 秒）。

### 檔案結構

```
.
├── .github/workflows/test.yml      # push / PR 時自動跑 pytest
├── .gitignore
├── README.md
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI app、路由、SESSION 記憶體儲存
│   └── stats_engine.py             # PLS-SEM 運算 + 兩層最佳化引擎
├── requirements.txt
├── run.sh                          # uvicorn app.main:app
└── tests/
    └── test_survey_copilot.py      # 34 個測試（stats 函式 + API 端點）
```

### 資料流水線

```
POST /upload                → load_data() 解析 CSV/XLSX，依欄名自動分組成 construct_dict
    ↓
POST /analyze/measurement   → Cronbach α、Loading / AVE / CR、Cross-loadings 區辨檢查
    ↓
POST /analyze/structural    → Bootstrapping 顯著性、VIF 共線性、R²
    ↓
POST /optimize/*            → Tier 1 刪題項救 AVE、Tier 2 刪樣本救單一路徑顯著
```

### API 端點總覽

| 端點 | 對應函式 | 用途 |
|---|---|---|
| `POST /upload` | `load_data` | 上傳問卷，自動偵測構面/題項分組 |
| `POST /analyze/measurement` | `calc_cronbach` / `calc_loadings_ave_cr` / `calc_cross_loadings` | 信度、收斂效度、區辨效度全檢 |
| `POST /analyze/structural` | `calc_bootstrapping` / `calc_vif` / `calc_r_squared` | 路徑顯著性、共線性、解釋力 |
| `POST /optimize/measurement` | `optimize_measurement` | Tier 1：貪婪演算法刪最低 loading 題項直到 AVE ≥ 0.5，剩 2 題仍不達標則標記「無可救藥」 |
| `POST /optimize/path` | `optimize_structural_path` | Tier 2：單一路徑的 Cook's Distance 靶向刪樣本，上限 10% |
| `POST /analyze/full` | — | measurement + structural 一次跑完 |
| `GET /session/info` · `GET /health` | — | 狀態檢查 |

---

## 二、目標架構的三個設計原則

架構不是憑空畫的，這三點分別對應一篇方法論文獻，決定了下面六層架構的形狀。

### A. 兩階段評估是業界標準，不是偏好

PLS-SEM 的標準做法本來就是先評估測量模型（信效度），通過之後才評估結構模型（路徑顯著性）——這不是教授個人的看法，是 Hair 等人教科書寫的標準流程。架構上代表：結構模型分析必須被測量模型的通過狀態**硬性擋住**，不能是可以繞過的建議。

> Hair, Hult, Ringle & Sarstedt, *A Primer on Partial Least Squares Structural Equation Modeling (PLS-SEM)*, Sage Publications.

### B. 排除任何一份樣本，都需要「統計 + 實質」雙重理由

Careless-responding 文獻明確指出：單一指標（像只看 Mahalanobis 距離）並不可靠，必須合併多個獨立訊號（作答時間、長串重複作答、IRV、注意力檢核題）才有說服力。架構上代表：系統不能只靠 Cook's Distance 一個數字決定刪誰，要有一層獨立的「資料品質診斷」收斂多個訊號。

> Curran, P. G. (2016). Methods for the detection of carelessly invalid responses in survey data.

### C. Confirmatory 跟 exploratory 必須分開標示，否則就是 p-hacking

Simmons, Nelson & Simonsohn (2011) 證明：不揭露的研究者自由度（要不要多刪幾筆資料、要不要換依變數）可以把名目 5% 的偽陽性率推到 60%。架構上代表：任何「刪到顯著為止」的搜尋結果，都必須明確標成「exploratory / 事後分析」，跟一開始就宣告好的 confirmatory 假設分開呈現，不能混在一起變成最終結論。

> Simmons, J. P., Nelson, L. D., & Simonsohn, U. (2011). False-Positive Psychology: Undisclosed Flexibility in Data Collection and Analysis Allows Presenting Anything as Significant. *Psychological Science*.
> Center for Open Science — OSF Preregistration。

---

## 三、目標架構：六層設計（L0–L6）

由上而下是資料實際流動的方向；**L2 是硬性關卡**，**L4 的每一筆輸出都帶著「exploratory」標籤**，**L5 貫穿全部層級做版本追蹤**。每層都標註了跟現有程式碼的對應狀態。

### L0 · 研究設計宣告層 — ✅ 已完成（2026-07-22），語法用純 JSON 而非 lavaan 風格

上傳資料之前，研究者先呼叫 `POST /declare`（`app/db.py` `create_declaration()`）宣告構面、題項、假設路徑，存成獨立於資料的「理論規格」，並蓋上時間戳記。

- 「理論」跟「資料」已拆成兩個獨立、各自有主鍵的物件——`declarations` 表存理論規格，`datasets` 表存每次上傳（見下方 L5），`datasets.declaration_id` 把兩者串起來。跟原始設計唯一的差異是**沒有照搬 `lavaan` 的 `=~`/`~`/`~~` 語法**，改用跟系統其他端點一致的純 JSON（`measurement_model` / `structural_model`），理由是這個系統本來就是 JSON API，硬套 lavaan 的字串語法只會多一層解析成本，不會多帶來任何好處。
- 這個時間戳記就是 confirmatory／exploratory 的分界線：`GET /declare/{id}` 可查回原始宣告與時間點，`POST /optimize/path`、`POST /optimize/full-search`（L4 的搜尋端點）在審計紀錄裡固定被標記 `is_exploratory: true`，不管宣告內容寫什麼——這是寫死的規則，不是靠比對宣告內容跟搜尋路徑是否一致這種模糊判斷（那種語意比對留給研究者自己看，不是系統能可靠自動判斷的事）。

### L1 · 資料品質層 — ✅ 多訊號偵測 + Stage B 強制掛勾已完成（2026-07-22）

`detect_careless_responses()`（`app/stats_engine.py`）+ `POST /analyze/data-quality`：

- **訊號來源**：Mahalanobis 距離（多變量離群值，卡方分布門檻，預設 α=0.001）、IRV（列內作答標準差，偵測一路填同一個答案）、Long-string（連續相同答案的最長長度，門檻是題數的一定比例）、作答時間（**選配**——只有資料裡真的有時間欄位、且呼叫時指定 `time_column` 才會啟用；目前的問卷資料沒有這欄位，這個訊號預設不會生效）。注意力檢核題訊號**未實作**——這要問卷本身有設計檢核題才有意義，屬於問卷設計範疇（已在第八節 8.1 排除在系統範疇外），不是這層的資料就能算出來的。
- **多訊號收斂**：至少兩個獨立訊號同時亮起（`min_signals`，預設 2）才列入 `recommend_review`，單一訊號（哪怕是波動很大的 Mahalanobis 距離）不足以構成理由。
- **這一層只診斷、標記原因，不執行刪除**——`/analyze/data-quality` 是純診斷端點，回傳每筆樣本觸發了哪些訊號，刪不刪是下面 L4 或研究者的決定。
- **跟 L4 的強制掛勾**：`optimize_structural_path()` 新增 `allowed_drop_indices` 參數，`optimize_unified()` 預設（`require_data_quality_flag=True`）會先跑這層診斷，把結果限制傳進去——Stage B **現在只能刪除同時符合「Cook's Distance 高」跟「至少兩個 L1 訊號亮起」的樣本**，不能再單憑統計數字說了算。真實測試中驗證過：同一組刻意做出來的離群值，如果不符合 L1 標記，Stage B 會直接回報搜尋失敗（`max_drop: 0`），即使把關掉這個限制（`require_data_quality_flag=False`）它其實找得到能製造顯著性的刪法——這正是這一層存在的意義。

### L2 · 測量模型層 — ✅ 統計面已補齊（見第九節）

信度（α、CR）、收斂效度（Loading、AVE）、區辨效度——以 **HTMT** 為主要判準（比 Fornell-Larcker 更敏感）。**硬性關卡**：L3 必須等 L2 全數通過才能執行（這條關卡本身還沒程式碼強制，見下方 L4）。

- 沿用貪婪刪題項邏輯，但輸出是一個新版本的「純化後模型」，原始上傳資料永遠不被覆寫。
- 程式碼層面直接擋：L3 的端點會檢查目前模型版本是否已通過 L2，沒過就回錯誤，除非有明確登記理由的人工 override——**這條關卡尚未實作**，目前 `/analyze/structural` 跟 `/analyze/seminr` 都可以在沒過 L2 的情況下直接呼叫。
- ~~缺 HTMT、缺完整 EFA~~ 已解決：`/analyze/efa`（`r/efa_wrapper.R`，全題項多因子 EFA + Parallel Analysis）+ `/analyze/seminr`（`r/seminr_wrapper.R`，內含 HTMT）+ `/analyze/deleted-alpha`。詳見第九節。

### L3 · 結構模型層 — ✅ 大部分完成，SRMR 刻意暫緩（見第九節）

- **樣本內（in-sample）**：路徑係數 + Bootstrap CI/P 值、R²、**f² 效果量** — ✅ 已完成，`/analyze/seminr`。
- **樣本外（out-of-sample）**：**Q²predict / PLSpredict**（Shmueli et al. 2019 的現代作法，取代傳統 blindfolding）— ✅ 已完成，同一端點。
- **SRMR** 整體配適度 — ❌ 刻意不做：seminr 套件本身沒有內建 SRMR 函數，手刻公式沒有經過驗證，寧可先留白也不要生出一個沒人檢查過對不對的數字。之後若要補，必須先找到可以對答案的參照實作（例如拿真實資料同時餵 SmartPLS 比對）才能上。

### L4 · 情境式最佳化層 — 🟡 Stage A/B 分階段引擎已完成，情境化/L1 掛勾仍未做

Phase 2 已完成：`optimize_unified()`（`app/stats_engine.py`）+ `POST /optimize/full-search`，把原本「刪到顯著為止」的單一貪婪迴圈，換成第六節設計的分階段版本：

- **Stage A（強制關卡）**：跑 `optimize_measurement()`，任何構面卡在 AVE 救不起來（`⚠️ 無可救藥` / `❌ 計算錯誤`）就整個煞車，`status: "blocked_at_stage_a"`，Stage B 完全不執行——不是「執行了但結果不好」，是根本不跑。
- **Stage B（逐路徑獨立搜尋）**：Stage A 全過才開始。針對結構模型裡**每一條路徑分別**檢查：已顯著的路徑標記 `already_significant`、直接跳過搜尋（不會拿已經成立的東西去湊數字）；不顯著的路徑才呼叫 `optimize_structural_path()`（Cook's Distance 逐步刪樣本），成功／失敗各自獨立回報，**不會把多條路徑的刪除合併成同一個搜尋**——這是刻意的設計決定，見第六節末段「多路徑同時最佳化」暫緩的理由。
- **建構面整併只會是建議，不會自動執行**：Stage B 刪到上限仍不顯著，才會生成 `construct_review_suggestions`（文字建議，附帶「系統不會自動執行」的明確標註），交由研究者自己判斷。

測試（`TestOptimizeUnified`）過程中意外印證了一個值得記住的方法論風險：一條 p 值卡在邊界（如 p≈0.055）但**沒有真實效果**的路徑，偶爾真的會被 Cook's Distance 搜尋「救」到顯著——這不是 bug，是這個演算法設計上的固有風險。**這個防線缺口已在同一天（2026-07-22）補上**：見上方 L1，Stage B 現在強制要求樣本排除同時有 L1 標記的實質理由，不能只憑統計數字，`require_data_quality_flag` 預設就是開啟的。

**還沒做的部分**（原本 L4 設計的完整範圍）：
- 每次搜尋結果目前是單次回應，不是「不可變的情境記錄」，沒有版本化、沒辦法事後並排比較多個情境。
- 回應目前沒有固定帶 EXPLORATORY 標籤欄位，前端／使用這個端點的人要自己記得這件事——L1 訊號能不能百分之百排除誤判，仍然是統計推論不是保證，這個端點的輸出仍然應該當成 exploratory 看待，只是現在多了一層實質理由把關，不是毫無防備的純統計搜尋。

### L5 · 審計與版本層 — ✅ 已完成（2026-07-22）

不是流程裡的一個步驟，而是所有層共用的底層機制——概念上借用 DVC / MLflow 的 provenance 模式，實作上是 SQLite（`app/db.py`，理由見第八節 8.3 一貫的「先求堪用、之後真的要上正式環境再換 Postgres」原則）。

- **每個動作都是一筆不可變、有時間戳記、只能新增不能修改的紀錄**：`audit_log` 表，每次 `/upload`、`/analyze/structural`、`/optimize/measurement`、`/optimize/path`、`/optimize/full-search` 執行完都會寫入一筆，欄位包含完整的 request 參數跟 result 內容（不是摘要，是真的可以拿來重放的完整資料）。`app/db.py` 整個模組刻意**沒有寫任何 UPDATE 或 DELETE**，要修正只能新增一筆新紀錄，舊的永遠留著——這點有測試直接驗證（`test_no_update_or_delete_functions_exist`）。
- **以「研究 × 資料版本 × 分析執行」為主鍵**：`declarations`（研究/理論宣告，對應 L0）→ `datasets`（資料版本，用 SHA-256 內容雜湊避免同一份資料被誤判成不同版本，反之亦然）→ `audit_log`（分析執行，透過 `dataset_id`/`declaration_id` 外鍵串起前兩者）。`GET /audit/history`、`GET /audit/{id}` 可以查詢，且有做基本存取控制（只能看自己 user_id 底下的紀錄）。
- ~~現有系統的 `SESSION: Dict = {}` 是全域單一變數，兩個人同時用會互相覆蓋資料~~ 已解決：`app/session_store.py` 依 API token / `x-session-id` 分開存，多使用者不會再互相覆蓋——這部分維持原樣，是「目前 session 快照」用途，跟這裡的 `audit_log`（完整歷史）是兩個不同、互補的機制，不是同一件事的兩種寫法。

### L6 · 呈現層 — 現有系統完全沒有前端

前端純粹是 L0–L5 輸出的呈現：紅綠燈儀表板、情境比較檢視、可匯出成論文附錄的審計履歷。**已定案要有互動對話功能**（使用者可以針對分析結果提問），由 Python 服務負責串接 LLM API（見第八節 8.3 的技術棧決策），輸出必須固定帶「AI 生成之詮釋，請對照原始統計數字」的免責提示——畢竟 LLM 在論文情境下把 p 值講錯一次，後果比系統少做一個功能嚴重得多。

---

## 四、現況 vs 目標：差異總表

| 面向 | 現有系統 | 目標架構 | 對應層級 |
|---|---|---|---|
| 理論／假設 | 每次 API 呼叫才帶的臨時 dict，不存版本 | 獨立宣告、版控、加時間戳記 | L0 |
| 資料品質／異常樣本判定 | 只看 Cook's Distance 一個數字 | 多訊號收斂 + 對應實質理由 | L1 |
| 測量模型 | AVE / CR / α，缺 HTMT | + HTMT 區辨效度 | L2 |
| 結構模型指標 | R² / VIF / Bootstrapped P | + f² / Q² / PLSpredict / SRMR | L3 |
| 最佳化引擎 | 刪題項與刪樣本是兩支互不相干迴圈，且原設計把刪構面跟刪題項/樣本放同一輪貪婪比較（已判定不合理，見第六節） | 分階段關卡（L2 全過才進 L3）+ 情境並列、明確標 exploratory | L2 → L4 |
| 資料儲存 | 全域記憶體 dict，重啟即消失，多使用者互相覆蓋 | 版控資料庫，可重放審計，session 隔離 | L5 |
| 前端 | 無（純 API） | 紅綠燈儀表板、情境比較、審計履歷匯出 | L6 |
| AI 敘事 | 無 | LLM 顧問層，帶免責提示 | L6 |

---

## 五、統一開發路線圖

整合前後兩份文件的建議，依「急迫性 × 工程量 × 學術嚴謹度回報 × 相依順序」排出唯一一條開發順序。原則：**先堵住會在展示時出包的地基問題，再做低成本高回報的統計完整度，再做教授真正要的演算法核心，最後做需要前面地基穩定後才好蓋的重工程跟前端。**

### Phase 0 · 展示安全網（對應 L5 的最小版本）

**目標**：`POST /upload` 回傳 `session_id`（`uuid4`），`SESSION` 從單一 dict 改成 `Dict[str, Dict]`，之後每個請求帶 `session_id`。

**為什麼排第一**：這是唯一一個「不做就可能在教授/同學面前直接出包」的項目——目前部署版任何人同時打開網址都會互相覆蓋資料。工程量小（不需要真的資料庫，先用記憶體 dict 加 key 即可），效益立即。真正的持久化資料庫留到 Phase 4 一起做，避免現在做兩次。

### Phase 1 · 統計完整度（對應 L2 / L3 的缺口）

**目標**：`stats_engine.py` 新增 `calc_htmt()`（區辨效度）、`calc_f_squared()`、`calc_q_squared()`（blindfolding）、`calc_srmr()`，可選 PLSpredict。另外新增完整多因子 EFA + Parallel Analysis 因子保留判準、Deleted Alpha——這兩項按第八節 8.3 的技術棧決策改用 R 的 `psych` 套件實作（透過 plumber 服務暴露給 Python 呼叫），不在 Python 端重寫。

**為什麼排第二**：都是獨立的統計函式，不需要改動架構，也不依賴其他 Phase，可以現在就開始寫、獨立測試。學術嚴謹度回報最高——口試被問「你們有沒有做區辨效度／預測力檢定」、「構面分組怎麼驗證的」，現在答不出來，做完這步就能答。

### Phase 2 · 分階段最佳化引擎 MVP（對應 L2 → L4 的關卡設計）— ✅ 已完成（2026-07-22）

**目標**：實作 `optimize_unified()`：Stage A 沿用現有 `optimize_measurement()` 當強制關卡，Stage B 只用樣本刪除做結構顯著性搜尋（沿用 `optimize_structural_path()` 邏輯，逐步增加刪除份數、每步重跑 Bootstrapping），刪構面不進自動迴圈、只在 Stage B 刪到上限仍失敗時作為人工建議跳出。新端點 `POST /optimize/full-search`。已完成，詳見第四節 L4。7 個新測試（`TestOptimizeUnified` + 端點測試）涵蓋 Stage A 卡關、路徑已顯著跳過搜尋、搜尋成功、搜尋失敗產生建議四種情境，全部用真的算出來的數字驗證，不是只檢查回應格式。

**為什麼排第三**：這是教授反饋「先讓信效度過關，再看怎麼最少調整達到顯著」的直接對應，是整個系統最核心、最有 demo 價值的功能。先出一版 MVP（此時 L1 資料品質層還沒做，樣本排除理由暫時只有統計面），讓核心邏輯先能動、先能展示，Phase 3 再補實質理由這一塊，避免把兩件事綁在一起導致核心功能一直生不出來。

### Phase 3 · 資料品質層（對應 L1，回頭強化 Phase 2）— ✅ 已完成（2026-07-22）

**目標**：實作多訊號 careless-responding 偵測（Mahalanobis 距離、IRV、long-string、作答時間，視資料是否有時間戳記而定），至少兩訊號收斂才列入建議複查名單。修改 Phase 2 的 Stage B，要求每筆樣本排除都要能對應至少一個 L1 訊號，不能只憑 Cook's Distance。已完成，詳見第四節 L1。9 個新測試涵蓋訊號偵測本身（乾淨樣本不誤判、真的 straight-line 會被抓到、單一訊號不夠、時間欄位選配）跟 Stage B 掛勾（有正當理由才能刪、沒有就算統計上划算也不給刪），並且用同一組資料驗證了「關掉這層防護，原本會被擋下來的搜尋反而找得到路」——證明這道關卡真的有攔住東西，不是聊備一格。

**為什麼排第四**：這一步把 Phase 2 的 MVP 從「單純統計驅動」補強成「統計 + 實質理由雙重驗證」，是能不能在論文方法章節站得住腳的關鍵，但屬於「讓已存在的功能更嚴謹」而非「做出新功能」，所以排在核心功能之後。

### Phase 4 · 宣告層 + 完整版控儲存（對應 L0 + 完整版 L5）— ✅ 已完成（2026-07-22）

**目標**：從記憶體 dict 全面換成持久化資料庫；加入宣告式假設規格（L0，含時間戳記）；建立完整的不可變審計紀錄（每個操作可重放）。已完成，詳見第四節 L0、L5。資料庫選用 SQLite（`app/db.py`），`POST /declare` 建立宣告、`GET /audit/history`／`GET /audit/{id}` 查詢審計紀錄，`/upload`、`/analyze/structural`、`/optimize/measurement`、`/optimize/path`、`/optimize/full-search` 都已掛上稽核紀錄，L4 的兩個搜尋端點固定標記 `is_exploratory: true`。10 個新測試涵蓋宣告建立/查詢、資料集內容雜湊（同內容同雜湊、不同內容不同雜湊）、審計紀錄查詢與存取控制、confirmatory/exploratory 標記正確性，以及「模組裡沒有任何 UPDATE/DELETE 函數」這個不可變性的直接驗證。

**為什麼原本排第五（現在補記）**：工程量最大的一塊（要設計 schema、選資料庫、寫遷移），等 Phase 0–3 做完再動手，果然沒有重工——`audit_log` 表的欄位設計（`dataset_id`/`declaration_id`/`is_exploratory`）直接對應到 Phase 0-3 已經穩定下來的資料流，沒有中途改過 schema。

### Phase 5 · 呈現層（對應 L6）

**目標**：獨立 `frontend/`（建議先 Streamlit，求 demo 速度；要精緻再換 React），三個畫面：上傳 → 診斷儀表板（紅綠燈卡片）→ 最佳化模擬器（讀 Phase 2/3 的情境 audit trail，顯示步驟進度 + 一鍵套用）。選配 LLM 顧問層（`app/advisor.py`，固定帶 AI 生成內容免責提示）。

**為什麼排最後**：UI 應該蓋在穩定的 API 資料模型上；如果 API 回傳格式在 Phase 0–4 還會變動，現在做前端等於要重做。LLM 層同理——先讓演算法跟資料結構穩定，敘事包裝才不用跟著大改。

---

## 六、跨類型窮舉比較引擎：設計演進紀錄

這一節保留設計討論過程，讓之後回顧時知道「為什麼是這個設計，不是那個設計」。

**最初設想**：每一步同時模擬刪題項／刪構面／刪樣本三種動作，比較誰的邊際 P 值改善最大，貪婪選最好的。

**判定不合理，原因有三**：

1. 違反教授自己講的順序——「信效度要先過」是前提，不是跟顯著性搜尋平起平坐的候選動作。
2. 刪構面等於改變正在檢定的理論假設本身，需要文獻支持，不該讓演算法純粹用 ΔP 大小去自動決定。
3. 三種動作機制不同（測量誤差 vs. 模型規格改變 vs. 同模型換樣本數），用同一個 ΔP 當共同貨幣排序，統計上不是公平比較。

**修正後的架構（分階段，而非三選一貪婪迴圈）**，也就是本文 Phase 2／L2→L4 採用的版本：

- **Stage A（強制關卡，跟目標路徑無關）**：沿用現有 `optimize_measurement()`，把所有構面的 AVE / CR / α 全部弄過門檻，只動題項。必須完全通過才能進入 Stage B。
- **Stage B（結構顯著性搜尋，只在 Stage A 過關後才開始）**：只用樣本刪除去找顯著，因為這是唯一「模型不變、只是樣本數變小」的公平比較，對應教授「先假設刪除一份問卷…再看兩三個」的窮舉精神。
- **刪構面不進自動搜尋迴圈**：只有當 Stage B 刪到樣本上限仍不顯著時，系統才**建議**（不是自動執行）「可以考慮刪除／整併某個構面，但需要文獻支持」，交由研究者自己判斷。

另外曾考慮過「多路徑同時顯著最佳化」（一次讓 H1、H2 都顯著），評估後**暫緩**：機制上是把多條路徑的 Cook's Distance 合併排序，但同時對多個假設做事後樣本調整，researcher degrees of freedom 疊加得比單路徑更快，學術風險更高，且尚未設計好對應的實質理由驗證機制（依賴 Phase 3 / L1 完成後才適合重新評估）。

---

## 七、參考文獻

1. Hair, J. F., Hult, G. T. M., Ringle, C. M., & Sarstedt, M. *A Primer on Partial Least Squares Structural Equation Modeling (PLS-SEM)*. Sage Publications. — 兩階段評估法（測量模型→結構模型）。
2. Henseler, J., Ringle, C. M., & Sarstedt, M. (2015). A new criterion for assessing discriminant validity in variance-based structural equation modeling. — HTMT 判準與 0.85/0.90 門檻。
3. Curran, P. G. (2016). Methods for the detection of carelessly invalid responses in survey data. — 多訊號收斂偵測法。
4. Simmons, J. P., Nelson, L. D., & Simonsohn, U. (2011). False-Positive Psychology: Undisclosed Flexibility in Data Collection and Analysis Allows Presenting Anything as Significant. *Psychological Science*. — researcher degrees of freedom / p-hacking。
5. Center for Open Science — OSF Preregistration：confirmatory / exploratory 分界的標準做法。
6. Rosseel, Y. `lavaan`: An R Package for Structural Equation Modeling. — 宣告式模型語法（`=~` / `~` / `~~`）的設計靈感來源。
7. PLS-SEM 結構模型評估文獻（f² 效果量、Q² blindfolding、PLSpredict、SRMR）。
8. DVC / MLflow 官方文件 — provenance／experiment tracking 的架構模式，L5 設計靈感來源。

---

## 八、範疇確認與技術棧決策（2026-07-20 更新）

### 8.1 最終範疇：只做「問向 + 變數/題項」，不做「問卷排版與樣本」

問卷修正的標準實務依「由巨集到微觀」分三階段：

1. **問向（Dimensions）**——用 EFA 檢視構面結構是否合理，題目有沒有分錯群組。
2. **變數/題項（Variables/Items）**——信度、收斂/區辨效度、共線性、題項刪除。
3. **問卷排版與樣本**——Skip Logic、題目呈現順序、樣本代表性篩選機制。

**已定案：本系統只做第 1、2 階段，第 3 階段明確排除在範疇外。** 第 3 階段本質是「問卷設計與發放工具」，需要看到問卷本身的呈現介面與發放/蒐集流程；本系統的定位是「分析已回收的問卷資料」，兩者是完全不同的產品類型，勉強塞入只會做出功能陽春、定位模糊的四不像，不如明確排除。

### 8.2 Stage 1（問向）現況缺口：完整多因子 EFA

現有 `calc_loadings_ave_cr()`（[app/stats_engine.py:70](app/stats_engine.py#L70)）做的是**單構面單因子 EFA**——前提是構面分組已經正確，只用來算該構面內部的 loading/AVE/CR。這不是 Stage 1 要的「全部題項一次跑、驗證分組本身合不合理」。這是目前唯一真正對應 Stage 1 的功能缺口，補法見下方 8.3、8.4。

### 8.3 技術棧決策：Python 主體 + R 統計核心（混合式架構）

最終決定採**混合式架構**，不是純 Python 也不是純 R：

```
使用者
  │ HTTPS
  ▼
Python / FastAPI（對外唯一入口）
  - 檔案上傳、L0/L1/L4/L5/L6 的協調邏輯
  - 互動對話功能（呼叫 LLM API，串流回應、session 管理）
  - Tier 1 / Tier 2 優化迴圈的「控制流程」
  │ 內部網路 HTTP（不對外開放，只有 Python 服務打得到）
  ▼
R / plumber（純統計運算服務，不處理對話、不對外公開）
  - seminr → Cronbach α / AVE / CR / cross-loadings / bootstrap / VIF
  - psych  → 完整多因子 EFA + Parallel Analysis 因子保留判準（補 8.2 缺口）
```

**決策理由：**
- 系統確定要有互動對話功能，這需要 LLM API 串接、串流回應、session 管理——這些 Python/FastAPI 生態圈遠比 R 成熟，所以 Python 必須是對外的主體服務。
- 統計核心的正確性/可信度，R 的 `seminr`（SmartPLS 原班團隊開發，結果跟業界標準工具最接近）跟 `psych`（EFA + Parallel Analysis 的學術標準實作）明顯比 Python 對應套件更被廣泛驗證，尤其是 8.2 的完整 EFA 缺口，`psych::fa.parallel()` 是這塊公認最成熟的實作。
- 兩服務用內部 HTTP 溝通，不用 rpy2 把 R 直接嵌進 Python process——避免版本相容性問題、資料型別轉換問題，以及跨語言 debug 困難。R 服務不對外公開，只有 Python 服務打得到，兩者可以獨立部署、獨立除錯。
- 代價：Tier 1/Tier 2 優化迴圈每輪迭代都要重新呼叫 R 服務算一次指標，屬於高頻呼叫，R 服務需要保持常駐熱機、跟 Python 服務部署在同一個內部網路，避免延遲拖垮使用者體驗。

> **實作落差（2026-07-22 補記）**：實際做出來的不是「R 包成 plumber 常駐服務、走內部 HTTP」，而是 `app/r_bridge.py` 每次請求用 `subprocess.run(["Rscript", ...])` 直接跑一支 R 腳本、用暫存檔案交換資料，跑完就結束——R 沒有常駐，每次呼叫都重新啟動一次直譯器。比原計畫簡單、部署也不用管兩個服務怎麼串，但代價是上面講的「高頻呼叫」場景（Tier 1/2 優化迴圈）會被 R 重啟成本拖慢。目前只有單次呼叫 EFA/seminr，還沒接優化迴圈，暫時沒事；等 Phase 2 要把優化迴圈接上 R 的時候，需要重新評估要不要換成常駐服務。

### 8.4 待補功能清單（併入 Phase 1，見第五節）

- ~~完整多因子 EFA + Parallel Analysis（R `psych`）~~ ✅ 已完成，見第九節
- ~~Deleted Alpha~~ ✅ 已完成，`calc_deleted_alpha` / `/analyze/deleted-alpha`
- Composite Score 從簡單平均升級為 `seminr` 的正統 PLS 加權組合分數——🟡 部分完成：`calc_composite_score(weighting="loading")` 現在用 Python 自己單因子 EFA 的 loading 加權，比簡單平均進步，但仍不是呼叫 `seminr` 算出的真正 PLS outer weight（`results$composite_scores`，R 端已確認可以拿到，只是還沒接進 Python 這邊）——留給下次處理

---

## 九、Phase 1 統計框架完成紀錄（2026-07-22 更新）

### 9.1 這次做了什麼

把 `r/seminr_wrapper.R`（`/analyze/seminr` 端點）補完成含 HTMT、f²、Q²predict/PLSpredict 的完整版，同時發現並修掉一個**先前就已經存在、還沒被發現的嚴重 bug**：這個端點從被寫出來開始就沒有真正成功執行過一次。

### 9.2 發現的 bug：seminr 套件 API 版本落差

容器裡裝的是 `seminr 2.5.0`（R 4.5.0），舊版 wrapper 用的寫法在這個版本完全不存在：

| 舊 wrapper 用的寫法 | 這個版本實際要用的寫法 |
|---|---|
| `items(item_vector)` | 直接傳向量給 `composite(name, item_vector)`，不用包一層 |
| `pls_model(mm, sm)` + `pls(data, model)` | `estimate_pls(data, measurement_model, structural_model)` |
| `boot(results, R = 200)` | `bootstrap_model(seminr_model, nboot = 500)` |
| `do.call(paths, path_list)` 直接當結構模型用 | `paths()` 只回傳單組 from/to 關係（純字元向量，沒有維度），要用 `relationships(paths(...), paths(...), ...)` 包起來才是 `estimate_pls()` 吃得下的結構模型物件——這個沒包對，會在 `estimate_pls()` 內部丟出語意完全不相關的錯誤（`incorrect number of dimensions`），很難從錯誤訊息猜到根因 |

**後果**：R 腳本內部的 `tryCatch` 會抓到這些錯誤、寫進 `error` 欄位，`app/r_bridge.py` 的 `run_seminr()` 讀到非空的 `error` 欄位就會拋 `RBridgeError`，端點回 500——但因為錯誤訊息（例如 `could not find function "items"`）沒有明顯指向「API 版本不對」，加上端點本身有測試覆蓋卻斷言寫得很鬆（原本只檢查回傳的 dict 裡有沒有 `"success"`、`"loadings"`、`"suggested_new_items"` 三個 key 的其中一個，而 `{"success": True, ...}` 這個 shape 太容易「巧合」符合），這個 bug 才一直沒被抓出來。

**教訓**：這是這個 session 第二次遇到「表面上測試綠燈/回應 200，但底層邏輯其實沒有真正跑起來」的狀況（第一次是 `/analyze/llm-suggestions` 那三個疊在一起的 bug）。**針對外部套件/API 呼叫寫測試時，斷言要驗證實際數值是否合理（例如 p-value 跟 t-stat 要邏輯一致、HTMT 要在 0~2 之間），不能只檢查「有沒有回傳某個 key」**——後者對「呼叫失敗但格式恰好符合」這種狀況完全沒有防禦力。

### 9.3 現在 `/analyze/seminr` 回傳的完整欄位

```
{
  measurement_loadings, reliability,           # 原本就有
  validity: { htmt },                          # 新增
  f_squared,                                    # 新增
  paths: { beta, t_stat, p_value, ci_2_5, ci_97_5, ... },  # p_value 欄位原本對到錯的 R 欄位，已修正
  r_squared: { construct: { r_squared, adj_r_squared } },  # 原本是空的，已修正
  vif,
  predictive: { item: { q2predict, rmse_pls, rmse_lm_benchmark, beats_lm_benchmark } }  # 新增
}
```

`predictive` 只會出現結構模型裡「內生（依變數）構面」的題項——外生構面沒有任何路徑指向它，本來就沒有「被預測」這件事，這是 PLSpredict 方法論本身的限制，不是漏算。

### 9.4 SRMR 仍未實作（刻意決定，不是漏掉）

`seminr` 套件沒有內建 SRMR 函數。手刻公式在技術上做得到，但沒有驗證過的公式產出一個「看起來正常但可能是錯的」配適度指標，比直接不做的風險更高——尤其這個系統沒有直接競品可以拿來對答案（見先前討論），任何新增的數字都只能靠自己驗證。**先不做，等有機會拿真實資料同時跑 SmartPLS 或另一個獨立實作對照過，確認公式對得上，才要加回來。**

### 9.5 測試強化

`tests/test_r_endpoints.py::TestSeminrEndpoint::test_seminr_endpoint` 從單一鬆散斷言改成逐項檢查：AVE/CR 落在 0~1、HTMT 落在合理範圍、p-value 與 t-stat 邏輯一致（t > 2.6 時 p < 0.05）、R² 落在 0~1、predictive 欄位只涵蓋內生構面題項且 RMSE > 0。50/50 全域測試（含這批）在乾淨環境下通過。
