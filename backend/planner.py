import os
import json
from google import genai
from typing import List, Dict
from dotenv import load_dotenv

load_dotenv()

class ScenePlanner:
    def __init__(self):
        load_dotenv() # Ensure env is loaded inside the class too
        api_key = os.getenv("GOOGLE_API_KEY")
        print(f"DEBUG: GOOGLE_API_KEY present: {bool(api_key)}")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY not found in environment")
        
        # New SDK client
        self.client = genai.Client(api_key=api_key)
        # Using the latest 2026 model as specified
        self.model_id = "gemini-3-flash-preview"

    async def plan_documentary(self, topic: str) -> List[Dict]:
        prompt = f"""
        You are a Master Documentary Director. Create a 3-scene cinematic documentary outline for the topic: "{topic}".
        
        For each scene, provide:
        1. "title": A compelling scene title.
        2. "script": The narration script (approx 2-3 sentences).
        3. "visual_prompt": A high-detail 4K image generation prompt in "National Geographic Photography" style.
        
        Return the response as a valid JSON list of objects.
        Example format:
        [
          {{
            "title": "The Spark",
            "script": "In the heart of...",
            "visual_prompt": "Cinematic close-up of..."
          }}
        ]
        """
        
        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config={
                    "response_mime_type": "application/json"
                }
            )
            
            scenes = json.loads(response.text)
            return scenes
        except Exception as e:
            print(f"Error calling Gemini or parsing response: {e}")
            return []

if __name__ == "__main__":
    # Quick test
    import asyncio
    planner = ScenePlanner()
    async def test():
        scenes = await planner.plan_documentary("The Life of a Red Giant Star")
        print(json.dumps(scenes, indent=2))
    asyncio.run(test())
