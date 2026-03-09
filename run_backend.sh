#!/bin/bash
# run_backend.sh
export PYTHONPATH=$PYTHONPATH:.
source backend/venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
