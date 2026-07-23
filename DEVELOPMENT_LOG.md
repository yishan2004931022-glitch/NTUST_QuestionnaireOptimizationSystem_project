# 開發歷程紀錄（自留）

> 從最早期到現在，依時間順序記錄遇到的技術問題、為什麼會發生、怎麼解決的。給自己/組員以後回顧用，不是給老師看的版本（那份是 `PROFESSOR_REPORT.md`）。
>
> 最後更新：2026-07-23

---

## 階段 0：起點——純 Python 架構

最早的系統就是 FastAPI + Python 統計套件（`pandas`/`numpy`/`statsmodels`/`pingouin`/`factor_analyzer`），沒有 R、沒有資料庫（`SESSION: Dict = {}` 全域變數）、沒有前端。部署討論了 Render 免費方案、Vercel、Netlify——後來發現 Vercel/Netlify 這類平台本質是給前端/serverless 用的，硬要跑一個 Docker 化的常駐後端服務是邊緣案例，不建議用，改回 Docker Compose 自架。

**當時順便修過的東西**：
- `.gitignore` 沒有排除 `data/`（執行期 session 暫存資料，含 tokens），補上。
- GitHub Collaborator 邀請卡在 Pending 狀態沒接受，確認後解決。
- 本機沒裝 GitHub CLI，手動下載 zip 版免安裝到 `%LOCALAPPDATA%\GitHubCLI`，加進 PATH。

## 階段 1：R vs Python 的統計核心評估

問題：Python 生態圈沒有被廣泛驗證過的 PLS-SEM 實作，尤其是完整多因子 EFA + Parallel Analysis 這塊，Python 端明顯弱於 R 的 `psych`。R 的 `seminr` 是 SmartPLS 原班團隊做的，結果跟業界標準工具最接近。

但系統的完整願景包含「使用者可以互動對話」，這需要 LLM API 串接、串流回應、session 管理——R 的網頁服務生態（`plumber`）遠不如 Python 的 FastAPI 成熟。

**決策**：混合式架構。Python 當對外主服務（上傳、對話、流程控制），R 當內部統計運算核心，兩者間溝通方式討論過三個選項：
1. rpy2（R 嵌進 Python process）——否決，版本相容性/型別轉換/跨語言 debug 都是坑
2. R 包成 plumber 常駐服務，走內部 HTTP——原計畫
3. R 用 subprocess 每次呼叫重啟——最後**實際做出來的是這個**，比原計畫簡單，代價是高頻呼叫場景會被 R 重啟成本拖慢（目前呼叫頻率還沒到會感知到的程度，先不處理，見階段 8 的重新評估）

## 階段 2：願景釐清 + 文獻調查

**問卷修正的標準流程**（老師的建議）：問向（EFA）→ 變數/題項（信效度）→ 問卷排版與樣本，由巨集到微觀。討論後**確認排除第三階段**（問卷排版/樣本代表性），因為那本質是問卷設計與發放工具，跟「分析已回收資料」的系統定位不同，硬塞會做出四不像。

**文獻檢索**（確認這個系統的自動化優化角度是不是已經有人做過）：
- SmartPLS/JASP/R 套件都停在「算出指標、標示合不合格」，決定怎麼調整靠人工——這塊確實是空的
- 找到 2024-2026 的相關文獻：AGAS-PLS（基因演算法自動搜尋結構模型設定）、Bee Swarm Optimization 做 SEM specification search——方向類似但聚焦結構模型全域搜尋，跟本系統鎖定測量模型信效度修復+可解釋啟發式演算法的方向不同，可以當論文的相關研究/研究缺口論述

## 階段 3：LLM 建議功能——三個疊加的 bug

用戶回報 `/analyze/llm-suggestions` 疑似沒有真的呼叫到 LLM。實測後發現**三個 bug 疊在一起**，導致這個端點從寫出來開始就沒有成功呼叫過 LLM，但因為有本地備援機制（fallback 到規則式建議），表面上看起來一直正常：

