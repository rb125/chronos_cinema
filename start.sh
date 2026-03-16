#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "============================================"
echo "  CHRONOS CINEMA — AI Documentary Studio"
echo "============================================"

# ── Backend ──────────────────────────────────────
echo ""
echo "[1/2] Starting backend..."
cd "$SCRIPT_DIR/backend"
if [ -d ".venv" ]; then
  source .venv/bin/activate
fi
uvicorn main:app --host 0.0.0.0 --port 8000 \
  --ws websockets-sansio \
  --ws-ping-interval 60 --ws-ping-timeout 60 &
BACKEND_PID=$!

# ── Frontend ─────────────────────────────────────
echo ""
echo "[2/2] Starting frontend..."
cd "$SCRIPT_DIR/frontend"
if [ ! -d "node_modules" ]; then
  echo "  Installing frontend dependencies..."
  npm install --silent
fi
npm run dev &
FRONTEND_PID=$!

echo ""
echo "============================================"
echo "  Chronos Cinema is running!"
echo "  Backend  : http://localhost:8000"
echo "  Frontend : http://localhost:3000"
echo "  Press Ctrl+C to stop all services"
echo "============================================"
echo ""

trap "echo 'Shutting down...'; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM

wait $BACKEND_PID $FRONTEND_PID
