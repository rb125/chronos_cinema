import asyncio
import base64
import json
import os
import re
from contextlib import suppress
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
import uvicorn
import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest
from pydantic import BaseModel, Field, ValidationError

try:
    from google.cloud import storage
except ImportError:
    storage = None


load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_vertex_runtime() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
    location = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("GCP_LOCATION")
    api_key = (
        os.getenv("GOOGLE_CLOUD_API_KEY")
        or os.getenv("VERTEX_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
    )
    auth_mode = (os.getenv("VERTEX_AUTH_MODE") or "auto").strip().lower()

    if auth_mode not in {"auto", "project", "api_key"}:
        raise RuntimeError(
            "Invalid VERTEX_AUTH_MODE. Use one of: auto, project, api_key."
        )

    if auth_mode == "project":
        if not project_id:
            raise RuntimeError(
                "VERTEX_AUTH_MODE=project requires GOOGLE_CLOUD_PROJECT (or GCP_PROJECT_ID)."
            )
        if not location:
            location = "us-central1"
        return project_id, location, None

    if auth_mode == "api_key":
        if not api_key:
            raise RuntimeError(
                "VERTEX_AUTH_MODE=api_key requires GOOGLE_CLOUD_API_KEY, "
                "VERTEX_API_KEY, or GOOGLE_API_KEY."
            )
        if not location:
            # API-key mode uses the global publisher endpoint by default.
            location = "global"
        return None, location, api_key

    if not project_id and not api_key:
        raise RuntimeError(
            "Missing Vertex auth configuration. Set either:\n"
            "1) GOOGLE_CLOUD_PROJECT (+ optional GOOGLE_CLOUD_LOCATION) with ADC/service account, or\n"
            "2) GOOGLE_CLOUD_API_KEY (or VERTEX_API_KEY / GOOGLE_API_KEY) for API-key mode."
        )

    # Auto mode prefers project auth for full Live + media functionality.
    if project_id:
        if not location:
            location = "us-central1"
        return project_id, location, None

    if not location:
        location = "global"
    return None, location, api_key


class ScriptBeat(BaseModel):
    beat_id: int = Field(ge=1)
    segment_title: str
    target_duration_seconds: int = Field(default=15, ge=15, le=60)
    narration_script: str
    visual_prompt_veo: str
    fallback_image_prompt: str


class ScriptBlueprint(BaseModel):
    title: str
    music_prompt: str
    documentary_flow: List[ScriptBeat] = Field(min_length=5, max_length=10)


class ChronosAgent:
    VIDEO_POLL_INTERVAL_SECONDS = 3
    VIDEO_POLL_TIMEOUT_SECONDS = 180

    def __init__(
        self,
        project_id: Optional[str],
        location: Optional[str],
        video_output_gcs_uri: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.project_id = project_id
        self.location = location
        self.video_output_gcs_uri = video_output_gcs_uri
        self.auth_mode = "express_api_key" if api_key and not project_id else "adc"
        client_kwargs: Dict[str, Any] = {"http_options": {"api_version": "v1"}}
        if api_key and not project_id:
            # Gemini API key mode — vertexai=True is incompatible with api_key
            client_kwargs["api_key"] = api_key
        else:
            client_kwargs["vertexai"] = True
            if project_id:
                os.environ.pop("GOOGLE_API_KEY", None)
                os.environ.pop("GEMINI_API_KEY", None)
                client_kwargs["project"] = project_id
            if location:
                client_kwargs["location"] = location

        self.client = genai.Client(
            **client_kwargs,
        )

        # Model IDs are env-overridable to allow rapid tuning during hackathon iteration.
        self.live_model = os.getenv(
            "GEMINI_LIVE_MODEL",
            "gemini-live-2.5-flash-native-audio",
        )
        self.script_model = os.getenv("GEMINI_SCRIPT_MODEL", "gemini-2.5-flash")
        self.video_model = os.getenv("GEMINI_VIDEO_MODEL", "veo-3.1-generate-001")
        self.image_model = os.getenv("GEMINI_IMAGE_MODEL", "imagen-4.0-fast-generate-001")
        self.text_model = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
        self.lyria_model = os.getenv("GEMINI_MUSIC_MODEL", "lyria-002")
        self.video_only_mode = os.getenv("VIDEO_ONLY_MODE", "true").strip().lower() != "false"
        try:
            self.video_render_concurrency = max(
                1,
                int(os.getenv("VIDEO_RENDER_CONCURRENCY", "3")),
            )
        except ValueError:
            self.video_render_concurrency = 3

        self._storage_client: Optional["storage.Client"] = None
        self._video_guard = asyncio.Semaphore(self.video_render_concurrency)
        self._image_guard = asyncio.Semaphore(1)

        self.live_tools = [
            {
                "function_declarations": [
                    {
                        "name": "queue_scene_video",
                        "description": "Queue a cinematic video shot for the current narration beat.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "prompt": {"type": "STRING"},
                            },
                            "required": ["prompt"],
                        },
                    },
                    {
                        "name": "switch_music_mood",
                        "description": "Update the background score mood for this narrative segment.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "prompt": {"type": "STRING"},
                            },
                            "required": ["prompt"],
                        },
                    },
                ],
            }
        ]

    async def _send(self, websocket: WebSocket, payload: dict):
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as send_error:
            print(f"WebSocket send failed: {send_error}")

    async def _send_error(self, websocket: WebSocket, message: str):
        await self._send(websocket, {"type": "error", "content": message})

    def _get_storage_client(self):
        if not self.project_id:
            raise RuntimeError(
                "Cloud Storage download requires project-scoped auth. "
                "API-key express mode cannot download gs:// artifacts."
            )
        if self._storage_client is not None:
            return self._storage_client
        if storage is None:
            raise RuntimeError(
                "google-cloud-storage is required for Vertex media downloads. "
                "Install it with: pip install google-cloud-storage"
            )
        self._storage_client = storage.Client(project=self.project_id)
        return self._storage_client

    @staticmethod
    def _parse_gcs_uri(gcs_uri: str) -> Tuple[str, str]:
        if not gcs_uri.startswith("gs://"):
            raise ValueError(f"Not a valid gs:// uri: {gcs_uri}")

        without_scheme = gcs_uri[5:]
        bucket, _, blob_path = without_scheme.partition("/")
        if not bucket or not blob_path:
            raise ValueError(f"Invalid gs:// uri: {gcs_uri}")
        return bucket, blob_path

    def _download_gcs_uri_bytes(self, gcs_uri: str) -> bytes:
        bucket_name, blob_path = self._parse_gcs_uri(gcs_uri)
        storage_client = self._get_storage_client()
        blob = storage_client.bucket(bucket_name).blob(blob_path)
        return blob.download_as_bytes()

    async def _wait_for_video_operation(self, operation, websocket: Optional[WebSocket] = None):
        elapsed = 0
        while not operation.done and elapsed < self.VIDEO_POLL_TIMEOUT_SECONDS:
            await asyncio.sleep(self.VIDEO_POLL_INTERVAL_SECONDS)
            elapsed += self.VIDEO_POLL_INTERVAL_SECONDS
            operation = await asyncio.to_thread(
                self.client.operations.get,
                operation=operation,
            )
            if websocket and elapsed % 15 == 0:
                await self._send(
                    websocket,
                    {
                        "type": "status",
                        "content": f"[Video Agent] Rendering in progress ({elapsed}s)...",
                    },
                )

        if not operation.done:
            raise TimeoutError(
                f"Video operation timed out after {self.VIDEO_POLL_TIMEOUT_SECONDS}s"
            )
        if operation.error:
            raise RuntimeError(str(operation.error))

        return operation

    async def _get_video_bytes(self, generated_video) -> Optional[bytes]:
        video_obj = getattr(generated_video, "video", None)
        if not video_obj:
            return None

        video_bytes = getattr(video_obj, "video_bytes", None)
        if video_bytes:
            return video_bytes

        video_uri = getattr(video_obj, "uri", None)
        if video_uri and video_uri.startswith("gs://"):
            return await asyncio.to_thread(self._download_gcs_uri_bytes, video_uri)

        # Gemini Developer fallback (kept for local compatibility).
        try:
            return await asyncio.to_thread(
                self.client.files.download,
                file=generated_video,
            )
        except Exception:
            return None

    async def _get_image_bytes(self, generated_image) -> Optional[bytes]:
        image_obj = getattr(generated_image, "image", None)
        if not image_obj:
            return None

        image_bytes = getattr(image_obj, "image_bytes", None)
        if image_bytes:
            return image_bytes

        gcs_uri = getattr(image_obj, "gcs_uri", None)
        if gcs_uri and gcs_uri.startswith("gs://"):
            return await asyncio.to_thread(self._download_gcs_uri_bytes, gcs_uri)

        return None

    @staticmethod
    def _default_story_blueprint(topic: str) -> Dict[str, Any]:
        return {
            "title": topic,
            "narration_outline": [
                (
                    f"What if everything you knew about {topic} was only the surface? "
                    f"Scientists stare at {topic} and find a phenomenon so extreme it defies intuition."
                ),
                (
                    f"{topic} is measurable and predictable. At its core, one vivid analogy unlocks everything — "
                    "and that is where we begin."
                ),
                (
                    f"Here is how {topic} actually works: cause leads to effect in a chain "
                    "that, once seen, cannot be unseen."
                ),
                (
                    f"And so we return to the question we began with, but now see it differently. "
                    f"What does {topic} tell us about the nature of reality? The universe is still speaking."
                ),
            ],
            "scene_cues": [
                {"kind": "video", "prompt": f"Dramatic cinematic opening shot of {topic}, ultra-wide lens, golden hour lighting, slow push-in"},
                {"kind": "video", "prompt": f"Close-up documentary footage introducing the fundamental nature of {topic}, macro detail"},
                {"kind": "video", "prompt": f"Dynamic cinematic sequence showing the mechanism of {topic} in action, slow motion"},
                {"kind": "video", "prompt": f"Transcendent wide cinematic final shot, {topic} at cosmic or universal scale, reflective mood"},
            ],
            "image_cues": [
                {"kind": "image", "prompt": f"Photorealistic dramatic still of {topic}, cinematic lighting, documentary quality"},
                {"kind": "image", "prompt": f"High-detail scientific illustration of the core structure of {topic}"},
                {"kind": "image", "prompt": f"Cinematic still showing the mechanism of {topic} in vivid detail"},
                {"kind": "image", "prompt": f"Ethereal cosmic or philosophical wide still connecting {topic} to the universe"},
            ],
            "beat_durations": [15, 15, 15, 15],
            "music_prompt": "cinematic orchestral documentary score, rising strings, subtle tension, no vocals, epic scope",
        }

    def _normalize_story_blueprint(self, topic: str, raw: Any) -> Dict[str, Any]:
        fallback = self._default_story_blueprint(topic)
        if not isinstance(raw, dict):
            return fallback

        title = str(raw.get("title") or fallback["title"]).strip() or fallback["title"]

        narration_outline: List[str] = []
        scene_cues: List[Dict[str, str]] = []
        image_cues: List[Dict[str, str]] = []
        beat_durations: List[int] = []

        documentary_flow_raw = raw.get("documentary_flow")
        if isinstance(documentary_flow_raw, list) and documentary_flow_raw:
            for beat in documentary_flow_raw:
                if not isinstance(beat, dict):
                    continue
                narration = str(beat.get("narration_script") or beat.get("narration") or "").strip()
                video_prompt = str(beat.get("visual_prompt_veo") or beat.get("video_prompt") or "").strip()
                image_prompt = str(beat.get("fallback_image_prompt") or beat.get("image_prompt") or "").strip()
                if not narration or not video_prompt:
                    continue
                narration_outline.append(narration)
                scene_cues.append({"kind": "video", "prompt": video_prompt})
                if image_prompt:
                    image_cues.append({"kind": "image", "prompt": image_prompt})
                duration_raw = beat.get("target_duration_seconds", 15)
                try:
                    duration_val = int(duration_raw)
                except Exception:
                    duration_val = 15
                beat_durations.append(max(15, min(duration_val, 60)))

        if not narration_outline:
            narration_outline_raw = raw.get("narration_outline")
            if isinstance(narration_outline_raw, list):
                for item in narration_outline_raw:
                    if isinstance(item, str) and item.strip():
                        narration_outline.append(item.strip())
        if not narration_outline:
            narration_outline = fallback["narration_outline"]

        if not scene_cues:
            scene_cues_raw = raw.get("scene_cues")
            if isinstance(scene_cues_raw, list):
                for cue in scene_cues_raw:
                    if not isinstance(cue, dict):
                        continue
                    kind = str(cue.get("kind") or "").strip().lower()
                    prompt = str(cue.get("prompt") or "").strip()
                    if kind == "image":
                        kind = "video"
                    if kind == "video" and prompt:
                        scene_cues.append({"kind": kind, "prompt": prompt})
        if not scene_cues:
            scene_cues = fallback["scene_cues"]

        if not image_cues:
            image_cues_raw = raw.get("image_cues")
            if isinstance(image_cues_raw, list):
                for cue in image_cues_raw:
                    if not isinstance(cue, dict):
                        continue
                    prompt = str(cue.get("prompt") or "").strip()
                    if prompt:
                        image_cues.append({"kind": "image", "prompt": prompt})
        if not image_cues:
            for cue in scene_cues:
                image_cues.append(
                    {
                        "kind": "image",
                        "prompt": (
                            "Photorealistic documentary still, cinematic lighting, no text overlay. "
                            + str(cue.get("prompt") or "")
                        ).strip(),
                    }
                )

        if not beat_durations:
            beat_durations_raw = raw.get("beat_durations")
            if isinstance(beat_durations_raw, list):
                for item in beat_durations_raw:
                    try:
                        beat_durations.append(max(15, min(int(item), 60)))
                    except Exception:
                        beat_durations.append(15)
        if not beat_durations:
            beat_durations = list(fallback.get("beat_durations", []))
        if not beat_durations:
            beat_durations = [15] * len(narration_outline)

        music_prompt = str(raw.get("music_prompt") or "").strip() or fallback["music_prompt"]

        return {
            "title": title,
            "narration_outline": narration_outline,
            "scene_cues": scene_cues,
            "image_cues": image_cues,
            "beat_durations": beat_durations,
            "music_prompt": music_prompt,
        }

    @staticmethod
    def _extract_json_object_text(raw_text: str) -> Optional[str]:
        if not raw_text:
            return None

        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw_text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            return fenced.group(1).strip()

        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return raw_text[start : end + 1].strip()
        return None

    @staticmethod
    def _parse_json_loose(raw_text: str) -> Optional[Dict[str, Any]]:
        if not raw_text:
            return None

        candidates = [raw_text]
        extracted = ChronosAgent._extract_json_object_text(raw_text)
        if extracted and extracted != raw_text:
            candidates.append(extracted)

        for candidate in candidates:
            cleaned = candidate.strip()
            if not cleaned:
                continue
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

            # Common LLM JSON error: trailing commas.
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
        return None

    async def build_story_blueprint(self, topic: str) -> Dict[str, Any]:
        prompt = (
            "You are the Script Writer + Director planner for a premium multimodal educational documentary.\n"
            "Return valid JSON only with this exact schema:\n"
            "{\n"
            '  "title": "string",\n'
            '  "music_prompt": "string describing cinematic BGM mood",\n'
            '  "documentary_flow": [\n'
            "    {\n"
            '      "beat_id": 1,\n'
            '      "segment_title": "string",\n'
            '      "target_duration_seconds": 15,\n'
            '      "narration_script": "40-50 words of vivid narration",\n'
            '      "visual_prompt_veo": "cinematic video prompt",\n'
            '      "fallback_image_prompt": "photorealistic still prompt"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Constraints:\n"
            "- Generate exactly 4 beats.\n"
            "- Beat progression follows this arc:\n"
            "  1. Hook: Open with a stunning fact or question that grabs attention\n"
            "  2. Foundation: Establish core concepts with vivid real-world analogy\n"
            "  3. Mechanism: Explain HOW it works — one key insight, precisely\n"
            "  4. Reflection: End with a philosophical question that lingers\n"
            "- Each narration_script must be exactly 40-50 words, punchy and vivid.\n"
            "- visual_prompt_veo: cinematic moving-shot language, photorealistic, documentary style.\n"
            "- fallback_image_prompt: dramatic, high-detail, photorealistic still.\n"
            "- target_duration_seconds must be exactly 15.\n"
            "- music_prompt must describe a 1-minute orchestral documentary score mood.\n"
            "- No markdown, no commentary, no trailing commas.\n"
            f"Topic: {topic}"
        )
        try:
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.script_model,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            raw_text = response.text or ""
            parsed = self._parse_json_loose(raw_text)
            if parsed is not None:
                validated = ScriptBlueprint.model_validate(parsed)
                normalized = {
                    "title": validated.title,
                    "music_prompt": validated.music_prompt,
                    "documentary_flow": [beat.model_dump() for beat in validated.documentary_flow],
                }
                return self._normalize_story_blueprint(topic, normalized)

            repair_prompt = (
                "Fix this malformed JSON and return valid JSON only.\n"
                "Required keys: title, music_prompt, documentary_flow.\n"
                "documentary_flow must have exactly 8 objects and each object must contain:\n"
                "beat_id, segment_title, target_duration_seconds (30-40), narration_script (200-250 words), "
                "visual_prompt_veo, fallback_image_prompt.\n"
                f"Malformed JSON:\n{raw_text}"
            )
            repair_response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.script_model,
                contents=repair_prompt,
                config={"response_mime_type": "application/json"},
            )
            repaired = self._parse_json_loose(repair_response.text or "")
            if repaired is not None:
                validated = ScriptBlueprint.model_validate(repaired)
                normalized = {
                    "title": validated.title,
                    "music_prompt": validated.music_prompt,
                    "documentary_flow": [beat.model_dump() for beat in validated.documentary_flow],
                }
                return self._normalize_story_blueprint(topic, normalized)
            raise ValueError("Script writer returned non-JSON content after repair attempt.")
        except ValidationError as e:
            print(f"Script writer schema validation fallback: {e}")
            return self._default_story_blueprint(topic)
        except Exception as e:
            print(f"Script writer fallback: {e}")
            return self._default_story_blueprint(topic)

    @staticmethod
    def _render_story_packet(blueprint: Dict[str, Any]) -> str:
        outline = blueprint.get("narration_outline", [])
        cues = blueprint.get("scene_cues", [])

        outline_lines = []
        for idx, beat in enumerate(outline, start=1):
            outline_lines.append(f"{idx}. {beat}")

        cue_lines = []
        for idx, cue in enumerate(cues, start=1):
            cue_lines.append(f"{idx}. [{cue.get('kind', 'image')}] {cue.get('prompt', '')}")

        return (
            f"Title: {blueprint.get('title', '')}\n"
            "Narration beats:\n"
            + "\n".join(outline_lines)
            + "\nVisual cues:\n"
            + "\n".join(cue_lines)
            + f"\nMusic mood: {blueprint.get('music_prompt', '')}"
        )

    @staticmethod
    def _extract_first_audio_base64(payload: Any) -> Optional[str]:
        if isinstance(payload, dict):
            for key in ("audioContent", "bytesBase64Encoded", "audio"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            for value in payload.values():
                found = ChronosAgent._extract_first_audio_base64(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = ChronosAgent._extract_first_audio_base64(item)
                if found:
                    return found
        return None

    def _generate_lyria_audio_bytes(self, prompt: str) -> Optional[bytes]:
        if not self.project_id or not self.location:
            return None

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(GoogleAuthRequest())
        access_token = credentials.token
        if not access_token:
            return None

        endpoint = (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/"
            f"{self.project_id}/locations/{self.location}/publishers/google/models/"
            f"{self.lyria_model}:predict"
        )
        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1},
        }
        req = urllib_request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib_request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib_error.HTTPError as e:
            details = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Lyria predict HTTP {e.code}: {details}") from e

        encoded_audio = self._extract_first_audio_base64(body)
        if not encoded_audio:
            return None
        return base64.b64decode(encoded_audio)

    async def generate_background_score_audio(self, prompt: str, websocket: WebSocket):
        if not prompt.strip():
            return

        await self._send(websocket, {"type": "music_prompt", "content": prompt.strip()})
        if not self.project_id:
            await self._send(
                websocket,
                {
                    "type": "status",
                    "content": "Lyria requires project-scoped auth. Using local ambient fallback track.",
                },
            )
            await self._send(websocket, {"type": "bgm_fallback"})
            return

        # Start a fallback bed immediately so ambience is in sync even while Lyria renders.
        await self._send(
            websocket,
            {
                "type": "status",
                "content": "Starting ambient bed while Lyria composes a custom score...",
            },
        )
        await self._send(websocket, {"type": "bgm_fallback"})

        try:
            await self._send(websocket, {"type": "status", "content": "Composing background score with Lyria..."})
            async with asyncio.timeout(45):
                audio_bytes = await asyncio.to_thread(self._generate_lyria_audio_bytes, prompt)
            if not audio_bytes:
                await self._send(
                    websocket,
                    {
                        "type": "status",
                        "content": "Lyria returned no audio payload. Keeping the ambient bed.",
                    },
                )
                return

            await self._send(
                websocket,
                {
                    "type": "bgm_audio",
                    "data": base64.b64encode(audio_bytes).decode("utf-8"),
                    "mime_type": "audio/wav",
                },
            )
            await self._send(websocket, {"type": "status", "content": "Lyria score is now synced and active."})
        except Exception as e:
            print(f"Lyria generation failed: {e}")
            await self._send(
                websocket,
                {
                    "type": "status",
                    "content": "Lyria generation failed. Continuing with the ambient bed.",
                },
            )

    @staticmethod
    def _spawn_task(task_set: Set[asyncio.Task], coro, label: str) -> asyncio.Task:
        task = asyncio.create_task(coro)
        task_set.add(task)

        def _on_done(done_task: asyncio.Task):
            task_set.discard(done_task)
            with suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc:
                    print(f"{label} task failed: {exc}")

        task.add_done_callback(_on_done)
        return task

    async def _wait_with_story_updates(
        self,
        *,
        websocket: WebSocket,
        tasks: List[asyncio.Task],
        updates: List[str],
        timeout_seconds: int,
    ) -> bool:
        pending = {task for task in tasks if task and not task.done()}
        if not pending:
            return True

        if not updates:
            updates = ["Calibrating your cinematic sequence..."]

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        update_idx = 0

        while pending:
            if loop.time() >= deadline:
                return False

            await self._send(
                websocket,
                {"type": "status", "content": updates[update_idx % len(updates)]},
            )
            update_idx += 1

            remaining = max(0.0, deadline - loop.time())
            wait_slice = min(4.0, remaining)
            done, still_pending = await asyncio.wait(
                pending,
                timeout=wait_slice,
                return_when=asyncio.FIRST_COMPLETED,
            )
            pending = still_pending

            for task in done:
                with suppress(asyncio.CancelledError):
                    _ = task.exception()

        return True

    async def _handle_live_function_calls(
        self,
        *,
        function_calls,
        session,
        websocket: WebSocket,
        task_set: Set[asyncio.Task],
        seen_call_ids: Set[str],
        send_lock: Optional[asyncio.Lock] = None,
    ):
        responses = []
        for function_call in function_calls:
            call_id = str(getattr(function_call, "id", "") or "")
            name = str(getattr(function_call, "name", "") or "")
            args = dict(getattr(function_call, "args", {}) or {})
            prompt = str(args.get("prompt") or "").strip()

            if call_id and call_id in seen_call_ids:
                response = {"name": name, "response": {"status": "duplicate_ignored"}}
                if call_id:
                    response["id"] = call_id
                responses.append(response)
                continue
            if call_id:
                seen_call_ids.add(call_id)

            if name == "queue_scene_video":
                if prompt:
                    self._spawn_task(
                        task_set,
                        self.generate_video_scene(prompt, websocket),
                        "video",
                    )
                    result = {"status": "queued", "prompt": prompt}
                else:
                    result = {"status": "error", "message": "Missing prompt"}
            elif name == "queue_scene_image":
                if prompt:
                    normalized_prompt = (
                        "Video-only mode: cinematic moving shot, documentary realism. "
                        + prompt
                    )
                    self._spawn_task(
                        task_set,
                        self.generate_video_scene(normalized_prompt, websocket),
                        "video",
                    )
                    result = {
                        "status": "queued_as_video",
                        "prompt": normalized_prompt,
                    }
                else:
                    result = {"status": "error", "message": "Missing prompt"}
            elif name == "switch_music_mood":
                if prompt:
                    self._spawn_task(
                        task_set,
                        self.generate_background_score_audio(prompt, websocket),
                        "music",
                    )
                    result = {"status": "queued", "prompt": prompt}
                else:
                    result = {"status": "error", "message": "Missing prompt"}
            else:
                result = {"status": "error", "message": f"Unknown function: {name}"}

            response = {"name": name, "response": result}
            if call_id:
                response["id"] = call_id
            responses.append(response)

        if responses:
            if send_lock:
                async with send_lock:
                    await session.send_tool_response(function_responses=responses)
            else:
                await session.send_tool_response(function_responses=responses)

    async def _render_video_payload(
        self,
        prompt: str,
        websocket: WebSocket,
        *,
        status_prefix: str = "[Video Agent]",
    ) -> Optional[Dict[str, Any]]:
        """Generate a cinematic shot with Veo and return a websocket payload."""
        async with self._video_guard:
            try:
                await self._send(
                    websocket,
                    {"type": "status", "content": f"{status_prefix} Rendering cinematic shot: {prompt[:70]}..."},
                )

                config = genai.types.GenerateVideosConfig(number_of_videos=1)
                if self.video_output_gcs_uri:
                    config.output_gcs_uri = self.video_output_gcs_uri

                prompt_candidates = [
                    (
                        "Documentary footage, cinematic composition, no text overlay, no logos, "
                        "no graphic content. " + prompt.strip()
                    ),
                    (
                        "Clean cinematic establishing shot for educational documentary, no text overlay. "
                        + prompt.strip()
                    ),
                ]
                last_error: Optional[Exception] = None

                for attempt, candidate_prompt in enumerate(prompt_candidates, start=1):
                    await self._send(
                        websocket,
                        {
                            "type": "status",
                            "content": f"{status_prefix} Attempt {attempt}/{len(prompt_candidates)} in progress...",
                        },
                    )
                    try:
                        operation = await asyncio.to_thread(
                            self.client.models.generate_videos,
                            model=self.video_model,
                            prompt=candidate_prompt,
                            config=config,
                        )
                        operation = await self._wait_for_video_operation(operation, websocket=websocket)
                        result = operation.result or operation.response
                        generated_videos = getattr(result, "generated_videos", None) or []
                        if not generated_videos:
                            raise RuntimeError("Veo returned no generated videos.")

                        for generated_video in generated_videos:
                            video_obj = getattr(generated_video, "video", None)
                            video_bytes = await self._get_video_bytes(generated_video)
                            if not video_bytes:
                                continue
                            return {
                                "type": "video",
                                "data": base64.b64encode(video_bytes).decode("utf-8"),
                                "mime_type": getattr(video_obj, "mime_type", None) or "video/mp4",
                                "uri": getattr(video_obj, "uri", None),
                            }

                        raise RuntimeError("Veo completed but no downloadable video bytes were found.")
                    except Exception as attempt_error:
                        error_str = str(attempt_error)
                        # Code 3 = content policy violation — swap in a safe generic prompt and continue
                        if "'code': 3" in error_str or '"code": 3' in error_str or "usage guidelines" in error_str:
                            print(f"Veo attempt {attempt} prompt policy violation — using safe fallback prompt")
                            config = genai.types.GenerateVideosConfig(number_of_videos=1)
                            if self.video_output_gcs_uri:
                                config.output_gcs_uri = self.video_output_gcs_uri
                            prompt_candidates = ["Cinematic wide establishing shot, nature documentary style, no text."]
                        else:
                            last_error = attempt_error
                        print(f"Veo attempt {attempt} failed: {attempt_error}")

                if last_error:
                    raise last_error
                raise RuntimeError("Veo did not produce a valid output.")
            except Exception as e:
                print(f"Video generation failed: {e}")
                if self.video_only_mode:
                    await self._send(
                        websocket,
                        {
                            "type": "status",
                            "content": (
                                f"{status_prefix} Shot unavailable right now. "
                                "Continuing on current visual while retrying next beat."
                            ),
                        },
                    )
                    return None

                await self._send(
                    websocket,
                    {
                        "type": "status",
                        "content": f"{status_prefix} Video unavailable. Falling back to Image Agent.",
                    },
                )
                await self.generate_image_scene(
                    f"Cinematic wide shot, documentary style: {prompt}",
                    websocket,
                )
                return None

    async def _send_video_payload(
        self,
        websocket: WebSocket,
        payload: Optional[Dict[str, Any]],
        *,
        delivered_status: str = "[Video Agent] Shot delivered.",
    ) -> bool:
        if not payload:
            return False
        await self._send(websocket, payload)
        await self._send(websocket, {"type": "status", "content": delivered_status})
        return True

    async def generate_video_scene(self, prompt: str, websocket: WebSocket):
        """Generate and immediately stream a cinematic shot."""
        payload = await self._render_video_payload(prompt, websocket=websocket)
        await self._send_video_payload(websocket, payload)

    async def _render_image_payload(
        self,
        prompt: str,
        websocket: WebSocket,
        *,
        status_prefix: str = "[Image Agent]",
    ) -> Optional[Dict[str, Any]]:
        async with self._image_guard:
            max_attempts = 4
            for attempt in range(max_attempts):
                try:
                    await self._send(
                        websocket,
                        {"type": "status", "content": f"{status_prefix} Generating storyboard frame: {prompt[:70]}..."},
                    )
                    response = await asyncio.to_thread(
                        self.client.models.generate_images,
                        model=self.image_model,
                        prompt=prompt,
                        config=genai.types.GenerateImagesConfig(number_of_images=1),
                    )

                    generated_images = getattr(response, "generated_images", None) or []
                    if not generated_images:
                        raise RuntimeError("Imagen returned no images.")

                    for generated_image in generated_images:
                        image_obj = getattr(generated_image, "image", None)
                        image_bytes = await self._get_image_bytes(generated_image)
                        if not image_bytes:
                            continue
                        return {
                            "type": "image",
                            "data": base64.b64encode(image_bytes).decode("utf-8"),
                            "mime_type": getattr(image_obj, "mime_type", None) or "image/png",
                            "uri": getattr(image_obj, "gcs_uri", None),
                        }

                    raise RuntimeError("Imagen completed but no renderable image bytes were found.")
                except Exception as e:
                    error_text = str(e)
                    is_quota = "RESOURCE_EXHAUSTED" in error_text or "429" in error_text
                    print(f"Image generation error (attempt {attempt + 1}/{max_attempts}): {e}")
                    if is_quota and attempt < max_attempts - 1:
                        wait_secs = 5 * (2 ** attempt)  # 5s, 10s, 20s
                        await self._send(
                            websocket,
                            {"type": "status", "content": f"{status_prefix} Quota limit hit — retrying in {wait_secs}s (attempt {attempt + 2}/{max_attempts})..."},
                        )
                        await asyncio.sleep(wait_secs)
                        continue
                    if is_quota:
                        await self._send(
                            websocket,
                            {"type": "status", "content": f"{status_prefix} Quota exhausted after {max_attempts} attempts. Reusing current visual."},
                        )
                    else:
                        await self._send(
                            websocket,
                            {"type": "status", "content": f"{status_prefix} Generation failed. Keeping current visual."},
                        )
                    return None
            return None

    async def _send_image_payload(
        self,
        websocket: WebSocket,
        payload: Optional[Dict[str, Any]],
        *,
        delivered_status: str = "[Image Agent] Storyboard frame delivered.",
    ) -> bool:
        if not payload:
            return False
        await self._send(websocket, payload)
        await self._send(websocket, {"type": "status", "content": delivered_status})
        return True

    async def generate_image_scene(self, prompt: str, websocket: WebSocket):
        """Generate an image with Imagen and stream it to the UI."""
        payload = await self._render_image_payload(prompt, websocket=websocket)
        await self._send_image_payload(websocket, payload)

    async def generate_music_prompt(self, story_context: str) -> str:
        """Creates a background-score descriptor used by the BGM worker."""
        try:
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.text_model,
                contents=(
                    "Write a concise cinematic soundtrack prompt (max 12 words) for this "
                    f"story context: {story_context}"
                ),
            )
            if response.text:
                return response.text.strip()
        except Exception:
            pass
        return "cinematic ambient orchestral strings slow-building atmospheric pulse"

    async def build_quiz(self, topic: str, script_content: str) -> List[Dict[str, Any]]:
        """Generate 5 structured MCQ questions based on documentary content."""
        prompt = (
            f"You are creating a quiz for an educational documentary about: {topic}\n\n"
            "Documentary content summary:\n"
            f"{script_content[:3000]}\n\n"
            "Generate exactly 5 multiple-choice quiz questions. Return valid JSON only:\n"
            "{\n"
            '  "questions": [\n'
            "    {\n"
            '      "question": "string",\n'
            '      "options": ["A. text", "B. text", "C. text", "D. text"],\n'
            '      "correct": "A",\n'
            '      "explanation": "1-2 sentence explanation of the correct answer"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Rules:\n"
            "- Questions must test specific facts from the documentary content\n"
            "- Each question should test a DIFFERENT concept\n"
            "- Options should be plausible but clearly only one is correct\n"
            "- Progress from easier to harder questions\n"
            "- Explanations reinforce learning\n"
            "- No markdown, valid JSON only"
        )
        try:
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.text_model,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            parsed = self._parse_json_loose(response.text or "")
            if parsed and isinstance(parsed.get("questions"), list):
                questions = parsed["questions"]
                valid = []
                for q in questions:
                    if (
                        isinstance(q, dict)
                        and q.get("question")
                        and isinstance(q.get("options"), list)
                        and len(q["options"]) >= 3
                        and q.get("correct")
                        and q.get("explanation")
                    ):
                        valid.append(q)
                return valid[:5]
        except Exception as e:
            print(f"Quiz generation failed: {e}")
        return []

    @staticmethod
    def _build_story_beats(blueprint: Dict[str, Any]) -> List[Dict[str, Any]]:
        beats: List[Dict[str, Any]] = []
        outlines = [
            str(item).strip()
            for item in blueprint.get("narration_outline", [])
            if isinstance(item, str) and item.strip()
        ]
        cues = [
            cue for cue in blueprint.get("scene_cues", [])
            if isinstance(cue, dict) and str(cue.get("prompt", "")).strip()
        ]
        image_cues = [
            cue for cue in blueprint.get("image_cues", [])
            if isinstance(cue, dict) and str(cue.get("prompt", "")).strip()
        ]
        durations = []
        for raw_duration in blueprint.get("beat_durations", []):
            try:
                durations.append(max(15, min(int(raw_duration), 60)))
            except Exception:
                durations.append(24)
        if not outlines:
            return beats
        if not cues:
            cues = [{"kind": "video", "prompt": f"Cinematic documentary shot: {outlines[0]}"}]
        if not image_cues:
            image_cues = [
                {
                    "kind": "image",
                    "prompt": f"Photorealistic documentary still of {outlines[0]}",
                }
            ]

        for idx, narration in enumerate(outlines):
            cue = cues[idx % len(cues)]
            image_cue = image_cues[idx % len(image_cues)]
            beats.append(
                {
                    "narration": narration,
                    "video_prompt": str(cue.get("prompt", "")).strip(),
                    "image_prompt": str(image_cue.get("prompt", "")).strip(),
                    "target_duration_seconds": durations[idx % len(durations)] if durations else 15,
                }
            )
        return beats

    @staticmethod
    def _is_visual_request(text: str) -> bool:
        lowered = text.lower()
        patterns = [
            r"\bshow me\b",
            r"\bclose[- ]?up\b",
            r"\bzoom in\b",
            r"\bcan i see\b",
            r"\bfocus on\b",
            r"\bpan to\b",
            r"\blook at\b",
        ]
        return any(re.search(pattern, lowered) for pattern in patterns)

    @staticmethod
    def _normalize_visual_request_prompt(user_text: str, topic: str) -> str:
        cleaned = user_text.strip().rstrip(".")
        return (
            "Cinematic documentary moving shot, high realism, no text overlay, no logos. "
            f"Topic context: {topic}. Requested shot: {cleaned}"
        )

    @staticmethod
    def _extract_input_transcript_text(server_content: Any) -> Optional[str]:
        candidates = (
            getattr(server_content, "input_transcription", None),
            getattr(server_content, "input_audio_transcription", None),
        )
        for candidate in candidates:
            text = getattr(candidate, "text", None) if candidate else None
            if isinstance(text, str) and text.strip():
                return text.strip()
        return None

    @staticmethod
    def _build_beat_prompt(
        *,
        topic: str,
        title: str,
        beat_index: int,
        beat_count: int,
        beat: Dict[str, Any],
        user_name: str,
    ) -> str:
        personalization = (
            f"The viewer's name is {user_name}. Address them warmly by name once in this beat."
            if user_name
            else ""
        )
        # Beat arc labels for context
        beat_arc = {
            1: "HOOK — grab attention with a stunning fact",
            2: "FOUNDATION — lay down core concepts",
            3: "MECHANISM — explain how it works in detail",
            4: "SCALE — show the full scope and complexity",
            5: "COUNTERINTUITIVE — reveal the surprising twist",
            6: "HUMAN CONNECTION — link to history or culture",
            7: "FRONTIER — describe cutting-edge research",
            8: "REFLECTION — philosophical closing thought",
        }
        arc_label = beat_arc.get(beat_index, f"Beat {beat_index}")
        # Add a brief pause/reflection cue on beats 4 and 7
        pacing_cue = (
            "Pause for one breath mid-beat before the second half."
            if beat_index in {4, 7}
            else ""
        )
        return (
            f"Documentary title: {title}\n"
            f"Topic: {topic}\n"
            f"Beat {beat_index}/{beat_count} — {arc_label}\n"
            f"Target duration: {beat.get('target_duration_seconds', 15)} seconds\n"
            f"Visual cue: {beat.get('video_prompt', '')}\n\n"
            f"Narration (deliver word-for-word, with full dramatic pacing):\n"
            f"{beat.get('narration', '')}\n\n"
            "CRITICAL: Read every word of the narration above. Do NOT shorten or skip.\n"
            "Speak with documentary gravitas — measured pace, vivid emphasis.\n"
            "Do not call any tools unless the user explicitly asks to change visuals or music.\n"
            f"{pacing_cue}\n"
            f"{personalization}"
        ).strip()

    @staticmethod
    def _build_follow_up_prompt(topic: str, user_name: str) -> str:
        viewer = user_name if user_name else "viewer"
        return (
            f"The documentary about {topic} has just finished.\n"
            f"Address the viewer as '{viewer}'.\n"
            "Say a warm 2-3 sentence closing reflection that connects the topic to the "
            "viewer's everyday life or a sense of wonder.\n"
            "Then invite them to explore the quiz questions displayed on screen.\n"
            "Keep it brief — no more than 30 seconds of speaking."
        )

    async def start_session(self, websocket: WebSocket, initial_message: Optional[dict] = None):
        config = {
            "system_instruction": {
                "parts": [{
                    "text": """You are Chronos Delegator, a live creative director in an agentic multimodal system.

ROLE:
- Speak to the user with natural, cinematic narration.
- Coordinate specialist worker agents via tool calls:
  - queue_scene_video(prompt)
  - switch_music_mood(prompt)

INTERLEAVING RULES:
- Treat each narration segment as one beat tied to one visual cue.
- Keep language visually grounded in the currently shown shot.
- Use tool calls only when the user explicitly requests a visual/music change.
- Never read tool names/prompts aloud.

STYLE:
- Documentary tone, vivid but concise.
- Prioritize clarity, momentum, and educational value.
- For full topic runs, target a 1-2 minute educational experience with progressive depth.""",
                }],
            },
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": "Aoede"},
                },
            },
            "output_audio_transcription": {},
            "tools": self.live_tools,
        }

        try:
            async with self.client.aio.live.connect(model=self.live_model, config=config) as session:
                await self._send(
                    websocket,
                    {
                        "type": "status",
                        "content": (
                            f"Vertex Live connected ({self.project_id or 'express'}/{self.location or 'global'}) "
                            f"with model {self.live_model} via {self.auth_mode}"
                        ),
                    },
                )
                await self._send(
                    websocket,
                    {"type": "status", "content": "Delegator + Script + Video + Music agents ready (video-first mode)"},
                )

                active_tasks: Set[asyncio.Task] = set()
                seen_call_ids: Set[str] = set()
                output_transcript_state = ""
                current_topic = ""
                current_user_name = ""
                story_task: Optional[asyncio.Task] = None
                turn_complete_event = asyncio.Event()
                session_send_lock = asyncio.Lock()

                async def send_turn_input(input_text: str):
                    async with session_send_lock:
                        await session.send(input=input_text, end_of_turn=True)

                async def wait_for_turn_complete(timeout_seconds: int = 90) -> bool:
                    try:
                        await asyncio.wait_for(turn_complete_event.wait(), timeout=timeout_seconds)
                        turn_complete_event.clear()
                        return True
                    except TimeoutError:
                        return False

                async def run_story(topic: str, user_name: str):
                    nonlocal current_topic, current_user_name
                    current_topic = topic
                    current_user_name = user_name

                    # ── Visual generation helpers ────────────────────────────
                    async def render_image_for_beat(beat_idx: int, beat: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                        """Generate a still image for a beat (fast path: ~10-15s).
                        Returns the payload dict; also self-delivers via WebSocket when done."""
                        image_prompt = beat.get("image_prompt") or (
                            "Photorealistic documentary still. " + (beat.get("video_prompt") or "")
                        )
                        payload = await self._render_image_payload(
                            image_prompt,
                            websocket=websocket,
                            status_prefix=f"[Image][Beat {beat_idx + 1}]",
                        )
                        if payload:
                            payload["beat_index"] = beat_idx
                            await self._send(websocket, payload)
                        return payload

                    async def render_video_for_beat(beat_idx: int, beat: Dict[str, Any]):
                        """Generate a Veo video for a beat (slow path: 60-180s).
                        Self-delivers via WebSocket when done — upgrades the image for that beat."""
                        video_prompt = beat.get("video_prompt") or f"Cinematic shot for {topic}"
                        payload = await self._render_video_payload(
                            video_prompt,
                            websocket=websocket,
                            status_prefix=f"[Video][Beat {beat_idx + 1}]",
                        )
                        if payload:
                            payload["beat_index"] = beat_idx
                            await self._send(websocket, payload)
                            await self._send(
                                websocket,
                                {"type": "status", "content": f"[Video] Beat {beat_idx + 1} cinematic clip delivered — upgrading scene."},
                            )

                    try:
                        await self._send(websocket, {"type": "status", "content": "[Script Agent] Drafting your narrative arc..."})
                        blueprint = await self.build_story_blueprint(topic)
                        if not blueprint.get("music_prompt"):
                            blueprint["music_prompt"] = await self.generate_music_prompt(topic)

                        beats = self._build_story_beats(blueprint)
                        if not beats:
                            beats = [
                                {
                                    "narration": f"Cinematic introduction to {topic}.",
                                    "video_prompt": f"Cinematic opening shot for {topic}",
                                    "image_prompt": f"Photorealistic documentary still of {topic}",
                                    "target_duration_seconds": 15,
                                }
                            ]
                        target_beats = 4
                        if len(beats) < target_beats:
                            fallback = self._build_story_beats(self._default_story_blueprint(topic))
                            source = fallback or beats
                            while source and len(beats) < target_beats:
                                template = source[len(beats) % len(source)]
                                beats.append(
                                    {
                                        "narration": template.get("narration", f"Explore {topic} further."),
                                        "video_prompt": template.get("video_prompt", f"Cinematic shot about {topic}"),
                                        "image_prompt": template.get("image_prompt", f"Photorealistic still of {topic}"),
                                        "target_duration_seconds": template.get("target_duration_seconds", 15),
                                    }
                                )
                        if len(beats) > 10:
                            beats = beats[:10]

                        title = str(blueprint.get("title") or topic).strip() or topic
                        await self._send(websocket, {"type": "topic_received", "content": topic, "title": title})
                        await self._send(
                            websocket,
                            {"type": "status", "content": f"[Script Agent] '{title}' — {len(beats)}-beat documentary ready."},
                        )

                        # ── Phase 1: Fire all image renders immediately (fast, ~10-15s each) ──
                        # Images are the primary sync element. They arrive before narration starts.
                        # Veo videos fire in background and silently upgrade each beat's image when ready.
                        await self._send(websocket, {"type": "status", "content": "[Studio] Pre-rendering all scenes before narration begins..."})

                        image_tasks: Dict[int, asyncio.Task] = {}
                        for beat_idx, beat in enumerate(beats):
                            image_tasks[beat_idx] = self._spawn_task(
                                active_tasks,
                                render_image_for_beat(beat_idx, beat),
                                f"image-beat-{beat_idx + 1}",
                            )

                        # Veo videos run in background — each self-delivers and upgrades the image
                        for beat_idx, beat in enumerate(beats):
                            self._spawn_task(
                                active_tasks,
                                render_video_for_beat(beat_idx, beat),
                                f"video-beat-{beat_idx + 1}",
                            )

                        # BGM fires in background, fallback plays immediately
                        self._spawn_task(
                            active_tasks,
                            self.generate_background_score_audio(blueprint.get("music_prompt", ""), websocket),
                            "music",
                        )

                        # ── Phase 2: Hold narration until beat 0's image is ready ──
                        # This is the "cinematic curtain rise" — we wait for the first frame
                        # before the narrator speaks. Max wait: 28s.
                        # Subsequent beats: images pre-generate while the previous beat narrates,
                        # so we only need a short (≤6s) catch-up wait before each beat.
                        beat0_task = image_tasks.get(0)
                        if beat0_task and not beat0_task.done():
                            await self._send(
                                websocket,
                                {"type": "status", "content": "[Studio] Composing opening scene... narration starts when first frame is ready."},
                            )
                            try:
                                async with asyncio.timeout(28):
                                    await asyncio.shield(beat0_task)
                            except (asyncio.TimeoutError, asyncio.CancelledError):
                                await self._send(
                                    websocket,
                                    {"type": "status", "content": "[Studio] Opening scene delayed — starting narration with placeholder visual."},
                                )

                        await self._send(websocket, {"type": "status", "content": "[Studio] Scene locked. Rolling documentary."})

                        # ── Phase 3: Sequential narration — each beat waits briefly for its image ──
                        for beat_index, beat in enumerate(beats):
                            # Ensure this beat's image is ready before the narrator speaks.
                            # For beat 0: already handled above.
                            # For beats 1+: image was generating during previous beat's narration (~35s),
                            # so it should already be done. Short catch-up wait as safety net.
                            if beat_index > 0:
                                img_task = image_tasks.get(beat_index)
                                if img_task and not img_task.done():
                                    await self._send(
                                        websocket,
                                        {"type": "status", "content": f"[Studio] Loading scene {beat_index + 1}..."},
                                    )
                                    try:
                                        async with asyncio.timeout(6):
                                            await asyncio.shield(img_task)
                                    except (asyncio.TimeoutError, asyncio.CancelledError):
                                        pass

                            await self._send(
                                websocket,
                                {
                                    "type": "beat_start",
                                    "beat_index": beat_index,
                                    "total_beats": len(beats),
                                    "title": title,
                                    "target_duration_seconds": beat.get("target_duration_seconds", 15),
                                },
                            )

                            beat_prompt = self._build_beat_prompt(
                                topic=topic,
                                title=title,
                                beat_index=beat_index + 1,
                                beat_count=len(beats),
                                beat=beat,
                                user_name=user_name,
                            )
                            turn_complete_event.clear()
                            try:
                                await send_turn_input(beat_prompt)
                            except Exception as send_err:
                                print(f"Beat {beat_index + 1} session error: {send_err}")
                                await self._send(websocket, {"type": "status", "content": f"[Studio] Session interrupted at beat {beat_index + 1} — ending documentary early."})
                                break
                            got_turn = await wait_for_turn_complete(timeout_seconds=30)
                            if not got_turn:
                                await self._send(
                                    websocket,
                                    {"type": "status", "content": f"[Delegator] Beat {beat_index + 1} timed out. Advancing."},
                                )

                            await self._send(websocket, {"type": "beat_end", "beat_index": beat_index})

                        # ── Phase 4: Quiz generation ──
                        script_content = "\n\n".join(
                            f"Beat {i + 1}: {b.get('narration', '')}" for i, b in enumerate(beats)
                        )
                        await self._send(
                            websocket,
                            {"type": "status", "content": "[Quiz Agent] Generating quiz questions..."},
                        )
                        quiz_questions = await self.build_quiz(topic, script_content)
                        if quiz_questions:
                            await self._send(websocket, {"type": "quiz_data", "questions": quiz_questions, "topic": topic})
                        await self._send(websocket, {"type": "story_complete"})
                        await self._send(
                            websocket,
                            {"type": "status", "content": "[Studio] Documentary complete. Delivering closing reflection."},
                        )
                        turn_complete_event.clear()
                        await send_turn_input(self._build_follow_up_prompt(topic, user_name))
                        _ = await wait_for_turn_complete(timeout_seconds=60)

                    except asyncio.CancelledError:
                        await self._send(
                            websocket,
                            {"type": "status", "content": "[Studio] Story pipeline cancelled."},
                        )
                        raise
                    except Exception as story_error:
                        print(f"Story runner error: {story_error}")
                        await self._send_error(websocket, f"Story generation error: {story_error}")

                async def receive_from_gemini():
                    nonlocal output_transcript_state
                    try:
                        async for message in session.receive():
                            tool_call = getattr(message, "tool_call", None)
                            function_calls = getattr(tool_call, "function_calls", None) if tool_call else None
                            if function_calls:
                                await self._handle_live_function_calls(
                                    function_calls=function_calls,
                                    session=session,
                                    websocket=websocket,
                                    task_set=active_tasks,
                                    seen_call_ids=seen_call_ids,
                                    send_lock=session_send_lock,
                                )

                            server_content = message.server_content
                            if not server_content:
                                continue

                            if server_content.model_turn:
                                for part in server_content.model_turn.parts:
                                    inline_data = getattr(part, "inline_data", None)
                                    if inline_data and inline_data.data:
                                        await self._send(
                                            websocket,
                                            {
                                                "type": "audio_chunk",
                                                "data": base64.b64encode(inline_data.data).decode("utf-8"),
                                            },
                                        )
                                    text_part = getattr(part, "text", None)
                                    if text_part and text_part.strip():
                                        await self._send(websocket, {"type": "narration", "content": text_part})

                            output_transcription = getattr(server_content, "output_transcription", None)
                            transcript_text = getattr(output_transcription, "text", None) if output_transcription else None
                            if transcript_text:
                                delta_text = transcript_text
                                if output_transcript_state and transcript_text.startswith(output_transcript_state):
                                    delta_text = transcript_text[len(output_transcript_state):]
                                output_transcript_state = transcript_text
                                if delta_text.strip():
                                    await self._send(websocket, {"type": "narration", "content": delta_text})

                            if server_content.turn_complete:
                                turn_complete_event.set()
                                output_transcript_state = ""
                    except Exception as e:
                        print(f"Error in Gemini receive loop: {e}")
                        await self._send_error(websocket, f"Live stream receive error: {e}")
                        turn_complete_event.set()  # Unblock any beat waiting for turn completion

                receive_task = asyncio.create_task(receive_from_gemini())

                # If websocket_endpoint peeked at the first message to extract
                # the API key, replay it here so it is processed normally.
                _pending_msg: Optional[dict] = initial_message

                try:
                    while True:
                        if _pending_msg is not None:
                            msg = _pending_msg
                            _pending_msg = None
                        else:
                            data = await websocket.receive_text()
                            try:
                                msg = json.loads(data)
                            except json.JSONDecodeError:
                                await self._send_error(websocket, "Invalid JSON message from client.")
                                continue

                        msg_type = msg.get("type")
                        if msg_type == "start":
                            topic = str(msg.get("topic", "")).strip()
                            if not topic:
                                await self._send_error(websocket, "Topic cannot be empty.")
                                continue
                            user_name = str(msg.get("name", "")).strip()

                            current_topic = topic
                            current_user_name = user_name

                            if story_task and not story_task.done():
                                story_task.cancel()
                                with suppress(asyncio.CancelledError):
                                    await story_task

                            turn_complete_event.clear()
                            story_task = self._spawn_task(
                                active_tasks,
                                run_story(topic, user_name),
                                "story",
                            )
                        # ping — keep-alive from the browser heartbeat, no-op
                except WebSocketDisconnect:
                    print("Client disconnected")
                finally:
                    receive_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await receive_task
                    for task in list(active_tasks):
                        task.cancel()
                    for task in list(active_tasks):
                        with suppress(asyncio.CancelledError):
                            await task
        except TimeoutError as e:
            print(f"Timeout connecting to Gemini Live API: {e}")
            await self._send_error(
                websocket,
                "Failed to connect to Vertex Gemini Live API. Check network, region, model access, and ADC permissions.",
            )
        except Exception as e:
            print(f"Error in live session: {e}")
            await self._send_error(websocket, f"Session error: {str(e)}")


# Resolve server-side credentials from env (may all be None if not configured).
# When a client supplies apiKey in the start message, it takes precedence.
try:
    PROJECT_ID, LOCATION, VERTEX_API_KEY = _resolve_vertex_runtime()
except RuntimeError:
    PROJECT_ID, LOCATION, VERTEX_API_KEY = None, os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1", None


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Read the first message from the client so we can extract the user-supplied
    # API key before constructing the per-connection ChronosAgent.
    try:
        first_data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        return

    try:
        first_msg = json.loads(first_data)
    except json.JSONDecodeError:
        await websocket.send_text(json.dumps({"type": "error", "content": "Invalid JSON in first message."}))
        return

    # User-supplied key takes precedence over server env key.
    client_api_key = str(first_msg.get("apiKey", "")).strip() or None
    effective_api_key = client_api_key or VERTEX_API_KEY

    if not effective_api_key and not PROJECT_ID:
        await websocket.send_text(json.dumps({
            "type": "error",
            "content": "No API key provided. Enter your Gemini API key in the UI.",
        }))
        return

    conn_agent = ChronosAgent(
        project_id=None if (effective_api_key and not PROJECT_ID) else PROJECT_ID,
        location=LOCATION or "us-central1",
        video_output_gcs_uri=os.getenv("VEO_OUTPUT_GCS_URI"),
        api_key=effective_api_key,
    )
    await conn_agent.start_session(websocket, initial_message=first_msg)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        ws="websockets-sansio",
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