1. **缺 `base_url` 支援**：`.env` 設定的是 Groq 的金鑰（`gsk_...` 開頭）+ Llama 模型，但 `_call_llm()` 的 `openai` 分支寫死呼叫官方 OpenAI SDK，沒有 `base_url` 參數，金鑰被送去打 `api.openai.com` 而不是 Groq 的伺服器。
2. **`x-api-key` header 誤用**：這個 header 同時被拿來做「我們自己 API 的存取驗證」跟「LLM 供應商金鑰的備援來源」兩件不相關的事。呼叫方為了通過自己 API 的驗證帶的 `x-api-key`，被誤當成要送給 Groq 的金鑰，蓋掉了真正的 `LLM_API_KEY` 環境變數。
3. **忘記 `json.loads()`**：就算前兩個都修好，LLM 真的回傳了 JSON 字串，程式碼也沒解析它，直接把原始字串丟進驗證函數逐字元檢查，永遠得到空清單，又默默退回本地備援。

**修法**：加 `LLM_BASE_URL` 環境變數 + 傳給 OpenAI SDK 的 `base_url` 參數；`api_key` 的解析拿掉 `_get_api_key(request)` 這個來源，只用 `body.api_key` 或環境變數；`_call_llm` 的兩個 provider 分支都補上 `json.loads()`。修完後用真實 Groq API 驗證，回傳內容裡出現真正 LLM 生成的 `suggested_rewrite` 文字，確認整條路徑真的通了。

**這裡也順便發現**：Docker Compose 用 volume mount 掛整個專案目錄進容器，所以 R 腳本改了不用重建容器（下次呼叫就是新版）；但 Python 程式碼改了要 `docker compose restart`，因為 uvicorn process 是常駐的，不會自動重新載入模組。

## 階段 4：Git 大掃除

累積了一大堆未提交的變更（R bridge、session isolation、admin-web 前端等），混雜已 staged 跟未 staged 的東西。分三批 commit：
1. `.gitignore` 擴充（含 `data/` 排除）
2. Docker Compose / Vercel / Netlify 部署設定
3. Session 隔離 + R bridge + admin-web + 這次的 LLM 三個 bug 修正

**教訓**：`git commit` 會把整個 staging area 一起送出，不是只送剛才 `git add` 的東西——第一批 commit 因為這樣不小心把別的已 staged 檔案一起帶進去了，訊息跟內容對不太上，之後每次 commit 前都先 `git status`/`git diff --stat` 確認 staging area 內容再送出。

## 階段 5：Phase 1——seminr wrapper 整個沒真的成功執行過

要幫「統一優化引擎」做完整統計指標補完（HTMT、f²、Q²predict）時，發現 `r/seminr_wrapper.R` 用的是舊版 seminr API，容器裡裝的是 `seminr 2.5.0`，完全不相容：

| 舊寫法 | 2.5.0 版要用的寫法 |
|---|---|
| `items(item_vector)` | 直接傳向量給 `composite(name, item_vector)` |
| `pls_model(mm, sm)` + `pls(data, model)` | `estimate_pls(data, measurement_model, structural_model)` |
| `boot(results, R=200)` | `bootstrap_model(seminr_model, nboot=500)` |
| `do.call(paths, path_list)` 直接當結構模型 | `paths()` 只回傳單組關係（無維度的字元向量），要用 `relationships(paths(...), paths(...))` 包起來 |

錯誤發生在 `estimate_pls()` 內部，訊息是完全不相關的 `incorrect number of dimensions`，很難從錯誤訊息猜到根因——是直接進容器跑 R 互動式除錯（`Rscript -e '...'`）、對照 `tools::Rd_db("seminr")` 查最新文件才找到的。

**這個 bug 也是測試斷言太鬆造成的**：舊測試只檢查回傳的 dict 有沒有 `success`/`loadings`/`suggested_new_items` 三個 key 其中一個，`{"success": True, ...}` 這個 shape 太容易巧合符合。修完後把測試改成**驗證實際數值**（p-value 跟 t-stat 邏輯要一致、HTMT 要落在 0~2、AVE/CR 要落在 0~1），這樣「呼叫失敗但格式恰好符合」的狀況才會被抓到。

修完後補上 HTMT、f²、Q²predict/PLSpredict（Shmueli et al. 2019 的現代作法，取代傳統 blindfolding），R² 欄位原本是空的也修正。**SRMR 評估後決定不做**：seminr 沒有內建函數，手刻公式沒有驗證管道，寧可留白。後來再重新確認過一次「SRMR 是不是必要」，結論是 PLS-SEM 本來就沒有 CB-SEM 那套強制配適度指標的傳統，非必要指標，決策維持不變。

