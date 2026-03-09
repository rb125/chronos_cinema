import os
import asyncio
from google import genai
from dotenv import load_dotenv

load_dotenv()

async def test_capabilities():
    api_key = os.getenv("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)

    print("--- Testing Audio Generation (Attenborough Style) ---")
    try:
        # Requesting audio output from Gemini 2.0 Flash
        response = client.models.generate_content(
            model='gemini-2.0-flash-exp',
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
        # Trying Veo / Imagen Video
        # Note: 'imagen-3.0-generate-001' is a common internal ID, let's try a public alias first
        video_response = client.models.generate_videos(
            model='imagen-3.0-generate-001', 
            prompt='A cinematic timelapse of a flower blooming, 4k, documentary style',
            config={'aspect_ratio': '16:9'}
        )
        print("Video Generation Signal Sent (Async job)")
    except Exception as e:
        print(f"Video Gen Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_capabilities())
