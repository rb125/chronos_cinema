# Chronos Cinema (Vertex AI Edition)

Chronos Cinema is a **Creative Storyteller** agent built for Gemini hackathon requirements: it streams narration with **Gemini Live API** while interleaving generated **video + audio** in one cohesive flow.

## What It Does
- Runs a live Gemini delegator session with **native audio** output (`gemini-live-2.5-flash-native-audio`).
- Uses Live **function-calling** to interleave worker outputs (video/music) during narration.
- Triggers Veo generation from tool calls and streams cinematic shots to the frontend in real time.
- Supports interruption by **voice** and **chat** (barge-in behavior).
- Generates/updates background score via Lyria with fallback ambient track.

## Mandatory Tech Coverage
- Gemini model: `gemini-live-2.5-flash-native-audio` (delegator), plus Gemini text/video/music workers.
- SDK: **Google GenAI SDK** (Python).
- Google Cloud service(s): **Vertex AI** (required), optional Cloud Storage output path for Veo, deployable on **Cloud Run**.

## Architecture
See [docs/architecture.md](/home/rahul/hackathons/gemini_cinema/docs/architecture.md).

## Hackathon Submission Draft
See [docs/hackathon_submission.md](/home/rahul/hackathons/gemini_cinema/docs/hackathon_submission.md) for a ready-to-edit submission package.

## Local Setup

### 1. Prerequisites
- Python 3.10+
- Node.js 18+
- `gcloud` CLI
- A Google Cloud project with billing enabled

### 2. Enable Cloud APIs
```bash
gcloud services enable aiplatform.googleapis.com run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com storage.googleapis.com
```

### 3. Authenticate for Vertex AI (ADC)
```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project "$GOOGLE_CLOUD_PROJECT"
```
This is required for `VERTEX_AUTH_MODE=project`.
For local key-only testing, use `VERTEX_AUTH_MODE=api_key` and skip ADC.
For full Live interleaving (audio + interrupts + multimodal workers), use project mode.

### 4. Backend setup
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `backend/.env`:
```bash
# Vertex auth mode: auto|project|api_key
VERTEX_AUTH_MODE=project

# Option A: full Vertex project mode (recommended)
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-central1

# Option B: API-key mode (Express-style local testing)
# GOOGLE_CLOUD_API_KEY=your-vertex-api-key

# Optional but recommended for Veo output artifacts:
VEO_OUTPUT_GCS_URI=gs://your-bucket/chronos-veo-output
VIDEO_ONLY_MODE=true

# Optional model overrides:
# GEMINI_LIVE_MODEL=gemini-live-2.5-flash-native-audio
# GEMINI_SCRIPT_MODEL=gemini-2.5-flash
# GEMINI_VIDEO_MODEL=veo-3.1-generate-001
# GEMINI_IMAGE_MODEL=imagen-4.0-fast-generate-001
# GEMINI_TEXT_MODEL=gemini-2.5-flash
# GEMINI_MUSIC_MODEL=lyria-002
```
Or copy [backend/.env.example](/home/rahul/hackathons/gemini_cinema/backend/.env.example).

If you use **API-key mode only**, set `VERTEX_AUTH_MODE=api_key`.
In that mode, project vars are ignored.
For full multimodal reliability (especially Live interleaving + Veo + GCS artifact downloads), use `VERTEX_AUTH_MODE=project` + ADC.

Run backend:
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 5. Frontend setup
```bash
cd frontend
npm install
```

Create `frontend/.env.local`:
```bash
NEXT_PUBLIC_BACKEND_WS_URL=ws://localhost:8000/ws
```
Or copy [frontend/.env.local.example](/home/rahul/hackathons/gemini_cinema/frontend/.env.local.example).

Run frontend:
```bash
npm run dev
```

Open `http://localhost:3000`.

## Cloud Run Deployment (Automated Script)

Use the deployment helper script:
```bash
export GOOGLE_CLOUD_PROJECT=your-gcp-project-id
export GOOGLE_CLOUD_LOCATION=us-central1
export VEO_OUTPUT_GCS_URI=gs://your-bucket/chronos-veo-output
./deploy_cloud_run.sh
```

This deploys backend from `backend/` to Cloud Run and injects Vertex env vars.

## Reproducibility Checklist (for Judges)
- Backend startup instructions: this README (`Local Setup` + `Cloud Run Deployment`).
- Proof of Google Cloud usage:
  - [backend/main.py](/home/rahul/hackathons/gemini_cinema/backend/main.py) uses `genai.Client(vertexai=True, ...)` against Vertex AI (project/ADC or API-key mode).
  - [deploy_cloud_run.sh](/home/rahul/hackathons/gemini_cinema/deploy_cloud_run.sh) automates Cloud Run deployment.
- Architecture diagram: [docs/architecture.md](/home/rahul/hackathons/gemini_cinema/docs/architecture.md).

## Notes
- Veo generation is long-running and quota-sensitive; the app falls back to Imagen when video is unavailable.
- If your browser blocks autoplay, user interaction is required before background score starts.
