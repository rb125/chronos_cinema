import asyncio
import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

async def debug_session():
    api_key = os.getenv("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key, http_options={'api_version': 'v1alpha'})
    
    config = {
        "generation_config": {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": "Aoede"
                    }
                }
            }
        }
    }
    try:
        async with client.aio.live.connect(model="gemini-2.5-flash-native-audio-preview-12-2025", config=config) as session:
            print(f"Session type: {type(session)}")
            print(f"Session dir: {dir(session)}")
            # Check for __aiter__
            print(f"Has __aiter__: {hasattr(session, '__aiter__')}")
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(debug_session())
