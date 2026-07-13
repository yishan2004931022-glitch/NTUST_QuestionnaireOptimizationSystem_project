#!/usr/bin/env bash
# Start Survey Co-Pilot API server
# Usage: bash run.sh [port]

PORT=${1:-8000}
echo "🚀 Survey Co-Pilot API starting on http://localhost:$PORT"
echo "📖 Swagger docs: http://localhost:$PORT/docs"
echo ""

cd "$(dirname "$0")"
uvicorn app.main:app --host 0.0.0.0 --port $PORT --reload
