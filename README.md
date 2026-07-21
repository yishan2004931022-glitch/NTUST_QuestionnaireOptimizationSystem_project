# Survey Co-Pilot API

AI-powered PLS-SEM diagnostic + optimization engine.

## Quick Start (Windows 11 + Docker Desktop)

外在需求：
- Docker Desktop 已安裝並啟動
-建議用 **Git Bash** 或 **PowerShell**（先不要用 CMD）

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

上傳支援 `.csv` 或 `.xlsx`。
若為 R-backed 分析（EFA / seminr），首欄應為 item 欄位；常見 metadata 欄位會被自動忽略。

## Tests

```bash
python -m pytest -q
```

## Notes

- `/analyze/efa` 與 `/analyze/seminr` 需要在 container 內安裝 `Rscript`、
  `psych`、`magrittr`、`seminr`；若未安裝，api 會回 500。
- Stage 3 目前先不涵蓋在 Docker 範圍內。
