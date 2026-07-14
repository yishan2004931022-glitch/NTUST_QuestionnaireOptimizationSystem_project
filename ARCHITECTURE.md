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

### L0 · 研究設計宣告層 — 現有系統完全沒有

上傳資料之前，研究者先用宣告式語法定義構面、題項、假設路徑（借用 `lavaan` 的 `=~` 測量 / `~` 結構 / `~~` 共變 語法概念），存成獨立於資料的「理論規格」，並蓋上時間戳記。

- 把「理論」跟「資料」拆成兩個獨立、各自版控的物件——現有系統把 `structural_model` 當成每次 API 呼叫才帶的臨時參數，理論本身沒有被存下來、追蹤版本。
- 這個時間戳記就是 confirmatory／exploratory 的分界線：宣告之後才做的任何「目標路徑搜尋」都自動歸類為事後分析。

### L1 · 資料品質層 — 現有系統完全沒有

上傳資料先對照 L0 的規格做結構驗證，再平行跑多個獨立的低品質作答訊號——不是只有一個。

- 訊號來源：注意力檢核題（如果問卷裡有設計）、長串重複作答、IRV、作答時間、Mahalanobis 距離。
- 採「多訊號收斂」而非單一指標：至少兩個獨立訊號同時亮起，才列入「建議複查」名單。
- 這一層只**診斷、標記原因**，不執行刪除——刪不刪是研究者或 L4 的決定，不是這一層的權責。

### L2 · 測量模型層 — 現有系統已有雛形（`optimize_measurement`），缺 HTMT

信度（α、CR）、收斂效度（Loading、AVE）、區辨效度——以 **HTMT** 為主要判準（比 Fornell-Larcker 更敏感），嚴格門檻 0.85 / 寬鬆 0.90。**硬性關卡**：L3 必須等 L2 全數通過才能執行。

- 沿用貪婪刪題項邏輯，但輸出是一個新版本的「純化後模型」，原始上傳資料永遠不被覆寫。
- 程式碼層面直接擋：L3 的端點會檢查目前模型版本是否已通過 L2，沒過就回錯誤，除非有明確登記理由的人工 override。

### L3 · 結構模型層 — 現有系統只做了一半

現有系統只有 R²、VIF、Bootstrapped P 值。完整的 PLS-SEM 結構模型評估還需要樣本內跟樣本外兩種指標：

- **樣本內（in-sample）**：路徑係數 + Bootstrap CI、R²、**f² 效果量**。
- **樣本外（out-of-sample）**：**Q²（blindfolding 預測相關性）**、**PLSpredict**、**SRMR** 整體配適度。
- 少了樣本外指標，模型可能只是「解釋力好看」但預測力差——這在現有系統跟教授原本的構想裡都沒被提到，是完整性上最大的缺口。

### L4 · 情境式最佳化層 — 對應「跨類型窮舉比較引擎」，需要重新設計

這是原本「刪到顯著為止」邏輯所在的位置，但重新設計成產生一組組可比較的「情境（scenario）」，而不是直接覆蓋資料。詳細設計演進見[第六節](#六跨類型窮舉比較引擎設計演進紀錄)。

- 每個情境 = 一筆不可變的差異記錄（排除了哪些題項/樣本 + 對應到 L1 的實質理由）+ 該情境下的完整 L2/L3 結果，多個情境可以並排比較。
- 強制規則：任何樣本排除都要能對應到 L1 標記的至少一個實質理由，不能只憑「Cook's Distance 比較高」這種純統計理由。
- 每份情境報告固定帶著「**EXPLORATORY · 事後分析，非驗證性結果**」標籤，跟 L0 宣告的 confirmatory 假設視覺上明確分開。

### L5 · 審計與版本層 — 現有系統是全域記憶體 dict，等同於沒有

不是流程裡的一個步驟，而是所有層共用的底層機制——概念上借用 DVC / MLflow 的 provenance 模式。

- 每個動作（上傳、L2 純化、L4 情境）都是一筆不可變、有時間戳記、只能新增不能修改的紀錄。
- 用真正的資料庫（不是現有系統的全域記憶體 dict）保存，以「研究 × 資料版本 × 分析執行」為主鍵，讓口試委員可以完整重放從原始資料到最終數字的每一步。
- 現有系統的 `SESSION: Dict = {}` 是**全域單一變數**，不只沒有版控，連多使用者隔離都沒有——兩個人同時用部署好的網址會互相覆蓋資料，這是目前最迫切的問題（見第五節 Phase 0）。

### L6 · 呈現層 — 現有系統完全沒有前端

前端純粹是 L0–L5 輸出的呈現：紅綠燈儀表板、情境比較檢視、可匯出成論文附錄的審計履歷。LLM 敘事若加入，輸出必須固定帶「AI 生成之詮釋，請對照原始統計數字」的免責提示——畢竟 LLM 在論文情境下把 p 值講錯一次，後果比系統少做一個功能嚴重得多。

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

**目標**：`stats_engine.py` 新增 `calc_htmt()`（區辨效度）、`calc_f_squared()`、`calc_q_squared()`（blindfolding）、`calc_srmr()`，可選 PLSpredict。

**為什麼排第二**：都是獨立的統計函式，不需要改動架構，也不依賴其他 Phase，可以現在就開始寫、獨立測試。學術嚴謹度回報最高——口試被問「你們有沒有做區辨效度／預測力檢定」，現在答不出來，做完這步就能答。

### Phase 2 · 分階段最佳化引擎 MVP（對應 L2 → L4 的關卡設計）

**目標**：實作 `optimize_unified()`：Stage A 沿用現有 `optimize_measurement()` 當強制關卡，Stage B 只用樣本刪除做結構顯著性搜尋（沿用 `optimize_structural_path()` 邏輯，逐步增加刪除份數、每步重跑 Bootstrapping），刪構面不進自動迴圈、只在 Stage B 刪到上限仍失敗時作為人工建議跳出。新端點 `POST /optimize/full-search`。

**為什麼排第三**：這是教授反饋「先讓信效度過關，再看怎麼最少調整達到顯著」的直接對應，是整個系統最核心、最有 demo 價值的功能。先出一版 MVP（此時 L1 資料品質層還沒做，樣本排除理由暫時只有統計面），讓核心邏輯先能動、先能展示，Phase 3 再補實質理由這一塊，避免把兩件事綁在一起導致核心功能一直生不出來。

### Phase 3 · 資料品質層（對應 L1，回頭強化 Phase 2）

**目標**：實作多訊號 careless-responding 偵測（Mahalanobis 距離、IRV、long-string、作答時間，視資料是否有時間戳記而定），至少兩訊號收斂才列入建議複查名單。修改 Phase 2 的 Stage B，要求每筆樣本排除都要能對應至少一個 L1 訊號，不能只憑 Cook's Distance。

**為什麼排第四**：這一步把 Phase 2 的 MVP 從「單純統計驅動」補強成「統計 + 實質理由雙重驗證」，是能不能在論文方法章節站得住腳的關鍵，但屬於「讓已存在的功能更嚴謹」而非「做出新功能」，所以排在核心功能之後。

### Phase 4 · 宣告層 + 完整版控儲存（對應 L0 + 完整版 L5）

**目標**：從記憶體 dict 全面換成持久化資料庫；加入宣告式假設規格（L0，含時間戳記）；建立完整的不可變審計紀錄（每個操作可重放）。

**為什麼排第五**：工程量最大的一塊（要設計 schema、選資料庫、寫遷移），最好等 Phase 0–3 把「系統實際需要存哪些物件」摸清楚以後再一次做對，不然現在做會因為前面需求還沒定型而重工。

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
