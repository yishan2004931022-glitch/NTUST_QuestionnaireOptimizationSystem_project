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

## 階段 12：宣告回填 + 情境並列比較頁面

使用者實測後問了兩個問題：（1）不宣告能不能直接上傳分析——查程式碼確認 `declaration_id` 從頭到尾只有被讀取，從來沒有任何地方強制檢查，**技術上完全不需要宣告**，宣告純粹是方法論加分項（讓 L4 搜尋動作的 `is_exploratory` 標記有一個時間戳記可以對照）；（2）能不能實作「情境並列比較」跟「多輪對話」——決定先做前者。

**宣告頁面小改良**：原本測量模型要手動全部重打，改成如果 `st.session_state` 裡已經有上傳後自動偵測到的 `construct_dict`，就用它回填文字框，使用者只要補結構模型那幾行就好。這個改動不影響宣告的方法論意義——重點是宣告時間戳記要早於 L4 搜尋動作，不是早於上傳，所以上傳後才宣告完全合理。

**情境並列比較**：資料其實已經全部存在 L5 審計紀錄裡（`optimize_full_search` 每次都存完整 request/result），不需要新的持久化層。做法：
1. `/optimize/full-search` 加一個選填的 `label` 欄位，寫進 `request_params` 方便之後辨識；回應也加 `audit_entry_id`，前端知道這次搜尋對應哪一筆紀錄。
2. 新頁面 `7_情境比較.py`：查 `/audit/history`，篩出 `action == "optimize_full_search"` 的紀錄，讓使用者多選 2-4 筆，逐筆呼叫 `/audit/{id}` 拿完整內容，並排顯示 Stage A/B 結果。

**驗證方式**：用真實資料跑兩次 `/optimize/full-search`，一次 `require_data_quality_flag=true` 標籤「has-L1」、一次 `false` 標籤「no-L1」，確認審計歷程裡兩筆紀錄的 `label`、`require_data_quality_flag` 都正確區分開來。88/88 測試依然全過（這次沒改任何既有邏輯，只加欄位跟新頁面，沒寫新的 pytest，前端邏輯目前只靠手動 curl 驗證 + 語法檢查）。

