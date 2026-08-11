# 系統架構與 LLM 後續優化

最後更新：2026-08-10

## 文件分工

| 文件 | 用途 |
| --- | --- |
| `README.md` | 安裝、啟動、環境變數與 API 快速使用 |
| `PROFESSOR_REPORT.md` | 研究背景、方法與成果說明 |
| `DEVELOPMENT_LOG.md` | 開發問題、決策與修改紀錄 |
| 本文件 | 現行架構、資料流與 LLM 後續優化邊界 |

既有三份文件各有用途，暫不刪除。架構變更先更新本文件，再在開發日誌補上決策紀錄。

## 不可變核心

`上傳 → L1 資料品質 → L2 測量模型 → L3 結構模型 → L4 優化/模擬` 是系統基礎。
這些計算仍由 `app/stats_engine.py` 與 R bridge 執行。LLM 不可改變公式、閾值、輸入資料或自行產生統計數字。

## L6：後續優化討論

```text
既有統計與首次優化結果
  → 不可變分析快照
  → 使用者與 LLM 討論
  → 候選方案
  → 既有 optimize_unified 模擬
  → 保存正式結果與比較
```

新增 SQLite 資料表：

- `optimization_sessions`：凍結基準指標、構面/路徑宣告與第一次建議。
- `optimization_messages`：與快照綁定的對話。
- `optimization_scenarios`：候選參數、狀態與正式模擬結果。

候選方案只使用既有引擎支援的參數：`max_drop_ratio`、`boot_iterations`、`require_data_quality_flag`。後端會限制在原引擎安全範圍（刪除比例 0.02–0.30、bootstrap 50–1000）。

## API

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| POST | `/optimization-sessions` | 建立快照並開始討論 |
| GET | `/optimization-sessions/{id}` | 讀取快照、訊息與方案 |
| POST | `/optimization-sessions/{id}/messages` | 以快照為上下文與 LLM 討論 |
| POST | `/optimization-sessions/{id}/scenarios` | 建立候選方案草稿 |
| POST | `/optimization-scenarios/{id}/simulate` | 呼叫既有優化引擎進行模擬 |

LLM 若呼叫既有 `rerun_optimization`，後端會自動保存該次輸入與結果為候選方案。第一版不提供直接套用到正式問卷的功能，因此 LLM 無法自動修改原始資料或問卷。

## 可追溯性與安全

- 每個討論、方案與模擬都關聯使用者、資料集與研究宣告。
- 資料集與快照不一致時，討論和模擬 API 會拒絕執行。
- 原有 `audit_log` 仍會記錄建立快照與每次模擬。
- LLM API key 只應放在伺服器環境變數，不能放在前端。
