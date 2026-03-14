import asyncio
from dotenv import load_dotenv
from vertex_config import create_vertex_client

load_dotenv()

async def test_live_music():
    client = create_vertex_client(api_version="v1")
    
    config = {
        "response_modalities": ["AUDIO"],
        "system_instruction": "You are a musician. When I ask for music, generate audio that is purely instrumental music. No talking."
    }
    
    try:
        async with client.aio.live.connect(model="gemini-live-2.5-flash", config=config) as session:
            print("Connected to Live session...")
            await session.send(input="Play a 5-second upbeat drum beat. No talking, just the music.", end_of_turn=True)
            
            async for message in session.receive():
                if message.server_content and message.server_content.model_turn:
                    for part in message.server_content.model_turn.parts:
                        if part.inline_data:
                            print(f"Received audio chunk: {len(part.inline_data.data)} bytes")
                            return True
                if message.server_content and message.server_content.turn_complete:
                    print("Turn complete.")
                    break
    except Exception as e:
        print(f"Live music error: {e}")
    return False

if __name__ == "__main__":
    asyncio.run(test_live_music())
