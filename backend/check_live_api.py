import asyncio
import os

from dotenv import load_dotenv
from vertex_config import create_vertex_client

load_dotenv()


async def test_live_api():
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
    auth_mode = (os.getenv("VERTEX_AUTH_MODE") or "auto").strip().lower()
    location = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("GCP_LOCATION")
    if not location:
        location = "global" if auth_mode == "api_key" or not project_id else "us-central1"
    model = os.getenv("GEMINI_LIVE_MODEL", "gemini-live-2.5-flash-native-audio")

    print(
        f"Testing Vertex Live API with auth_mode={auth_mode}, "
        f"project={project_id}, location={location}, model={model}"
    )

    try:
        client = create_vertex_client(api_version="v1")
        print("✓ Vertex client created")

        config = {
            "response_modalities": ["AUDIO"],
            "input_audio_transcription": {},
            "output_audio_transcription": {},
        }

        print("Attempting to connect to Vertex Gemini Live...")
        async with asyncio.timeout(30):
            async with client.aio.live.connect(model=model, config=config) as session:
                print("✓ Live API connection successful")
                await session.send(input="Say hello in one sentence.", end_of_turn=True)
                saw_audio = False
                saw_transcript = False
                async for response in session.receive():
                    sc = response.server_content
                    if not sc:
                        continue
                    if sc.model_turn and sc.model_turn.parts:
                        for part in sc.model_turn.parts:
                            inline_data = getattr(part, "inline_data", None)
                            if inline_data and inline_data.data:
                                saw_audio = True
                    output_tx = getattr(sc, "output_transcription", None)
                    if output_tx and getattr(output_tx, "text", None):
                        saw_transcript = True
                    if sc.turn_complete:
                        break
                if saw_audio and saw_transcript:
                    print("✓ Received streamed audio + transcription")
                elif saw_audio:
                    print("✓ Received streamed audio")
                else:
                    print("✗ Connected but received no audio payload")

    except TimeoutError:
        print("✗ Timeout: Live API connection did not complete in time")
    except Exception as e:
        print(f"✗ Error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(test_live_api())
