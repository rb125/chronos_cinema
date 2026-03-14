#!/bin/bash
# run_backend.sh
set -euo pipefail

cd backend
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 --reload --ws websockets-sansio --ws-ping-interval 60 --ws-ping-timeout 60