## 階段 6：Phase 2——統一優化引擎，跑測試時意外驗證出一個真實風險

把原本兩支互不相干的優化迴圈（Tier 1 刪題項、Tier 2 刪樣本）換成分階段版本：Stage A（測量模型強制關卡）→ Stage B（逐路徑獨立搜尋，不合併多路徑）。

**校準測試資料時的意外發現**：為了測「Stage B 搜尋成功」這個情境，刻意做了一組「弱效果 + 幾個離群值壓低顯著性」的合成資料。結果發現：**一條 p 值卡在邊界（p≈0.055）但沒有真實效果的路徑，真的會被 Cook's Distance 搜尋「救」到顯著**。這不是測試寫錯，是這個演算法設計上的固有風險——純統計驅動的搜尋，本來就可能把雜訊搜尋成看似顯著的結果。這個發現直接變成階段 7（L1 資料品質層）要解決的問題的理由。

## 階段 7：Phase 3——L1 資料品質層，驗證關卡真的擋得住

實作多訊號 careless-responding 偵測（Mahalanobis 距離、IRV、Long-string），至少兩訊號收斂才標記「建議複查」。掛勾進 Stage B：`optimize_structural_path()` 加 `allowed_drop_indices` 參數，限制只能刪除同時符合「Cook's Distance 高」+「L1 標記」的樣本。

**驗證方式**：用階段 6 校準好的同一組「假顯著」合成資料重測，加上限制後**搜尋正確失敗**（`max_drop: 0`，因為離群值只觸發 1 個訊號，不到 2 個門檻）；接著另外做一組「真的隨意作答」樣本（straight-line，同時觸發 Mahalanobis + Long-string 兩個訊號），確認這種**合法**的情況搜尋能成功——證明這道關卡不是聊備一格，是真的有攔住東西，也真的放得過合理的案例。

## 階段 8：Phase 4——L0 宣告 + L5 審計，資料庫選型

決定要做完整版（L0 宣告層 + 不可變審計紀錄），不是只換資料庫這麼簡單。**資料庫選 SQLite**，不是 Postgres：理由跟部署選型一路以來的原則一致（先求堪用、簡單，之後真的要上正式環境再換，遷移只是換連線字串不用重寫邏輯）。

`app/db.py` 三張表：`declarations`（L0 宣告，含時間戳記）、`datasets`（每次上傳，SHA-256 內容雜湊避免版本混淆）、`audit_log`（不可變操作紀錄）。**整個模組刻意不寫任何 UPDATE/DELETE**，要修正只能新增一筆新紀錄，這點直接寫了測試驗證（檢查模組匯出的函數名稱裡沒有 update/delete 字樣）。

`/optimize/path`、`/optimize/full-search` 這兩個 L4 搜尋端點的審計紀錄固定標記 `is_exploratory: true`，**用寫死規則而非語意比對**（比對宣告內容跟實際搜尋路徑是否一致太模糊，系統沒辦法可靠自動判斷，這件事留給研究者自己看）。

## 階段 9：缺口清理輪

Phase 0-4 做完後盤點四個已知小缺口，逐一處理：

1. **L2 硬性關卡**：原本只有內部的 `optimize_unified()` 有 Stage A 關卡，`/analyze/structural`、`/analyze/seminr`、`/optimize/path` 這幾個獨立端點都還沒被擋。加了 `_enforce_l2_gate()`，沒過回 403，override 要帶理由且進審計紀錄。
2. **Composite Score 真實 PLS 權重**：原本 `weighting="loading"` 是 Python 自己單因子 EFA 加權的近似版本，不是真正的 PLS 迭代權重。新增 `weighting="pls"`，呼叫 `run_seminr()` 拿 `results$construct_scores`（R 端這個欄位原本沒有輸出，順便補上）。**注意事項**：`estimate_pls()` 內部會先標準化資料，`pls` 模式回傳的分數均值落在 0 附近，跟另外兩個模式的原始量尺不能直接比較，回應裡加了 `"scale": "standardized"` 避免誤讀。
3. **SRMR**：重新評估「這個指標到底重不重要」，結論維持不做（見階段 5 的說明），但這次是主動重新確認過，不是單純沿用舊決定。
4. **R subprocess 效能**：重新定性為「這不是小缺口，是整個架構搬遷（換成常駐服務），工程量跟 Phase 2-4 一個量級」，而且目前沒有實際變慢的證據，維持現狀，不做投機性優化。

