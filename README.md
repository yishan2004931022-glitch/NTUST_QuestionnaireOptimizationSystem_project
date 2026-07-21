# Survey Co-Pilot API

AI-powered PLS-SEM diagnostic + optimization engine.

## Quick Start (Windows 11 + Docker Desktop)

外在需求：
- Docker Desktop 已安裝並啟動
- 建議用 **Git Bash** 或 **PowerShell**（先不要用 CMD）

1) Clone / 進入專案資料夾
2) Build image + 啟動 container

```bash
docker compose up --build
```

3) OpenAPI / Swagger

- Swagger UI：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc

4) Health

```bash
curl http://localhost:8000/health
```

5) 上傳資料（CSV）

```bash
curl -F "file=@/c/Users/Selina/path/to/data.csv" http://localhost:8000/upload
```

> Windows 絕對路徑在 curl 裡建議用 `/c/Users/...` 這種 Git Bash 格式。

6) Stop

```bash
docker compose down
```

## Environment

- `.env`：端口等設定
- `.env.example`：可攜帶的模板

## Data format

- 上傳支援 `.csv` 或 `.xlsx`。
- 首欄建議放 item 名稱，其餘 metadata 會被自動忽略。
- 在記憶體內的一個 session 內，`/upload` → `/analyze/...` 可連續呼叫。

```csv
TR1,TR2,TR3,PE1,PE2
3.2,2.8,3.1,4.0,3.9
2.9,3.0,2.7,3.8,4.1
```

## Session 與 construct_dict

`/upload` 會自動偵測構面；也可手動帶入：

```json
{
  "Trust": ["TR1","TR2","TR3"],
  "Performance": ["PE1","PE2"]
}
```

## APIs + Error codes

| 方法 | 路徑 | 說明 | 成功碼 | 常見錯誤 |
|------|------|------|--------|----------|
| GET | `/health` | 健康檢查 | 200 | - |
| POST | `/upload` | 上傳問卷 | 200 | 400 解析失敗 |
| POST | `/analyze/measurement` | 測量模型診斷 | 200 | 400 沒上傳資料 |
| POST | `/analyze/structural` | 結構模型診斷 | 200 | 400 沒上傳資料；500 分析失敗 |
| POST | `/analyze/efa` | EFA + PA | 200 | 400 沒上傳資料；500 R 錯誤 |
| POST | `/analyze/seminr` | PLS-SEM | 200 | 400 measurement/structural 為空；500 R 錯誤 |
| POST | `/analyze/llm-suggestions` | LLM 建議（可選 provider） | 200 | 400 沒上傳資料；500 外部 LLM 錯誤 |
| POST | `/optimize/measurement` | 測量模型最佳化 | 200 | 400 沒上傳資料 |
| POST | `/optimize/path` | 結構路徑最佳化 | 200 | 400 沒上傳資料 |
| POST | `/analyze/full` | 完整 pipeline | 200 | 400 沒上傳資料；500 分析失敗 |
| GET | `/session/info` | 目前 session | 200 | - |

### /analyze/llm-suggestions

```bash
# 本地 fallback（不用 provider）
curl -H 'x-api-key: NTUSTProject' \
  -H 'Content-Type: application/json' \
  -d '{"action":"optimize_items","target_items":["TR1","TR2"]}' \
  http://localhost:8000/analyze/llm-suggestions
```

```bash
# 接 OpenAI
curl -H 'x-api-key: NTUSTProject' \
  -H 'Content-Type: application/json' \
  -d '{"action":"optimize_items","provider":"openai","model":"gpt-4o-mini","api_key":"sk-...","target_items":["TR1","TR2"]}' \
  http://localhost:8000/analyze/llm-suggestions
```

```bash
# 接 Claude
curl -H 'x-api-key: NTUSTProject' \
  -H 'Content-Type: application/json' \
  -d '{"action":"optimize_items","provider":"anthropic","model":"claude-3-5-haiku-20241022","api_key":"sk-ant-...","target_items":["TR1","TR2"]}' \
  http://localhost:8000/analyze/llm-suggestions
```

| 參數 | 說明 |
|------|------|
| `provider` | `openai`、`anthropic`，省略則用後端 guardrailed fallback |
| `model` | 模型名稱，省略則用預設：`gpt-4o-mini` / `claude-3-5-haiku-20241022` |
| `api_key` | provider API key，也可透過 `LLM_API_KEY` 注入 |
| `temperature` | 建立 prompt 用，預設 `0.2` |
| `max_tokens` | 預設 `1200` |

## LLM Environment（建議用 `.env`）

```
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...
```

- `provider` 只接受 `openai`、`anthropic`
- 若呼叫端未帶 `api_key`，後端會 fallback 成 guardrailed 建議，不會中斷

### /analyze/efa

```json
{
  "par_suggest": null,
  "efa_factors": 2,
  "rmse": null,
  "loadings": {...},
  "item_assignments": {...}
}
```

### /analyze/seminr

```json
{
  "measurement_loadings": [...],
  "reliability": [...],
  "paths": [...],
  "r_squared": [...],
  "vif": [...],
  "error": {}
}
```

### 400 / 422 常見原因
- 尚未上傳資料
- `construct_dict` / `measurement` / `structural` 為空
- 題項名稱在 uploaded data 裡找不到
- 上傳檔案格式不支援或損毀

### 500 常見原因
- container 內缺少 R 套件：`psych`、`magrittr`、`seminr`
- R 版本/相依性與 image 不符

## Tests

```bash
python -m pytest -q
```

## Notes

- `/analyze/efa` 與 `/analyze/seminr` 需要在 container 內安裝 `Rscript`、
  `psych`、`magrittr`、`seminr`；若未安裝，api 通常會回 500。
- Stage 3 目前先不涵蓋在 Docker 範圍內。
- 建議不要把 `.env` 推上 repo；自己用 `.env.example` 建立。

## Production Hardening Checklist

Use this checklist before exposing the API beyond a trusted admin network.

- [ ] `API_KEY`: set a strong shared key in `.env`; rotate periodically.
- [ ] `LLM_API_KEY`: store in a real secret manager instead of `.env` for production.
- [ ] `SESSION_USER_ISOLATION=true`: activate per-user file paths.
- [ ] `SESSION_USER_ROOT`: point to a mounted volume with quota/backup policy.
- [ ] `SESSION_TOKEN_TTL`: reduce from default `86400` to the minimum acceptable window.
- [ ] CORS: lock `allow_origins` from `*` to your frontend origin(s).
- [ ] TLS: terminate TLS at a reverse proxy (Nginx/Caddy/Traefik), not uvicorn.
- [ ] Persistence: mount `/app/data` to external storage in `docker-compose.yml`.
- [ ] Secrets: use Docker secrets, an external `.env`, or a vault for API/LLM keys.
- [ ] Observability: add request logging, structured JSON logs, and metrics.
