import os
from typing import Optional, Tuple

from google import genai


def resolve_vertex_api_key() -> Optional[str]:
    # Optional for local testing. Prefer ADC/service-account auth in production.
    return (
        os.getenv("GOOGLE_CLOUD_API_KEY")
        or os.getenv("VERTEX_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
    )


def resolve_project_location() -> Tuple[str, str]:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
    location = (
        os.getenv("GOOGLE_CLOUD_LOCATION")
        or os.getenv("GCP_LOCATION")
        or "us-central1"
    )
    if not project_id:
        raise RuntimeError("Set GOOGLE_CLOUD_PROJECT or GCP_PROJECT_ID.")
    return project_id, location


def create_vertex_client(
    *,
    api_version: str = "v1",
    timeout: Optional[int] = None,
) -> genai.Client:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
    location = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("GCP_LOCATION")
    auth_mode = (os.getenv("VERTEX_AUTH_MODE") or "auto").strip().lower()
    http_options = {"api_version": api_version}
    if timeout is not None:
        http_options["timeout"] = timeout
    client_kwargs = {
        "vertexai": True,
        "http_options": http_options,
    }
    api_key = resolve_vertex_api_key()
    if auth_mode not in {"auto", "project", "api_key"}:
        raise RuntimeError("Invalid VERTEX_AUTH_MODE. Use auto, project, or api_key.")

    if auth_mode == "project":
        if not project_id:
            raise RuntimeError(
                "VERTEX_AUTH_MODE=project requires GOOGLE_CLOUD_PROJECT (or GCP_PROJECT_ID)."
            )
        if not location:
            location = "us-central1"
        os.environ.pop("GOOGLE_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        client_kwargs["project"] = project_id
        client_kwargs["location"] = location
        return genai.Client(**client_kwargs)

    if auth_mode == "api_key":
        if not api_key:
            raise RuntimeError(
                "VERTEX_AUTH_MODE=api_key requires GOOGLE_CLOUD_API_KEY, "
                "VERTEX_API_KEY, or GOOGLE_API_KEY."
            )
        client_kwargs["api_key"] = api_key
        return genai.Client(**client_kwargs)

    if not project_id and not api_key:
        raise RuntimeError(
            "Set either GOOGLE_CLOUD_PROJECT (ADC mode) or GOOGLE_CLOUD_API_KEY/GOOGLE_API_KEY (API-key mode)."
        )

    # Auto mode prefers API key when available, then falls back to project mode.
    if api_key:
        client_kwargs["api_key"] = api_key
        return genai.Client(**client_kwargs)

    if not location:
        location = "us-central1"
    if project_id:
        os.environ.pop("GOOGLE_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        client_kwargs["project"] = project_id
        client_kwargs["location"] = location

    return genai.Client(**client_kwargs)
