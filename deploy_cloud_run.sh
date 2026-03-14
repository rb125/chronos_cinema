#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-${GCP_PROJECT_ID:-}}"
REGION="${GOOGLE_CLOUD_LOCATION:-${GCP_LOCATION:-us-central1}}"
SERVICE_NAME="${CLOUD_RUN_SERVICE:-chronos-cinema-backend}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Set GOOGLE_CLOUD_PROJECT (or GCP_PROJECT_ID)."
  exit 1
fi

echo "Deploying ${SERVICE_NAME} to Cloud Run in project=${PROJECT_ID}, region=${REGION}"

gcloud config set project "${PROJECT_ID}"
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com

ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION}"
if [[ -n "${VEO_OUTPUT_GCS_URI:-}" ]]; then
  ENV_VARS="${ENV_VARS},VEO_OUTPUT_GCS_URI=${VEO_OUTPUT_GCS_URI}"
fi
if [[ -n "${GEMINI_LIVE_MODEL:-}" ]]; then
  ENV_VARS="${ENV_VARS},GEMINI_LIVE_MODEL=${GEMINI_LIVE_MODEL}"
fi
if [[ -n "${GEMINI_VIDEO_MODEL:-}" ]]; then
  ENV_VARS="${ENV_VARS},GEMINI_VIDEO_MODEL=${GEMINI_VIDEO_MODEL}"
fi
if [[ -n "${GEMINI_IMAGE_MODEL:-}" ]]; then
  ENV_VARS="${ENV_VARS},GEMINI_IMAGE_MODEL=${GEMINI_IMAGE_MODEL}"
fi
if [[ -n "${GEMINI_TEXT_MODEL:-}" ]]; then
  ENV_VARS="${ENV_VARS},GEMINI_TEXT_MODEL=${GEMINI_TEXT_MODEL}"
fi

gcloud run deploy "${SERVICE_NAME}" \
  --source backend \
  --region "${REGION}" \
  --allow-unauthenticated \
  --set-env-vars "${ENV_VARS}"

echo "Deployment complete."
echo "Tip: copy the deployed URL and set NEXT_PUBLIC_BACKEND_WS_URL to wss://.../ws"
