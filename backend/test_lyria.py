import asyncio
from dotenv import load_dotenv
from vertex_config import create_vertex_client

load_dotenv()

async def test_lyria():
    client = create_vertex_client(api_version="v1")
    try:
        print("Connecting to Gemini Live native audio...")
        async with client.aio.live.connect(model="gemini-live-2.5-flash") as session:
            print("Connected!")
            await session.send(input="A cinematic space background score, no speech.", end_of_turn=True)
            async for message in session.receive():
                print(f"Message received: {message}")
                break
    except Exception as e:
        print(f"Lyria error: {e}")

if __name__ == "__main__":
    asyncio.run(test_lyria())
