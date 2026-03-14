import asyncio
from dotenv import load_dotenv
from vertex_config import create_vertex_client

load_dotenv()

async def test_capabilities():
    client = create_vertex_client(api_version="v1")

    print("--- Testing Audio Generation (Attenborough Style) ---")
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents='Describe a black hole in one sentence, like a dramatic documentary narrator.',
            config={
                'response_mime_type': 'audio/mp3'
            }
        )
        print(f"Audio Generated: {len(response.text) if response.text else 'Binary data received'}")
    except Exception as e:
        print(f"Audio Gen Failed: {e}")

    print("\n--- Testing Video Generation ---")
    try:
        client.models.generate_videos(
            model='veo-3.1-generate-001',
            prompt='A cinematic timelapse of a flower blooming, 4k, documentary style',
            config={'aspect_ratio': '16:9'}
        )
        print("Video Generation Signal Sent (Async job)")
    except Exception as e:
        print(f"Video Gen Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_capabilities())