## 階段 10：全端點審計覆蓋率稽核

跑一輪端到端 pipeline 手動驗證（宣告→上傳→L1→L2→L3→L4→審計查詢）時，**發現 `/analyze/seminr` 雖然有掛 L2 關卡，卻沒有寫審計紀錄**，跟姊妹端點 `/analyze/structural` 不對稱——回頭查是 Phase 4 一開始就只挑了五個端點（`/upload`、`/analyze/structural`、`/optimize/measurement`、`/optimize/path`、`/optimize/full-search`）掛審計，`/analyze/seminr` 沒被排進去，這次加 L2 關卡時也沒順便補。

決定把**全部 13 個分析端點**都補上審計紀錄（`/analyze/measurement`、`/analyze/llm-suggestions`、`/analyze/data-quality`、`/analyze/full`、`/analyze/efa`、`/analyze/deleted-alpha`、`/analyze/seminr`、`/analyze/composite` 是這次新補的），順便發現 `/analyze/full` 也缺 L2 關卡（它跟 `/analyze/structural` 一樣會跑結構分析），一併補上。

新增了一個「跑一輪所有端點、檢查審計紀錄裡的 action 集合完整」的測試（`test_all_non_r_analysis_endpoints_are_audited`），這種測試形狀才會真的抓到這類「少掛一個端點」的疏漏，不是靠每次人工記得檢查。

## 階段 11：Phase 5a——Streamlit 前端 + 瀏覽器實測

新增 `streamlit_app/`，六個頁面對應 L0-L5，docker-compose 多開一個 `frontend` 服務，走 Docker 內部網路連後端（`BACKEND_URL=http://api:8000`）。每個瀏覽器分頁在 `st.session_state` 生成隨機 `x-session-id`，機制跟 API 呼叫者的 session 隔離模型一致。

**自己驗證的部分**（沒有瀏覽器工具，只能做到這裡）：容器建置成功、Streamlit 健康檢查通過、頁面檔案語法正確、容器間網路連通。**沒辦法驗證**實際畫面渲染跟互動邏輯，請使用者自己拿瀏覽器走一遍。

**使用者實測結果**：
- 首頁把 `BACKEND_URL`（`http://api:8000`，Docker 內部網路位址）用 `st.metric` 顯示成看起來可點擊的連結，瀏覽器點了打不開（`api` 這個主機名稱只有容器網路內部能解析，使用者的瀏覽器在網路外面）。改用 `st.code` 純文字顯示 + 加註說明修掉。
- **拿真實問卷資料（185 筆）把六個頁面完整走一輪，結果符合預期**：測量模型 10/10 構面過關；結構模型（TE/OE/EC → ATT → BI）裡 TE→ATT、EC→ATT、ATT→BI 顯著，OE→ATT 不顯著；優化模擬器對 OE→ATT 搜尋，因為只有 14/185 筆樣本達到 L1 複查門檻、搜尋範圍內找不到符合實質理由的刪法，**正確回報搜尋失敗**，附上人工複核建議而非硬湊顯著性。這是階段 6、7 那兩個安全機制在真實資料上的具體驗證，不是合成資料的巧合。

---

## 目前為止踩過的坑，整理成一句話清單（給以後的自己）

- Docker Compose volume mount：R 腳本改了自動生效，Python 程式碼改了要 `docker compose restart`。
- `git commit` 會送出整個 staging area，不是只送剛 `git add` 的東西，commit 前一定要 `git status` 確認。
- 測試斷言只檢查「有沒有回傳某個 key」防禦力等於零，外部套件/API 呼叫的測試要驗證實際數值合理性。
- R 套件版本一改，教學文章上的語法可能整批失效，官方 `tools::Rd_db()` 查目前版本文件比相信網路教學可靠。
- 同一個 HTTP header 不要身兼兩種不相關用途（`x-api-key` 那次的教訓）。
- 統計指標「能不能做」跟「該不該做」是兩回事，沒有驗證管道的公式，寧可不做（SRMR）。
- 安全機制有沒有用，要用「關掉它、看原本擋得住的東西是不是就通過了」來驗證，不能只驗證「它存在」。
