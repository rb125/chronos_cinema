#!/bin/bash
# deploy.sh — Deploy Chronos Cinema backend to Cloud Run
set -euo pipefail

# ── Config (override via env or edit here) ────────────────────────────────────
# Load from backend/.env if present
if [ -f "./backend/.env" ]; then
  set -o allexport
  source ./backend/.env
  set +o allexport
fi

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT in backend/.env or env}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE_NAME="chronos-cinema-backend"

echo "============================================"
echo "  Chronos Cinema — Cloud Run Deployment"
echo "  Project : $PROJECT_ID"
echo "  Region  : $REGION"
echo "  Service : $SERVICE_NAME"
echo "============================================"

# ── Enable required APIs ──────────────────────────────────────────────────────
echo ""
echo "[1/3] Enabling GCP APIs..."
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project "$PROJECT_ID"

# ── Deploy backend from source ────────────────────────────────────────────────
echo ""
echo "[2/3] Deploying backend to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
  --source ./backend \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --platform managed \
  --allow-unauthenticated \
  --timeout 3600 \
  --concurrency 80 \
  --min-instances 0 \
  --max-instances 10 \
  --set-env-vars "VERTEX_AUTH_MODE=project,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GENERATE_VIDEO=false"

# ── Print frontend env ────────────────────────────────────────────────────────
echo ""
echo "[3/3] Deployment complete."
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" --project "$PROJECT_ID" \
  --format "value(status.url)")
echo ""
echo "  Backend URL : $SERVICE_URL"
echo ""
echo "  Set this in frontend/.env.local:"
echo "  NEXT_PUBLIC_WS_URL=wss://${SERVICE_URL#https://}/ws"
echo ""
echo "  Then deploy the frontend to Vercel or any static host."
