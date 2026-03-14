import os

from dotenv import load_dotenv
from google import genai

load_dotenv()


def list_models():
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
    location = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("GCP_LOCATION") or "us-central1"
    if not project_id:
        raise RuntimeError("Set GOOGLE_CLOUD_PROJECT or GCP_PROJECT_ID.")

    client = genai.Client(
        vertexai=True,
        project=project_id,
        location=location,
        http_options={"api_version": "v1"},
    )

    print(f"Listing models for Vertex project={project_id}, location={location}")
    try:
        models = client.models.list()
        for model in models:
            methods = getattr(model, "supported_generation_methods", None)
            print(f"- {model.name}")
            if methods:
                print(f"  Methods: {methods}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    list_models()
