import os
import asyncio
import json
import base64
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv
from google import genai

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChronosAgent:
    def __init__(self, api_key: str):
        # v1alpha is the required version for the Live Multimodal API
        self.client = genai.Client(api_key=api_key, http_options={'api_version': 'v1alpha'})
        # Using the standard experimental model for Live capabilities
        self.model_id = "gemini-2.0-flash-exp"
        self.image_model = "imagen-4.0-fast-generate-001"

    async def start_session(self, websocket: WebSocket):
        # Configure the live session for FULL IMMERSIVE AUDIO
        config = {
            "system_instruction": {
                "parts": [{
                    "text": """
                    You are a Master Documentary Director and Sound Designer.
                    Your goal is to provide a complete, cinematic AUDIO experience.
                    
                    AUDIO PERFORMANCE RULES:
                    1. DO NOT just speak. You are the entire soundscape.
                    2. LAYER your narration with subtle AI-generated background music and ambiance.
                    3. For COSMIC topics, include 'low orchestral hums' and 'resonant space winds'.
                    4. For HISTORICAL topics, include 'distant period-accurate ambiance' (e.g., bustling markets, wind on a battlefield).
                    5. Match the emotional tone of the music to your story (e.g., wonder, tension, sadness).
                    6. The background score should be quiet enough to hear your voice clearly but loud enough to feel 'Cinematic'.
                    7. Use your voice dynamically—whisper for mystery, speak clearly for facts.
                    
                    STORY RULES:
                    1. Direct and narrate a 3-scene documentary.
                    2. Use vivid, descriptive language so the user can 'see' the world.
                    3. Transition seamlessly from a Narrator to 'The Inquisitor' for the quiz once the documentary ends.
                    """
                }]
            },
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": "Aoede"
                    }
                }
            }
        }

        async with self.client.aio.live.connect(model=self.model_id, config=config) as session:
            print("Live session connected")
            
            # 1. Background task to receive audio/text from Gemini and send to Frontend
            async def receive_from_gemini():
                try:
                    async for message in session.receive():
                        # Handle Audio/Text content
                        if message.server_content and message.server_content.model_turn:
                            for part in message.server_content.model_turn.parts:
                                if part.inline_data:
                                    await websocket.send_text(json.dumps({
                                        "type": "audio_chunk",
                                        "data": base64.b64encode(part.inline_data.data).decode('utf-8')
                                    }))
                                if part.text:
                                    await websocket.send_text(json.dumps({
                                        "type": "narration",
                                        "content": part.text
                                    }))
                        
                except Exception as e:
                    print(f"Error in Gemini receive loop: {e}")

            asyncio.create_task(receive_from_gemini())

            # 2. Receive User Input
            try:
                while True:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    
                    if msg["type"] == "start":
                        topic = msg["topic"]
                        await session.send(input=f"Direct and narrate a cinematic documentary about: {topic}.", end_of_turn=True)
                    
                    elif msg["type"] == "user_audio":
                        audio_data = base64.b64decode(msg["data"])
                        await session.send(input=genai.types.LiveClientInput(
                            realtime_input=genai.types.LiveClientRealtimeInput(
                                media_chunks=[genai.types.LiveClientMediaChunk(
                                    data=audio_data,
                                    mime_type="audio/pcm"
                                )]
                            )
                        ))
            except WebSocketDisconnect:
                print("Client disconnected")

agent = ChronosAgent(os.getenv("GOOGLE_API_KEY"))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await agent.start_session(websocket)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
