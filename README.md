# Chronos Cinema — AI Documentary Studio

An AI agent that produces cinematic, multimodal educational documentaries on any topic. Enter a subject and watch a live director, narrator, cinematographer, and composer collaborate in real time to generate a fully synchronized documentary — narration, video, images, and music as one cohesive unit.

**Hackathon category:** Creative Storyteller — multimodal interleaved output

---

## How It Works

1. **Script** — Gemini 2.5 Flash writes an 8-beat documentary arc (Hook → Foundation → Mechanism → Scale → Counterintuitive → Human Connection → Frontier → Reflection), ~200 words per beat.
2. **Images** — Imagen 4.0 Fast pre-renders all 8 scene stills in parallel (~10-15s each). Narration is held until the first frame is ready — the narrator never speaks to a black screen.
3. **Narration** — Gemini Live streams native audio beat-by-beat. Each beat starts only after its image is on screen. Ken Burns pan-zoom animations make stills feel cinematic.
4. **Video upgrade** — Veo 3.1 generates full cinematic clips in the background (60-180s). When each clip is ready it silently replaces its scene's still image.
5. **Music** — Lyria composes a custom background score. A fallback ambient track plays immediately while Lyria renders. BGM auto-ducks during narration and restores between beats.
6. **Quiz** — After the documentary, Gemini generates 5 multiple-choice questions from the script. Interactive quiz with scoring and per-question explanations.
7. **Interruptions** — Voice (mic) and chat can interrupt the narrator at any time.

---

## Tech Stack

| Component | Model / Service |
|-----------|----------------|
| Narration (live audio) | `gemini-live-2.5-flash-native-audio` |
| Script generation | `gemini-2.5-flash` |
| Quiz generation | `gemini-2.5-flash` |
| Video generation | `veo-3.1-generate-001` |
| Image generation | `imagen-4.0-fast-generate-001` |
| Music generation | `lyria-002` |
| Cloud platform | Vertex AI + Cloud Run + Cloud Storage |
| Backend | FastAPI + Python, WebSocket streaming |
| Frontend | Next.js 14, Tailwind CSS, Web Audio API |

---

## Prerequisites

- Python 3.10+
- Node.js 18+
- A Google Cloud project with Vertex AI enabled and billing active
- `gcloud` CLI (for ADC auth and deployment)

---

## Quick Start (Local)

### 1. Enable Cloud APIs

```bash
gcloud services enable aiplatform.googleapis.com \
  run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com
```

### 2. Authenticate

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

### 3. Configure environment

Edit `backend/.env` (already present in repo, update with your values):

```bash
VERTEX_AUTH_MODE=project
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-central1

# Required for Veo to write output artifacts:
VEO_OUTPUT_GCS_URI=gs://your-bucket/chronos-veo-output

# Optional — override default models:
# GEMINI_LIVE_MODEL=gemini-live-2.5-flash-native-audio
# GEMINI_SCRIPT_MODEL=gemini-2.5-flash
# GEMINI_VIDEO_MODEL=veo-3.1-generate-001
# GEMINI_IMAGE_MODEL=imagen-4.0-fast-generate-001
# GEMINI_MUSIC_MODEL=lyria-002
```

**API-key mode** (no ADC, limited features — no Lyria, no GCS):
```bash
VERTEX_AUTH_MODE=api_key
GOOGLE_API_KEY=your-api-key
```

### 4. Backend setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 5. Frontend setup

```bash
cd frontend
npm install
```

The frontend connects to `ws://localhost:8000/ws` by default. To use a different backend URL:

```bash
# frontend/.env.local
NEXT_PUBLIC_WS_URL=ws://your-backend-host/ws
```

### 6. Run both services

**Option A — single command:**
```bash
chmod +x start.sh
./start.sh
```

**Option B — separately (two terminals):**

Terminal 1 — backend:
```bash
cd backend
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 \
  --ws websockets-sansio --ws-ping-interval 60 --ws-ping-timeout 60
```

Terminal 2 — frontend:
```bash
cd frontend
npm run dev
```

Open **http://localhost:3000**.

---

## What to Expect

| Time | What happens |
|------|-------------|
| 0s | You enter a topic and click "Produce My Documentary" |
| ~5-10s | Script generated, all image and video renders start simultaneously |
| ~15-25s | First scene image ready → narrator begins (you see the scene, then hear the voice) |
| ~30-60s | Subsequent scene images arrive (pre-rendered while previous beat plays) |
| ~60-180s | Veo cinematic clips start arriving and silently upgrade each scene |
| Ongoing | BGM plays under narration; auto-ducks when narrator speaks |
| After narration | 5-question quiz generated from the documentary content |

---

## Cloud Run Deployment

```bash
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1
export VEO_OUTPUT_GCS_URI=gs://your-bucket/chronos-veo-output
./deploy_cloud_run.sh
```

Then set `NEXT_PUBLIC_WS_URL=wss://your-cloud-run-url/ws` in your frontend environment.

---

## Notes

- **Veo quota** — Veo generation is quota-sensitive. The app always shows an Imagen still first; Veo is a bonus upgrade. If Veo fails or times out, the still stays.
- **Lyria quota** — Requires `VERTEX_AUTH_MODE=project`. In `api_key` mode the BGM fallback (ambient track) plays instead.
- **Browser autoplay** — Web Audio requires a user gesture before audio starts. Clicking "Produce My Documentary" satisfies this.
- **Mic input** — Uses `MediaRecorder` at 16kHz mono. Chrome/Edge work best. Safari may require additional permissions.
