import asyncio
from dotenv import load_dotenv
from vertex_config import create_vertex_client

load_dotenv()

async def debug_session():
    client = create_vertex_client(api_version="v1")
    
    config = {
        "response_modalities": ["AUDIO", "TEXT"],
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {
                    "voice_name": "Aoede"
                }
            }
        }
    }
    try:
        async with client.aio.live.connect(model="gemini-live-2.5-flash", config=config) as session:
            print(f"Session type: {type(session)}")
            print(f"Session dir: {dir(session)}")
            print(f"Has __aiter__: {hasattr(session, '__aiter__')}")
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(debug_session())
