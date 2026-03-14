# Gemini Hackathon Submission Notes

## Category
Creative Storyteller ✍️  
Focus: Multimodal Storytelling with Interleaved Output

## Text Description
Chronos Cinema is a live multimodal storytelling agent that combines Gemini Live narration with synchronized visual generation. Users provide a topic, then interrupt by voice or chat at any time to redirect the story.

### Core Features
- Real-time Gemini Live narration (audio + text interleaved stream).
- Inline mixed-media generation:
  - `[SCENE: ...]` cues trigger Veo video generation.
  - `[DIAGRAM: ...]` cues trigger Imagen image generation.
- Voice and chat barge-in interruptions.
- Background score layer in frontend to maintain cinematic atmosphere.

### Technologies Used
- Backend: FastAPI, WebSocket, Google GenAI SDK (Python).
- AI models: Gemini Live, Veo, Imagen (all via Vertex AI).
- Frontend: Next.js + Web Audio API.
- Cloud: Vertex AI + Cloud Run deployment script + optional Cloud Storage for Veo output artifacts.

### Other Data Sources
- None; generation is model-driven from user prompts and live interaction context.

### Findings / Learnings
- Veo on Vertex is long-running and commonly returns URIs, so download handling (GCS access) is important for reliable frontend playback.
- Gemini Live `AUDIO + TEXT` streaming is crucial for interleaved cue extraction while preserving natural spoken narration.
- Barge-in quality improves significantly when client-side audio queues are cleared immediately on interruption events.

## Public Repository URL
- Add your public GitHub URL here.

## Proof of Google Cloud Deployment
- Automated deployment script: [deploy_cloud_run.sh](/home/rahul/hackathons/gemini_cinema/deploy_cloud_run.sh)
- Vertex usage in backend: [backend/main.py](/home/rahul/hackathons/gemini_cinema/backend/main.py)
- Record a short screen capture of:
  1. Cloud Run service details/logs.
  2. Live app hitting deployed backend.

## Architecture Diagram
- [docs/architecture.md](/home/rahul/hackathons/gemini_cinema/docs/architecture.md)

## Demo Video Outline (<4 min)
1. Problem and value proposition.
2. Live storytelling session with interleaved visuals.
3. Voice interruption + chat interruption during narration.
4. Technical proof points (Vertex, Gemini Live interleaving, Cloud Run).
