from dotenv import load_dotenv
from vertex_config import create_vertex_client

load_dotenv()

def test_music_gen():
    client = create_vertex_client(api_version="v1")
    prompt = "Generate 30 seconds of cinematic ambient space music for a documentary about black holes. No speech, only music."
    
    try:
        print("Requesting music generation...")
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={
                'response_mime_type': 'audio/mp3'
            }
        )
        
        # Check where the audio is
        print("Response type:", type(response))
        for part in response.candidates[0].content.parts:
            if part.inline_data:
                print(f"Found inline_data: {len(part.inline_data.data)} bytes")
                with open("test_music.mp3", "wb") as f:
                    f.write(part.inline_data.data)
                print("Saved to test_music.mp3")
            if part.text:
                print(f"Found text: {part.text[:100]}...")
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_music_gen()
