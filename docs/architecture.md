# Chronos Cinema Architecture

```mermaid
flowchart LR
  U[User<br/>Voice + Chat] --> FE[Next.js Frontend]
  FE -->|WebSocket| BE[FastAPI Backend]
  BE -->|Live API stream<br/>audio + text| VERTEX[Vertex AI Gemini Live]
  BE -->|Cue-triggered generation| VEO[Veo (Video)]
  BE -->|Cue-triggered generation| IMAGEN[Imagen (Image)]
  VEO -->|Output URI / bytes| BE
  IMAGEN -->|Image bytes| BE
  GCS[(Cloud Storage<br/>optional Veo output bucket)] --> BE
  BE -->|audio chunks + visuals + narration| FE
```

## Flow
1. Frontend opens a WebSocket to backend.
2. Backend opens a Gemini Live session on Vertex AI.
3. Live response streams audio + text.
4. Text includes interleaving cues (`[SCENE]`, `[DIAGRAM]`).
5. Backend triggers Veo/Imagen and forwards generated media to frontend.
6. Frontend plays narration audio, updates visuals, and accepts voice/chat interruptions.