**小插曲**：第一次測試時 curl 指令用了跨行 `\` 加 `||` 備援語法混在一起，導致 JSON body 被弄壞，回傳「error parsing the body」，一度以為是後端 bug。改成單行乾淨的 curl 呼叫後才確認是自己測試指令寫法的問題，不是系統的錯。**教訓**：測試指令本身出錯的時候，先懷疑指令格式，不要急著往後端邏輯找原因。

---

## 目前為止踩過的坑，整理成一句話清單（給以後的自己）

- Docker Compose volume mount：R 腳本改了自動生效，Python 程式碼改了要 `docker compose restart`。
- `git commit` 會送出整個 staging area，不是只送剛 `git add` 的東西，commit 前一定要 `git status` 確認。
- 測試斷言只檢查「有沒有回傳某個 key」防禦力等於零，外部套件/API 呼叫的測試要驗證實際數值合理性。
- R 套件版本一改，教學文章上的語法可能整批失效，官方 `tools::Rd_db()` 查目前版本文件比相信網路教學可靠。
- 同一個 HTTP header 不要身兼兩種不相關用途（`x-api-key` 那次的教訓）。
- 統計指標「能不能做」跟「該不該做」是兩回事，沒有驗證管道的公式，寧可不做（SRMR）。
- 安全機制有沒有用，要用「關掉它、看原本擋得住的東西是不是就通過了」來驗證，不能只驗證「它存在」。

## 階段 13：兩個真的 bug，一次是使用者回報、一次是自己驗證時順手抓到

**Bug A：結構模型輸入格式，同一個依變數分兩行寫會互相覆蓋**

使用者在「測量結構診斷」頁面把 ATT 的前因分兩行寫（`ATT: TRU, PE, EE, SI, FC` 和 `ATT: TE, OE, EC`），結果只跑出 4 條路徑，不是預期的 9 條。原因：`_parse_structural()`／`_parse_model()` 這個小函數在三個頁面（宣告、測量結構診斷、優化模擬器）各自複製了一份，邏輯是 `result[key] = items`——**同一個 key 出現第二次會直接覆蓋，不會合併**，第二行的 `ATT: TE, OE, EC` 把第一行蓋掉，只剩 3 個前因。

修法：不要繼續維護三份拷貝，抽成 `api_client.py` 裡一個共用函數 `parse_line_dict()`，改成 `setdefault` 合併＋去重；三個頁面都改成呼叫這個共用版本，同時在結構模型輸入框下面加一個即時預覽（`st.json`），送出前就能看到解析結果，不用等送出後才發現打錯。

**教訓**：同一段邏輯複製三份，其中一份要修的時候，另外兩份大概率一起漏改（後來確認三份果然都有一樣的 bug）。看到「這段程式碼我好像在別的檔案寫過」要立刻停下來抽共用函數，不要等 bug report 才發現。

**Bug B：L2 關卡誤把單題的人口統計「假構面」當成要驗證信效度的真構面**

修 Bug A 之後，直接呼叫 `/analyze/structural`（不手動指定 `construct_dict`，用 session 自動偵測到的分組）被 L2 關卡擋下 403，理由是 Gender、Age、Edu 這些單題人口統計欄位沒過 AVE。這些欄位是自動分組時被當成「只有一題的構面」抓進來的（`load_data()` 的欄名分組邏輯對任何欄位都會分組，不分是不是真的多題測量構面），根本不該被要求過 AVE/信度這種只對多題構面有意義的檢定。

`/analyze/measurement`（純資訊性檢查）本來就有 `latent_constructs = {k:v for k,v in construct_dict.items() if len(v)>=2}` 這行過濾，但這次新加的 `_check_l2_gate()` 沒有抄到這行，兩邊邏輯不一致——這是自己抓到的，不是被回報的。

修法：`_check_l2_gate()` 加同一行過濾，只檢查 2 題以上的構面。補了一個測試（`test_l2_gate_ignores_single_item_pseudo_constructs`）把單題假構面混進乾淨的 construct_dict，確認不會被誤擋。

**教訓**：同一個「這是不是一個要被檢定的構面」判斷邏輯，出現在兩個不同的地方（資訊性檢查 vs 硬性關卡），寫第二個的時候很容易忘記第一個已經處理過這個邊界情況。新增關卡類的功能時，要主動去找「這個系統裡還有沒有類似但範疇更廣的既有邏輯」，不能只看自己這個端點的輸入。這個 bug 也是在幫使用者驗證另一個 bug 有沒有修好的過程中，用真實資料手動測試才發現的——再次印證「自己動手用真實資料跑一次」比只看程式碼推論可靠。

## 階段 14：Phase 5b——多輪對話介面，前端整個換成純聊天（不用分頁）

使用者要的不是「加一個聊天分頁」，而是整個操作方式都改成對話：上傳資料、宣告構面/結構路徑、跑 L1-L3 診斷、看建議、討論、決定要不要調參數重跑，全部在同一串對話裡完成，不要分頁。

**框架選型**：原本評估過要不要用 LangChain/LangGraph，查證後確認這個需求（單一 agent、有限工具、不需要分支/迴圈/多 agent 協作）用原生 SDK 的 tool calling 就夠，不需要額外框架——LangGraph 官方建議也是「基本聊天機器人請直接用 SDK，用 LangGraph 是過度工程」。前端部分原本是 Streamlit，但 Streamlit 的 `st.chat_message` 每次互動要整頁 rerun，對話變長會變慢；改評估 Chainlit 跟 Gradio，Chainlit 原創始團隊已經在 2025 年中離開去創業、改由社群維護，而且 2025-2026 年爆出兩個高風險 CVE（任意檔案讀取 + SSRF，可以偷 API 金鑰跟雲端憑證）——這個系統本身就存放 LLM API 金鑰，這個風險直接排除不用。最後選 Gradio：Hugging Face 持續維護、無資安包袱、原生支援工具呼叫視覺化跟串流。

**後端（`app/main.py`）**：新增 `POST /chat`，核心是一個有上限（4 輪）的 tool-calling 迴圈，OpenAI 跟 Anthropic 兩種 wire format 分開處理（兩家對「助理呼叫工具」跟「工具回傳結果」的訊息格式編碼方式完全不同，硬要共用一套格式反而更複雜）。定義三個工具：

- `set_declaration`：把使用者對構面/結構路徑的描述轉成結構化宣告，**合併不覆蓋**（跟階段 13 Bug A 同一個教訓，這次直接在設計時就避開，`_merge_dict_of_lists()` 只更新有帶到的 key，其他 key 維持原樣）；題項名稱如果資料檔裡找不到，直接拒絕整次更新，不會部分套用。
- `run_full_pipeline`：對已上傳資料依序跑 L1 資料品質、L2 測量模型、L3 結構模型，共用既有的 `detect_careless_responses`/`calc_cronbach`/`calc_bootstrapping` 等函式，L2 沒過一樣走 `_check_l2_gate()` 擋下 L3，不會讓 LLM 自己決定要不要略過關卡。
- `rerun_optimization`：對應既有 `/optimize/full-search` 背後同一顆 `optimize_unified()`，LLM 可以帶參數（`max_drop_ratio`、`boot_iterations`、`require_data_quality_flag`），後端一樣把數值夾到 0.02-0.30 的範圍，不信任 LLM 給的原始值。

每次工具執行都照樣寫 `audit_log`，`rerun_optimization` 額外標記 `request_params.triggered_by = "chat"`、`is_exploratory = True`，跟手動觸發的紀錄用同一套稽核邏輯，只是多一個欄位可以追溯是不是聊天觸發的。對話歷史存在 session（跟 df、construct_dict 同一個容器），只存 `{"role","content"}` 這種 provider 無關的純文字格式——工具呼叫當下那些 provider 專屬的訊息格式只在單次請求內部使用，不寫回 session，這樣使用者中途換 provider 也不會壞掉。

**前端**：整個刪掉 `streamlit_app/`（七個分頁），換成 `webapp/app.py`，一個 Gradio `Blocks` 頁面：檔案上傳 + 對話框 + LLM 設定（選填，留空吃後端環境變數）。上傳成功後不額外呼叫 LLM，直接用後端回傳的欄位/自動偵測構面組一段固定文字放進對話框——這樣開場白不用等 LLM、也不會有開場白被幻覺污染的風險。工具執行的結構化結果（信效度數字、路徑顯著性、Stage A/B 搜尋紀錄）額外格式化成 Markdown 附在同一則助理訊息下面，不是只顯示 LLM 自己講的那段話。

**踩到的坑**：
1. Gradio 6.20（拉 `gradio>=5.0.0` 沒設上限，直接抓到最新的 6.x）把 `gr.Chatbot(type=...)` 這個參數拿掉了（messages 格式現在是唯一格式，不用再指定），`theme=` 也從 `Blocks()` 建構子搬到 `launch()`，照著網路上 Gradio 5 的範例寫直接炸掉，改用容器內實際安裝的版本試出正確寫法。
2. 又一次忘記「Python 程式碼改了要 `docker compose restart api`」（階段 11 已經寫過的教訓，這次真的又忘記一次）——加完 `/chat` 端點直接用 curl 測試打 404，以為是路由沒寫對，實際上是舊的 uvicorn process 還在跑改之前的程式碼。

**驗證方式**：89 個既有測試全過（沒動到既有端點邏輯），新增 11 個 `TestChatEndpoint` 測試（`_call_llm_chat` 用假函式取代，直接呼叫真正的 `_execute_chat_tool` 分派，這樣可以測到真正的 session 狀態變化、L2 關卡、審計紀錄，不用真的打 LLM API）。另外拿真實 LLM key 手動跑了一次完整流程：上傳合成資料 → 傳一句話同時宣告構面跟結構路徑並要求跑分析 → LLM 正確依序呼叫 `set_declaration` 再呼叫 `run_full_pipeline`，回覆內容跟後端真實算出來的 bootstrapping 結果一致（TR→PE 顯著、TR→EE/PE→EE 不顯著），沒有自己編數字；接著在同一個對話裡要求「把刪除比例放寬到 20% 重跑」，LLM 正確呼叫 `rerun_optimization` 並帶 `max_drop_ratio=0.2`，搜尋依然救不回顯著性時**誠實回報搜尋失敗**，沒有硬湊一個假顯著結果——這是階段 6/7 那個「安全機制要驗證『擋不擋得住』而不是『存不存在』」的教訓，這次在 LLM 觸發的路徑上重新驗證了一次，結果一致。

## 階段 15：使用者第一次真的拿去用，馬上就撞到一個 bug——工具混淆 + 盲目重試

Phase 5b 上線後使用者拿真實資料（185 筆、10 個構面）馬上實測，一句話宣告結構路徑：「ATT: 由 TRU, PE, EE, SI, FC 組成；同時 ATT: 由 TE, OE, EC 組成。BI: 由 ATT 組成。」結果 LLM 把 `TRU`、`PE`、`EE`⋯這些**構面名稱**當成題項塞進 `construct_dict`（應該要放進 `structural_model`），`set_declaration` 當然失敗（資料檔裡沒有叫做 `TRU`、`EC`、`OE` 這種欄位）。更糟的是 LLM 沒有看錯誤訊息調整，而是**用一模一樣的參數連續重試** 4 次，直接撞到 `MAX_CHAT_TOOL_ITERATIONS` 上限，整輪對話沒有任何進展。

根因：「A 由 B、C、D 組成」這句中文本身就有歧義——可以是測量描述（B、C、D 是 A 的題項）也可以是結構描述（B、C、D 是預測 A 的構面），純粹靠 tool description 裡的英文/中文說明，模型沒能穩定分辨。

**修法，三處一起改**：
1. `_tool_exec_set_declaration()` 驗證失敗時，如果「找不到的題項欄位」剛好命中已知的構面名稱，錯誤訊息裡直接加一句提示：這些名稱看起來是構面、要放 `structural_model`。這是讓**工具的回傳結果自己教模型下一步該怎麼做**，而不是只靠系統提示詞事先講一次就要它記住。
2. `_call_llm_chat()` 的 tool-calling 迴圈加一個去重防線：同一輪對話裡，`(工具名稱, 參數)` 完全相同的呼叫只會真正執行一次，第二次以後直接回一個「不要再重試，請修正或停下來問使用者」的罐頭訊息——不管模型有沒有讀懂前面的提示，這道防線都會強制打斷盲目重試迴圈，不必完全信任模型會自我修正。
3. `CHAT_SYSTEM_PROMPT` 明確加規則：`construct_dict` 只能放題項欄位、`structural_model` 只能放構面名稱，並且「工具失敗不要用同樣參數重試，卡住就直接跟使用者說」。

**驗證方式**：新增兩個測試——一個直接測試提示訊息內容（`test_set_declaration_hints_when_construct_names_used_as_items`），一個用假造的 OpenAI client 模擬「LLM 對同一個失敗呼叫重試兩次」，確認 `_execute_chat_tool` 真正只被執行一次、第二次被去重防線攔下（`test_repeated_identical_tool_call_is_not_re_executed`）。寫這個測試的過程中自己也踩了一個小坑：測試檔案本身沒有 `import json`，測試裡的假 client helper 用了 `json.dumps`，直接讓 `/chat` 回 500，錯誤訊息是「name 'json' is not defined」——先以為是新寫的去重邏輯本身壞了，實際上是測試工具函式漏 import，跟階段 12 的教訓一樣：先懷疑自己剛寫的東西，不要急著懷疑核心邏輯。

用使用者原始輸入（同樣的構面名稱、同樣的句子）重新跑一次：LLM 第一次呼叫還是一樣把構面名塞進 `construct_dict`（提示無法保證模型一次就用對工具，這是預期中的事），但這次錯誤訊息裡的提示成功讓它在**下一步立刻自我修正**，改成正確呼叫 `structural_model={"ATT": ["TRU","PE","EE","SI","FC","TE","OE","EC"], "BI": ["ATT"]}`，接著呼叫 `run_full_pipeline` 成功跑完，全程只用 3 次工具呼叫（上限是 4），沒有再卡住。100 個既有測試（含新的 2 個）全過。

## 階段 16：同一次實測，馬上又撞到第二個 bug——上傳成功訊息只是前端裝飾，LLM 其實完全不知道有資料

階段 15 修完後使用者馬上重新整個測，這次是全新 session（可能點了「重置對話與資料」）：上傳「Test_1.csv」成功、Gradio 對話框正確顯示「✅ 已上傳...」跟自動偵測到的構面分組，接著打了「開始分析」，結果 LLM 回「您尚未上傳任何資料，無法進行分析」——明明訊息串上面自己都還看得到剛剛的上傳成功訊息。

先懷疑是不是 session id 沒對上（前端狀態沒跟後端同步），直接查 SQLite 裡 `audit_log` 這次互動的原始紀錄（不需要使用者配合重現，資料早就寫進去了）：上傳跟這次失敗的 `chat_message` 兩筆紀錄 `user_id`、`dataset_id` 完全一樣，證明後端 session 是對的、資料確實在。但失敗那筆紀錄的 `tool_calls` 是空陣列——**LLM 根本沒有呼叫任何工具去確認，直接用猜的回答「沒有資料」**。

真正的根因：Gradio 前端 `on_upload()` 顯示的「✅ 已上傳...」那則訊息，是前端組出來直接塞進畫面的本地字串，**從來沒有送進後端 `session["chat_history"]`**——這樣做是為了讓上傳後的回覆不用等 LLM（快、也不花 token），但代價是後端維護的「真正會餵給 LLM 的對話紀錄」跟「畫面上使用者看到的對話紀錄」是兩份不同步的東西。使用者這次是全新 session，第一句真正送進 `/chat` 的訊息就是「開始分析」，此時 `session["chat_history"]` 是空的，LLM 看到的整段對話就只有這一句話，完全不知道有資料存在，就照系統提示詞規則 4（「使用者還沒上傳資料，先請他們上傳」）猜了一個錯誤答案。

**修法**：`/upload` 端點在 `_set_user_session()` 之後，直接把一則描述這次上傳（檔名、筆數、欄位數、自動偵測到的構面分組）的訊息，用 `role: "user"` 寫進 `session["chat_history"]`（標註「系統提示，非使用者本人輸入」）。這樣不管使用者上傳完之後打的第一句話是什麼，`/chat` 端點組出的對話紀錄裡都保證至少有這一筆，LLM 不用用猜的。前端那則裝飾用的「✅ 已上傳...」訊息繼續保留（還是不用等 LLM），現在是「畫面顯示」跟「LLM 真正看到的上下文」兩邊都各自正確，只是不再假設兩者是同一份資料。

**教訓**：任何「為了體驗快，前端自己組訊息、不透過真正的對話流程」的捷徑，都要想清楚**這個捷徑塞進去的內容，之後串接到別的路徑時看不看得到**——這裡的裝飾訊息只活在瀏覽器畫面上，跟真正驅動 LLM 的資料完全是兩條路，寫的當下沒發現，因為兩條路平常大部分情況下「感覺起來」是同步的（因為使用者通常會先聊個幾句才問正事，這時候 `chat_history` 已經有東西了，不會是空的）——只有在「上傳後的第一句話就是正事」這個最短路徑才會露餡，剛好就是使用者這次做的事。

**驗證方式**：新增兩個測試（`test_upload_seeds_chat_history_so_first_message_has_grounding`、`test_reupload_replaces_chat_history_not_appends`），另外兩個既有測試因為 `chat_history` 现在多了種子訊息在最前面，調整斷言方式（比對訊息陣列的後半段，而不是整個陣列）。用使用者的真實構面名稱重新跑一次「上傳 → 開始分析」，這次 LLM 正確呼叫 `run_full_pipeline`，回報的是真實算出來的信效度結果，不再幻覺「沒有資料」。104 個測試全過。

## 階段 17：Groq 額度撞牆兩次——一次是真的用完額度，一次是自己的訊息設計有問題

**第一次（外部額度用完，不是 bug）**：階段 16 修完後使用者再測，這次收到 429「Rate limit reached ... tokens per day (TPD): Limit 100000, Used 94730」——`llama-3.3-70b-versatile` 在 Groq 免費層的每日 token 額度被我（階段 14-16 反覆用真實 LLM key 驗證）加上使用者自己的測試一起用完了。這不是程式邏輯的錯，純粹是共用同一把免費 API key 的代價。當下建議使用者在 Gradio 的「LLM 設定」把 Model 換成額度上限高很多的 `llama-3.1-8b-instant` 應急。

**第二次（換小模型後撞到另一個真的 bug）**：換成 `llama-3.1-8b-instant` 後，下一句話就收到 413「Request too large ... tokens per minute (TPM): Limit 6000, Requested 9056」——這次不是額度用完，是**單一次請求**就超過小模型的每分鐘 token 上限，代表我們送出去的單一 request 本身異常肥大，值得認真查。

根因：`detect_careless_responses()`（L1）回傳的結果裡有一個 `respondents` 陣列，**每一筆受訪者都佔一個 dict**（185 筆資料就是 185 個 entry），這個完整結果被 `run_full_pipeline` 工具原封不動當作「工具執行結果」塞進對話，在同一輪對話裡下一次呼叫 LLM 時整包 JSON 又要重新送一次——185 筆的逐筆明細序列化後保守估計就吃掉大幾千 token，這才是撞上 TPM 上限的真正原因。而這個逐筆明細其實從頭到尾沒有任何地方真的需要它：前端 `_fmt_data_quality()` 只顯示 `flagged_count`／`total_respondents` 兩個彙總數字，LLM 也只會拿彙總數字講話，逐筆明細只是単純被「順手」原封不動轉傳，沒有被用到。

**修法**：新增 `_trim_tool_result_for_llm()`，只在「把工具結果重新餵回 LLM 對話」這個路徑上把 `data_quality.respondents` 拿掉，其餘欄位不動；**API 回應、audit_log、session 裡存的還是完整版**（稽核紀錄的完整性不能因為要省 LLM token 而打折扣），只有「送進 LLM 對話」這一份是刪減過的。這樣分兩份是刻意的：一份給人看／稽核用（要完整），一份給 LLM 讀（只要摘要）。

**教訓**：工具呼叫的回傳值如果直接就是後端 API 原本的完整回應格式，要想清楚**這個完整格式裡有沒有「給人看的細節」被順手也送進了 LLM 的對話上下文**——這兩種消費者（人 vs. LLM）要的粒度通常不一樣，尤其是任何「每筆樣本一個 entry」這種會隨資料量線性成長的欄位，越大的資料集越容易撞到 provider 的單次請求大小上限，而且不會是額度用完那種「明顯是外部問題」的錯誤訊息，很容易被誤認成別的 bug。

**驗證方式**：新增 `test_trim_tool_result_for_llm_strips_respondent_list`，確認 `respondents` 被拿掉、其他欄位不變、且不會動到傳進去的原始 dict（呼叫端例如 audit log 用的還是同一個物件）。修完後用同一份 185 筆資料、同樣的結構路徑宣告，改指定 `model=llama-3.1-8b-instant` 重新走一次「上傳 → 宣告 → 跑完整分析」，這次沒有再收到 413；不過也觀察到 8B 這種小模型對「構面名稱該放 construct_dict 還是 structural_model」這個提示的理解力明顯比 70B 弱（兩次嘗試都沒抓對，最後選擇誠實跟使用者確認而不是繼續瞎猜），這是小模型能力落差的預期取捨，不是新 bug。105 個測試全過。
