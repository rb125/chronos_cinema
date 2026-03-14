# Gemini Hackathon Submission Notes

## Category
Creative Storyteller ✍️
Focus: Multimodal Storytelling with Interleaved Output

## Text Description
Chronos Cinema is an AI-powered documentary generator that streams narrated, scored films on any topic using Gemini, Veo, Imagen, and Lyria — complete with visuals, voice, music, quiz, and session replay.

Users provide a topic and their name, then watch a fully produced 8-beat documentary stream in real time. They can interrupt mid-narration by voice or chat to redirect the story. Completed sessions are saved locally (IndexedDB) for replay without repeat API calls. A post-documentary quiz tests recall, and a closed-caption overlay makes it feel like actual video.

### Core Features
- Real-time Gemini Live narration (`gemini-live-2.5-flash-native-audio`) with audio + text interleaved stream.
- Inline mixed-media generation:
  - `[SCENE: ...]` cues trigger Veo 3.1 video generation.
  - `[DIAGRAM: ...]` cues trigger Imagen 4.0 image generation with Ken Burns pan/zoom animations.
- Background score via Lyria (RealTime music generation), ducked under narration.
- Voice and chat barge-in interruptions with immediate audio queue clear and subtitle reset.
- Session persistence: all visuals, narration PCM, BGM, and quiz saved to IndexedDB for replay.
- Replay mode: plays back saved sessions beat-by-beat, no API calls.
- Post-documentary multiple-choice quiz (generated inline during narration).
- Closed-caption (CC) overlay on the video frame with toggle.
- Theater mode and fullscreen support.
- History sidebar showing past documentaries with one-click replay.
- YouTube-style video size controls (default / theater).
- Application-level WebSocket heartbeat to survive long Veo renders.

### Technologies Used
- **Backend**: FastAPI, WebSocket (`websockets-sansio`), Google GenAI SDK (Python), async semaphores for media workers.
- **AI Models**: Gemini Live 2.5 Flash Native Audio, Veo 3.1, Imagen 4.0, Lyria RealTime (all via Vertex AI).
- **Frontend**: Next.js 14 (App Router, TypeScript), Web Audio API (gapless PCM scheduling, BGM ducking), IndexedDB for session storage, Tailwind CSS v3 with dark glassmorphism design system.
- **Cloud**: Vertex AI + Cloud Run deployment script + optional Cloud Storage for Veo output artifacts.

### Design System
- Deep space dark theme (`#06060F` background) with violet/fuchsia/cyan accent palette.
- Animated gradient logo, glassmorphism cards, Ken Burns CSS keyframes (8 variants).
- Responsive layout: default card view ↔ theater mode ↔ fullscreen.

### Other Data Sources
- None; generation is entirely model-driven from user prompts and live interaction context.

### Findings / Learnings
- Veo on Vertex is long-running (90–180s); disabling server-side WebSocket pings (`ws_ping_interval=None`) and adding an application-level heartbeat (`{type:"ping"}` every 25s) prevents `1011 keepalive ping timeout` disconnections.
- Gemini Live `AUDIO + TEXT` streaming is crucial for interleaved cue extraction while preserving natural spoken narration.
- Barge-in quality improves significantly when client-side audio queues are cleared immediately on interruption events — `AudioContext` gain fade + `nextNarrationTime` reset achieves sub-100ms perceived response.
- Accumulating all streaming data (PCM chunks per beat, base64 visuals, BGM) in a ref during live playback enables seamless IndexedDB-backed replay without any re-generation.
- PCM sample count (Int16 = 2 bytes/sample at 24kHz) gives accurate audio duration estimates for subtitle timing during replay.
- Next.js builds in network-restricted environments (Cloud Run) require system font stacks — `next/font/google` fails silently at build time when Google Fonts CDN is unreachable.

## Public Repository URL
- Add your public GitHub URL here.

## Proof of Google Cloud Deployment
- Automated deployment script: `deploy_cloud_run.sh`
- Vertex usage in backend: `backend/main.py`
- Record a short screen capture of:
  1. Cloud Run service details/logs.
  2. Live app hitting deployed backend.

## Architecture Diagram
- [docs/architecture.md](./architecture.md)

## Demo Video Outline (<4 min)
1. Problem and value proposition — "watch a documentary on anything, in 90 seconds."
2. Live documentary session: topic input → loading log → first beat visual → narration + BGM.
3. Voice interruption + chat interruption mid-narration (subtitle clears, audio fades, story redirects).
4. Session saved → history sidebar → one-click replay (no API calls).
5. Post-documentary quiz.
6. CC toggle, theater mode, fullscreen.
7. Technical proof points: Vertex AI, Gemini Live interleaving, Veo/Imagen/Lyria workers, Cloud Run.
