import json
import os
from typing import Dict, List

from dotenv import load_dotenv
from google import genai

load_dotenv()


class ScenePlanner:
    def __init__(self):
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
        location = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("GCP_LOCATION") or "us-central1"
        if not project_id:
            raise ValueError("Set GOOGLE_CLOUD_PROJECT or GCP_PROJECT_ID.")

        self.client = genai.Client(
            vertexai=True,
            project=project_id,
            location=location,
            http_options={"api_version": "v1"},
        )
        self.model_id = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")

    async def plan_documentary(self, topic: str) -> List[Dict]:
        prompt = f"""
You are a documentary creative director.
Create a 3-scene outline for the topic: "{topic}".

For each scene include:
1. "title"
2. "script" (2-3 sentences)
3. "visual_prompt" (high-detail image prompt)

Return JSON array only.
"""
        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"Planner error: {e}")
            return []


if __name__ == "__main__":
    import asyncio

    planner = ScenePlanner()

    async def test():
        scenes = await planner.plan_documentary("The Life of a Red Giant Star")
        print(json.dumps(scenes, indent=2))

    asyncio.run(test())
