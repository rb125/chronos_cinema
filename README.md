# Chronos Cinema — AI Documentary Studio

An AI agent that produces cinematic, multimodal educational documentaries on any topic. Enter a subject and watch a live director, narrator, cinematographer, and composer collaborate in real time to generate a fully synchronized documentary — narration, images, and music as one cohesive unit.

**Hackathon category:** Creative Storyteller — multimodal interleaved output

---

## Architecture

The browser opens a WebSocket to the FastAPI backend. The backend creates a per-connection `ChronosAgent`, opens a Gemini Live session on Vertex AI, and runs parallel workers for script, images (Gemini image generation), and music (Lyria). All media streams back to the frontend in real time over the same WebSocket.

---

## For Judges: How to Test

### Option A — API-Key Mode (fastest, ~2 min setup)

> Lyria music requires a GCP project (Option B). In API-key mode the app falls back to an ambient BGM — narration, images, and quiz still work fully.

**1. Start the backend**

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 \
  --ws websockets-sansio --ws-ping-interval 60 --ws-ping-timeout 60
```

**2. Start the frontend**

```bash
cd frontend
npm install
npm run dev
```

**3. Open the app**

Open **http://localhost:3000**, enter your **Gemini API key** in the sidebar key field, type a topic, and click **▶ Produce My Documentary**.

> Get a free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

---

### Option B — Full GCP Project Mode (all features: Lyria BGM)

**Prerequisites:** GCP project with billing enabled, `gcloud` CLI installed and authenticated.

**1. Enable APIs and authenticate**

```bash
gcloud services enable aiplatform.googleapis.com
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

**2. Configure `backend/.env`**

```env
VERTEX_AUTH_MODE=project
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GEMINI_LIVE_MODEL=gemini-live-2.5-flash-native-audio
GEMINI_SCRIPT_MODEL=gemini-2.5-flash
GEMINI_IMAGE_MODEL=gemini-2.5-flash-image
GEMINI_TEXT_MODEL=gemini-2.5-flash-preview-tts
GENERATE_VIDEO=false
```

**3. Run both services**

```bash
chmod +x start.sh && ./start.sh
```

Open **http://localhost:3000**.

---

### What to Expect During a Run

| Time | What happens |
|------|-------------|
| 0 s | Enter topic → click Produce |
| ~5–10 s | Script generated, all 4 scene images start rendering in parallel |
| ~15–25 s | First scene image ready → narrator begins with Ken Burns animation |
| ~30 s | Remaining images arrive and are cached for each beat |
| Ongoing | Lyria BGM plays under narration; auto-ducks when narrator speaks |
| After narration | Narrator cues the quiz; 5-question quiz appears after audio finishes |

---

### Feature Checklist for Judges

- [ ] **Live narration** — Gemini Live audio plays beat-by-beat with closed captions
- [ ] **Interleaved visuals** — Gemini image stills with Ken Burns pan-zoom per beat
- [ ] **Background score** — Lyria composes a custom orchestral score (project mode); ambient fallback otherwise
- [ ] **Quiz** — Narrator cues the quiz; 5 MCQs appear after narration ends with scoring and explanations
- [ ] **Replay** — Click any entry in the history sidebar to replay the full session (IndexedDB)
- [ ] **Theater mode** — Click the resize icon on the cinema frame for fullscreen view

---

## How It Works

1. **Script** — Gemini 2.5 Flash writes a 4-beat documentary arc (Hook → Foundation → Mechanism → Reflection), ~50 words per beat.
2. **Images** — `gemini-2.5-flash-image` pre-renders all 4 scene stills in parallel. Narration is held until the first frame is ready — the narrator never speaks to a black screen. Ken Burns pan-zoom animations make stills feel cinematic.
3. **Narration** — Gemini Live (`gemini-live-2.5-flash-native-audio`) streams native audio beat-by-beat. Each beat starts only after its image is on screen.
4. **Music** — Lyria 002 composes a custom background score concurrently. A fallback ambient track plays immediately while Lyria renders. BGM auto-ducks during narration and restores between beats.
5. **Quiz** — After all beats, Gemini 2.5 Flash generates 5 MCQs concurrently with the closing reflection narration. The quiz appears only after the narrator finishes speaking.

---

## Tech Stack

| Component | Model / Service |
|-----------|----------------|
| Narration (live audio) | `gemini-live-2.5-flash-native-audio` (Vertex AI, `v1beta1`) |
| Script generation | `gemini-2.5-flash` (Vertex AI, `v1`) |
| Quiz generation | `gemini-2.5-flash` |
| Image generation | `gemini-2.5-flash-image` |
| Music generation | `lyria-002` (Vertex AI predict) |
| Backend | FastAPI + Python, WebSocket streaming |
| Frontend | Next.js 14, Tailwind CSS, Web Audio API |

---

## Prerequisites

- Python 3.10+
- Node.js 18+
- A Gemini API key **or** a GCP project with Vertex AI enabled

---

## Quick Start (Local)

```bash
# Terminal 1 — backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 \
  --ws websockets-sansio --ws-ping-interval 60 --ws-ping-timeout 60

# Terminal 2 — frontend
cd frontend
npm install && npm run dev
```

Open **http://localhost:3000** and enter your API key in the sidebar.

To use a different backend URL:
```bash
# frontend/.env.local
NEXT_PUBLIC_WS_URL=ws://your-backend-host/ws
```

---

## Cloud Deployment

[`deploy.sh`](deploy.sh) automates the full backend deployment to Cloud Run:

```bash
export GOOGLE_CLOUD_PROJECT=your-project-id
chmod +x deploy.sh && ./deploy.sh
```

The script:
1. Enables all required GCP APIs (`aiplatform`, `run`, `cloudbuild`, `artifactregistry`)
2. Builds and deploys the backend from source via `gcloud run deploy --source`
3. Prints the deployed service URL and the `NEXT_PUBLIC_WS_URL` value for the frontend

Then deploy the frontend to Vercel or any static host with:
```bash
# frontend/.env.local
NEXT_PUBLIC_WS_URL=wss://your-cloud-run-url/ws
```

---

## Notes

- **API key in UI** — Entered in the browser, transmitted only to your own backend over WebSocket. Stored in `sessionStorage` (clears on tab close), never persisted server-side.
- **Lyria** — Requires `VERTEX_AUTH_MODE=project` with ADC credentials. Falls back to ambient oscillator BGM in API-key mode.
- **Gemini Live API version** — The Live session uses `v1beta1`; all other Vertex AI calls use `v1`. These are isolated clients to prevent conflicts.
- **Browser autoplay** — Web Audio requires a user gesture. Clicking "Produce My Documentary" satisfies this.
- **Session history** — Saved to IndexedDB locally. Firebase sync is available but disabled by default (configure `frontend/app/lib/firebase.ts` to enable).
