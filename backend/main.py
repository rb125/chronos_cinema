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
    target_duration_seconds: int = Field(default=24, ge=15, le=60)
    narration_script: str
    visual_prompt_veo: str
    fallback_image_prompt: str


class ScriptBlueprint(BaseModel):
    title: str
    music_prompt: str
    documentary_flow: List[ScriptBeat] = Field(min_length=5, max_length=6)


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
        client_kwargs = {
            "vertexai": True,
            "http_options": {"api_version": "v1"},
        }
        if project_id:
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("GEMINI_API_KEY", None)
            client_kwargs["project"] = project_id
        if location and not api_key:
            client_kwargs["location"] = location
        if api_key:
            client_kwargs["api_key"] = api_key

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
        self._image_guard = asyncio.Semaphore(2)

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
                    f"{topic} is often described as mysterious, but the core mechanics are measurable and "
                    "predictable. Start from first principles, define the central phenomenon clearly, and frame "
                    "why scientists care about this topic today."
                ),
                (
                    f"Break down how {topic} works step by step with one concrete analogy and one physical detail. "
                    "Explain what changes over time and what an observer would actually see in a realistic scene."
                ),
                (
                    f"Move from beginner ideas to intermediate mechanics of {topic}, including at least one "
                    "counterintuitive effect and why it happens. Tie the explanation directly to visual evidence."
                ),
                (
                    f"Cover advanced implications of {topic} with scientific rigor, including open questions and "
                    "what current research is trying to resolve. Keep it accurate but accessible."
                ),
                (
                    f"Conclude by connecting {topic} to modern research and future discovery. End with a reflective "
                    "question that encourages the viewer to think deeper."
                ),
            ],
            "scene_cues": [
                {"kind": "video", "prompt": f"Cinematic establishing shot introducing {topic} with realistic scale and atmosphere"},
                {"kind": "video", "prompt": f"Detailed moving close-up explaining the core structure and mechanism of {topic}"},
                {"kind": "video", "prompt": f"Scientific visualization sequence showing intermediate mechanics and cause-effect in {topic}"},
                {"kind": "video", "prompt": f"Advanced documentary sequence linking cutting-edge research questions around {topic}"},
                {"kind": "video", "prompt": f"Wide cinematic finale connecting {topic} to future exploration and discovery"},
            ],
            "image_cues": [
                {"kind": "image", "prompt": f"Photorealistic documentary still of {topic} with dramatic but scientific lighting"},
                {"kind": "image", "prompt": f"High-detail educational close-up frame showing core components of {topic}"},
                {"kind": "image", "prompt": f"Conceptual science illustration frame visualizing intermediate mechanisms in {topic}"},
                {"kind": "image", "prompt": f"Advanced scientific frame showing unresolved research questions about {topic}"},
                {"kind": "image", "prompt": f"Inspirational wide final frame connecting {topic} to future discoveries"},
            ],
            "beat_durations": [24, 24, 24, 24, 24],
            "music_prompt": "cinematic ambient score, wide strings, subtle pulse, no vocals",
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
                duration_raw = beat.get("target_duration_seconds", 24)
                try:
                    duration_val = int(duration_raw)
                except Exception:
                    duration_val = 24
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
                        beat_durations.append(24)
        if not beat_durations:
            beat_durations = list(fallback.get("beat_durations", []))
        if not beat_durations:
            beat_durations = [24] * len(narration_outline)

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
            "You are the Script Writer + Director planner for a multimodal educational documentary.\n"
            "Return valid JSON only with this exact schema:\n"
            "{\n"
            '  "title": "string",\n'
            '  "music_prompt": "string",\n'
            '  "documentary_flow": [\n'
            "    {\n"
            '      "beat_id": 1,\n'
            '      "segment_title": "string",\n'
            '      "target_duration_seconds": 20,\n'
            '      "narration_script": "100-150 words",\n'
            '      "visual_prompt_veo": "video prompt",\n'
            '      "fallback_image_prompt": "image prompt"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Constraints:\n"
            "- Generate exactly 5 beats.\n"
            "- Beat progression: beginner -> intermediate -> advanced.\n"
            "- Narration per beat must be 100-150 words and factually accurate.\n"
            "- visual_prompt_veo must be cinematic moving-shot language.\n"
            "- fallback_image_prompt must be fast-generating and photorealistic.\n"
            "- target_duration_seconds should be between 20 and 30.\n"
            "- No markdown, no commentary, no trailing commas.\n"
            f"Topic: {topic}\n"
            "Use the user's name only if explicitly provided later by the narrator."
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
                "documentary_flow must have exactly 5 objects and each object must contain:\n"
                "beat_id, segment_title, target_duration_seconds (20-30), narration_script (100-150 words), "
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
                print(f"Image generation error: {e}")
                error_text = str(e)
                if "RESOURCE_EXHAUSTED" in error_text or "429" in error_text:
                    await self._send(
                        websocket,
                        {
                            "type": "status",
                            "content": f"{status_prefix} Quota reached. Reusing current visual.",
                        },
                    )
                    return None
                await self._send(
                    websocket,
                    {
                        "type": "status",
                        "content": f"{status_prefix} Generation failed. Keeping current visual.",
                    },
                )
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
                    "target_duration_seconds": durations[idx % len(durations)] if durations else 24,
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
            f"The viewer's name is {user_name}. Address them naturally once in this beat."
            if user_name
            else (
                "If this is beat 1, ask the viewer what name they prefer in one short sentence. "
                "If this is not beat 1, do not ask for a name."
            )
        )
        interactive_check = (
            "End this beat with one short reflective question to keep the viewer engaged."
            if beat_index in {3, 6}
            else "Do not end with a question in this beat."
        )
        return (
            f"Documentary title: {title}\n"
            f"Topic: {topic}\n"
            f"Beat: {beat_index}/{beat_count}\n"
            f"Target narration duration: {beat.get('target_duration_seconds', 24)} seconds\n"
            f"Visual cue for this beat: {beat.get('video_prompt', '')}\n"
            f"Narration script (read this exactly): {beat.get('narration', '')}\n\n"
            "Read the narration script exactly as written.\n"
            "Do not summarize, shorten, or paraphrase the script.\n"
            "Do not stop speaking until you reach the final sentence of this beat script.\n"
            "Stay tightly aligned with this visual cue and do not jump ahead.\n"
            "Do not call any tools unless the user explicitly asks to change visuals or music.\n"
            f"{interactive_check}\n"
            f"{personalization}"
        )

    @staticmethod
    def _build_follow_up_prompt(topic: str, user_name: str) -> str:
        viewer = user_name if user_name else "there"
        return (
            f"The story about {topic} has finished.\n"
            f"Address the viewer as {viewer}.\n"
            "Ask one engaging follow-up question, then ask one quick quiz question "
            "(multiple-choice with 3 options), then pause for user input."
        )

    async def start_session(self, websocket: WebSocket):
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

INTERRUPTIONS:
- If user interrupts by voice or chat, stop current thread immediately.
- Respond to interruption first, then continue from the new direction.

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
            "realtime_input_config": {
                "activity_handling": "START_OF_ACTIVITY_INTERRUPTS",
                "turn_coverage": "TURN_INCLUDES_ONLY_ACTIVITY",
                "automatic_activity_detection": {"disabled": False},
            },
            "input_audio_transcription": {},
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
                voice_turn_transcript = ""
                last_processed_voice_transcript = ""
                last_processed_voice_transcript_at = 0.0
                audio_input_enabled = True
                audio_input_error_reported = False
                current_topic = ""
                current_user_name = ""
                last_visual_request_signature = ""
                last_visual_request_at = 0.0
                story_task: Optional[asyncio.Task] = None
                turn_complete_event = asyncio.Event()
                interrupt_event = asyncio.Event()
                session_send_lock = asyncio.Lock()

                async def send_turn_input(input_text: str):
                    async with session_send_lock:
                        await session.send(input=input_text, end_of_turn=True)

                async def send_realtime_audio(audio_data: bytes):
                    async with session_send_lock:
                        await session.send_realtime_input(
                            audio=genai.types.Blob(
                                data=audio_data,
                                mime_type="audio/pcm;rate=16000",
                            )
                        )

                async def end_realtime_audio():
                    async with session_send_lock:
                        await session.send_realtime_input(audio_stream_end=True)

                async def mark_visual_request(user_text: str, source: str):
                    nonlocal last_visual_request_signature, last_visual_request_at
                    cleaned = " ".join(user_text.lower().split())
                    now = asyncio.get_running_loop().time()
                    if not cleaned:
                        return
                    if cleaned == last_visual_request_signature and now - last_visual_request_at < 2.5:
                        return
                    last_visual_request_signature = cleaned
                    last_visual_request_at = now

                    topic_context = current_topic or "the current documentary topic"
                    normalized_prompt = self._normalize_visual_request_prompt(user_text, topic_context)
                    await self._send(websocket, {"type": "interrupted", "source": source})
                    await self._send(
                        websocket,
                        {"type": "status", "content": f"[Delegator] Reframing shot request: {user_text}"},
                    )
                    self._spawn_task(
                        active_tasks,
                        self.generate_video_scene(normalized_prompt, websocket),
                        "video-interrupt",
                    )

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

                    beat_video_tasks: Dict[int, asyncio.Task] = {}
                    beat_image_tasks: Dict[int, asyncio.Task] = {}
                    visual_wait_notice_slots: Set[int] = set()
                    current_visual_slot_sent = -1
                    slot_visual_mode: Dict[int, str] = {}

                    async def prefetch_visual_slot(
                        slot_index: int,
                        beats: List[Dict[str, str]],
                        visual_plan_indices: List[int],
                    ):
                        if slot_index < 0 or slot_index >= len(visual_plan_indices):
                            return
                        beat_index_for_slot = visual_plan_indices[slot_index]
                        beat = beats[beat_index_for_slot]
                        if slot_index not in beat_video_tasks:
                            beat_prompt = beat.get("video_prompt") or f"Cinematic documentary shot for {topic}"
                            beat_video_tasks[slot_index] = self._spawn_task(
                                active_tasks,
                                self._render_video_payload(
                                    beat_prompt,
                                    websocket=websocket,
                                    status_prefix=f"[Video Agent][Segment {slot_index + 1}]",
                                ),
                                f"video-segment-{slot_index + 1}",
                            )
                        if slot_index not in beat_image_tasks:
                            image_prompt = beat.get("image_prompt") or (
                                "Photorealistic educational documentary still. "
                                + str(beat.get("video_prompt") or "")
                            )
                            beat_image_tasks[slot_index] = self._spawn_task(
                                active_tasks,
                                self._render_image_payload(
                                    image_prompt,
                                    websocket=websocket,
                                    status_prefix=f"[Image Agent][Segment {slot_index + 1}]",
                                ),
                                f"image-segment-{slot_index + 1}",
                            )

                    async def try_take_prefetched_video(slot_index: int) -> Optional[Dict[str, Any]]:
                        task = beat_video_tasks.get(slot_index)
                        if not task or not task.done():
                            return None
                        beat_video_tasks.pop(slot_index, None)
                        try:
                            return task.result()
                        except Exception as task_error:
                            print(f"Video segment {slot_index + 1} task failed: {task_error}")
                            return None

                    async def try_take_prefetched_image(slot_index: int) -> Optional[Dict[str, Any]]:
                        task = beat_image_tasks.get(slot_index)
                        if not task or not task.done():
                            return None
                        beat_image_tasks.pop(slot_index, None)
                        try:
                            return task.result()
                        except Exception as task_error:
                            print(f"Image segment {slot_index + 1} task failed: {task_error}")
                            return None

                    async def deliver_visual_slot(
                        slot_index: int,
                        *,
                        wait_seconds: int,
                        opening: bool = False,
                    ) -> Optional[str]:
                        if slot_index < 0:
                            return None

                        video_payload = await try_take_prefetched_video(slot_index)
                        if video_payload:
                            await self._send_video_payload(
                                websocket,
                                video_payload,
                                delivered_status=(
                                    "[Video Agent] Opening shot delivered."
                                    if opening
                                    else f"[Video Agent] Segment {slot_index + 1} delivered."
                                ),
                            )
                            slot_visual_mode[slot_index] = "video"
                            return "video"

                        image_payload = await try_take_prefetched_image(slot_index)
                        if image_payload:
                            await self._send_image_payload(
                                websocket,
                                image_payload,
                                delivered_status=(
                                    "[Image Agent] Opening storyboard frame delivered."
                                    if opening
                                    else (
                                        f"[Image Agent] Segment {slot_index + 1} storyboard frame delivered "
                                        "while cinematic clip renders."
                                    )
                                ),
                            )
                            slot_visual_mode[slot_index] = "image"
                            return "image"

                        pending_tasks = []
                        image_task = beat_image_tasks.get(slot_index)
                        video_task = beat_video_tasks.get(slot_index)
                        if image_task and not image_task.done():
                            pending_tasks.append(image_task)
                        if video_task and not video_task.done():
                            pending_tasks.append(video_task)

                        if pending_tasks and wait_seconds > 0:
                            await self._wait_with_story_updates(
                                websocket=websocket,
                                tasks=pending_tasks,
                                updates=[
                                    "[Script Agent] Finalizing beat script pacing...",
                                    "[Image Agent] Building the next storyboard frame...",
                                    "[Video Agent] Rendering cinematic motion for this beat...",
                                    "[Studio] Aligning narration timing with visual continuity...",
                                ],
                                timeout_seconds=wait_seconds,
                            )

                        video_payload = await try_take_prefetched_video(slot_index)
                        if video_payload:
                            await self._send_video_payload(
                                websocket,
                                video_payload,
                                delivered_status=(
                                    "[Video Agent] Opening shot delivered."
                                    if opening
                                    else f"[Video Agent] Segment {slot_index + 1} delivered."
                                ),
                            )
                            slot_visual_mode[slot_index] = "video"
                            return "video"

                        image_payload = await try_take_prefetched_image(slot_index)
                        if image_payload:
                            await self._send_image_payload(
                                websocket,
                                image_payload,
                                delivered_status=(
                                    "[Image Agent] Opening storyboard frame delivered."
                                    if opening
                                    else (
                                        f"[Image Agent] Segment {slot_index + 1} storyboard frame delivered "
                                        "while cinematic clip renders."
                                    )
                                ),
                            )
                            slot_visual_mode[slot_index] = "image"
                            return "image"

                        return None

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
                                    "target_duration_seconds": 24,
                                }
                            ]
                        target_beats = 5
                        if len(beats) < target_beats:
                            fallback = self._build_story_beats(self._default_story_blueprint(topic))
                            source = fallback or beats
                            while source and len(beats) < target_beats:
                                template = source[len(beats) % len(source)]
                                beats.append(
                                    {
                                        "narration": template.get("narration", f"Explain {topic} with clear detail."),
                                        "video_prompt": template.get(
                                            "video_prompt",
                                            f"Cinematic documentary shot about {topic}",
                                        ),
                                        "image_prompt": template.get(
                                            "image_prompt",
                                            f"Photorealistic documentary still about {topic}",
                                        ),
                                        "target_duration_seconds": template.get(
                                            "target_duration_seconds",
                                            24,
                                        ),
                                    }
                                )
                        if len(beats) > 6:
                            beats = beats[:6]

                        title = str(blueprint.get("title") or topic).strip() or topic
                        await self._send(websocket, {"type": "topic_received", "content": topic})
                        await self._send(
                            websocket,
                            {
                                "type": "status",
                                "content": f"[Script Agent] Story blueprint finalized for '{topic}' ({len(beats)} beats).",
                            },
                        )
                        await self._send(
                            websocket,
                            {"type": "status", "content": "[Studio] Preparing synchronized visuals, narration, and score..."},
                        )

                        visual_segment_target = max(1, len(beats))
                        visual_plan_indices = list(range(visual_segment_target))
                        if not visual_plan_indices:
                            visual_plan_indices = [0]

                        music_task = self._spawn_task(
                            active_tasks,
                            self.generate_background_score_audio(
                                blueprint.get("music_prompt", ""),
                                websocket,
                            ),
                            "music",
                        )

                        await prefetch_visual_slot(0, beats, visual_plan_indices)
                        await prefetch_visual_slot(1, beats, visual_plan_indices)
                        await prefetch_visual_slot(2, beats, visual_plan_indices)

                        opening_mode = await deliver_visual_slot(0, wait_seconds=18, opening=True)
                        if opening_mode:
                            current_visual_slot_sent = 0
                        else:
                            await self._send(
                                websocket,
                                {
                                    "type": "status",
                                    "content": (
                                        "[Studio] Opening visual is still rendering; holding the first beat briefly "
                                        "to keep your scene and narration aligned."
                                    ),
                                },
                            )
                        if music_task and not music_task.done():
                            await self._send(
                                websocket,
                                {
                                    "type": "status",
                                    "content": "[Music Agent] Score generation continues in parallel while narration begins.",
                                },
                            )

                        await self._send(
                            websocket,
                            {"type": "status", "content": "[Studio] Sequence locked. Rolling your documentary now."},
                        )

                        for beat_index, beat in enumerate(beats):
                            desired_slot = min(
                                beat_index,
                                len(visual_plan_indices) - 1,
                            )
                            await prefetch_visual_slot(desired_slot, beats, visual_plan_indices)
                            await prefetch_visual_slot(desired_slot + 1, beats, visual_plan_indices)
                            await prefetch_visual_slot(desired_slot + 2, beats, visual_plan_indices)

                            current_mode = slot_visual_mode.get(current_visual_slot_sent)
                            if current_mode == "image" and current_visual_slot_sent >= 0:
                                upgraded_video = await try_take_prefetched_video(current_visual_slot_sent)
                                if upgraded_video:
                                    await self._send_video_payload(
                                        websocket,
                                        upgraded_video,
                                        delivered_status=(
                                            f"[Video Agent] Upgraded segment {current_visual_slot_sent + 1} "
                                            "from storyboard frame to cinematic clip."
                                        ),
                                    )
                                    slot_visual_mode[current_visual_slot_sent] = "video"

                            if desired_slot > current_visual_slot_sent:
                                for slot in range(current_visual_slot_sent + 1, desired_slot + 1):
                                    mode = await deliver_visual_slot(slot, wait_seconds=10)
                                    if mode:
                                        current_visual_slot_sent = slot
                                        visual_wait_notice_slots.discard(slot)
                                        continue
                                    if slot not in visual_wait_notice_slots:
                                        visual_wait_notice_slots.add(slot)
                                        await self._send(
                                            websocket,
                                            {
                                                "type": "status",
                                                "content": (
                                                    f"[Studio] Holding beat {beat_index + 1} momentarily while "
                                                    f"segment {slot + 1} visual finishes."
                                                ),
                                            },
                                        )
                                    break

                            if beat_index == 0 and current_visual_slot_sent < 0:
                                opening_mode = await deliver_visual_slot(0, wait_seconds=12, opening=True)
                                if opening_mode:
                                    current_visual_slot_sent = 0
                                    visual_wait_notice_slots.discard(0)
                                elif 0 not in visual_wait_notice_slots:
                                    visual_wait_notice_slots.add(0)
                                    await self._send(
                                        websocket,
                                        {
                                            "type": "status",
                                            "content": (
                                                "[Studio] Opening visual still pending. Starting narration now with "
                                                "a descriptive lead-in to avoid dead air."
                                            ),
                                        },
                                    )

                            if interrupt_event.is_set():
                                interrupt_event.clear()
                                await self._send(
                                    websocket,
                                    {
                                        "type": "status",
                                        "content": "[Delegator] Integrating your interruption before the next beat.",
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
                            await send_turn_input(beat_prompt)
                            got_turn = await wait_for_turn_complete(timeout_seconds=95)
                            if not got_turn:
                                await self._send(
                                    websocket,
                                    {
                                        "type": "status",
                                        "content": "[Delegator] Narration segment timed out. Advancing to keep sync.",
                                    },
                                )

                        for index, task in list(beat_video_tasks.items()):
                            if task.done():
                                beat_video_tasks.pop(index, None)
                                continue
                            task.cancel()
                            with suppress(asyncio.CancelledError):
                                await task
                            beat_video_tasks.pop(index, None)

                        for index, task in list(beat_image_tasks.items()):
                            if task.done():
                                beat_image_tasks.pop(index, None)
                                continue
                            task.cancel()
                            with suppress(asyncio.CancelledError):
                                await task
                            beat_image_tasks.pop(index, None)

                        await self._send(websocket, {"type": "story_complete"})
                        await self._send(
                            websocket,
                            {
                                "type": "status",
                                "content": "[Studio] Story playback complete. Transitioning to interactive Q&A.",
                            },
                        )
                        turn_complete_event.clear()
                        await send_turn_input(self._build_follow_up_prompt(topic, user_name))
                        _ = await wait_for_turn_complete(timeout_seconds=60)
                    except asyncio.CancelledError:
                        await self._send(
                            websocket,
                            {"type": "status", "content": "[Studio] Previous story pipeline cancelled."},
                        )
                        raise
                    except Exception as story_error:
                        print(f"Story runner error: {story_error}")
                        await self._send_error(websocket, f"Story generation error: {story_error}")

                async def receive_from_gemini():
                    nonlocal output_transcript_state, voice_turn_transcript
                    nonlocal last_processed_voice_transcript, last_processed_voice_transcript_at
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

                            if server_content.interrupted:
                                interrupt_event.set()
                                await self._send(websocket, {"type": "interrupted", "source": "voice"})

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

                            input_transcript_text = self._extract_input_transcript_text(server_content)
                            if input_transcript_text:
                                voice_turn_transcript = input_transcript_text
                                normalized_live_transcript = " ".join(input_transcript_text.lower().split())
                                now = asyncio.get_running_loop().time()
                                if (
                                    normalized_live_transcript
                                    and self._is_visual_request(input_transcript_text)
                                    and (
                                        normalized_live_transcript != last_processed_voice_transcript
                                        or now - last_processed_voice_transcript_at > 3.0
                                    )
                                ):
                                    last_processed_voice_transcript = normalized_live_transcript
                                    last_processed_voice_transcript_at = now
                                    interrupt_event.set()
                                    await mark_visual_request(input_transcript_text, "voice")

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
                                transcript = voice_turn_transcript.strip()
                                voice_turn_transcript = ""
                                normalized_transcript = " ".join(transcript.lower().split())
                                now = asyncio.get_running_loop().time()
                                if (
                                    normalized_transcript
                                    and (
                                        normalized_transcript != last_processed_voice_transcript
                                        or now - last_processed_voice_transcript_at > 3.0
                                    )
                                ):
                                    last_processed_voice_transcript = normalized_transcript
                                    last_processed_voice_transcript_at = now
                                    if self._is_visual_request(transcript):
                                        interrupt_event.set()
                                        await mark_visual_request(transcript, "voice")
                    except Exception as e:
                        print(f"Error in Gemini receive loop: {e}")
                        await self._send_error(websocket, f"Live stream receive error: {e}")

                receive_task = asyncio.create_task(receive_from_gemini())

                try:
                    while True:
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

                            interrupt_event.clear()
                            turn_complete_event.clear()
                            story_task = self._spawn_task(
                                active_tasks,
                                run_story(topic, user_name),
                                "story",
                            )
                        elif msg_type == "user_chat":
                            user_text = str(msg.get("content", "")).strip()
                            if not user_text:
                                continue

                            interrupt_event.set()
                            await self._send(websocket, {"type": "interrupted", "source": "chat"})
                            await self._send(websocket, {"type": "status", "content": "Chat interruption received."})
                            if self._is_visual_request(user_text):
                                await mark_visual_request(user_text, "chat")
                            await send_turn_input(user_text)
                        elif msg_type == "user_audio":
                            if not audio_input_enabled:
                                continue
                            try:
                                audio_data = base64.b64decode(msg.get("data", ""))
                                if audio_data:
                                    await send_realtime_audio(audio_data)
                            except Exception as e:
                                error_text = str(e)
                                lowered = error_text.lower()
                                print(f"Error sending audio to Gemini: {error_text}")
                                if (
                                    "invalid frame payload data" in lowered
                                    or "keepalive ping timeout" in lowered
                                    or "no close frame received" in lowered
                                    or "1011" in lowered
                                ):
                                    audio_input_enabled = False
                                    if not audio_input_error_reported:
                                        audio_input_error_reported = True
                                        hint = (
                                            "Mic stream rejected by Live API (need 16kHz s16le mono). "
                                            "Voice input paused; toggle Mic Off/On to retry."
                                            if "invalid frame payload data" in lowered
                                            else "Live voice channel timed out. Voice input paused; toggle Mic Off/On to retry."
                                        )
                                        await self._send(
                                            websocket,
                                            {"type": "status", "content": hint},
                                        )
                                else:
                                    await self._send_error(websocket, f"Audio send error: {e}")
                        elif msg_type == "user_audio_end":
                            try:
                                await end_realtime_audio()
                                audio_input_enabled = True
                                audio_input_error_reported = False
                            except Exception as e:
                                error_text = str(e)
                                print(f"Error ending audio stream: {error_text}")
                                if not audio_input_error_reported:
                                    audio_input_error_reported = True
                                    await self._send(
                                        websocket,
                                        {
                                            "type": "status",
                                            "content": "Voice channel is unstable right now. Chat interruption remains available.",
                                        },
                                    )
                                audio_input_enabled = False
                        else:
                            await self._send_error(websocket, f"Unsupported message type: {msg_type}")
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


PROJECT_ID, LOCATION, VERTEX_API_KEY = _resolve_vertex_runtime()
agent = ChronosAgent(
    project_id=PROJECT_ID,
    location=LOCATION,
    video_output_gcs_uri=os.getenv("VEO_OUTPUT_GCS_URI"),
    api_key=VERTEX_API_KEY,
)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await agent.start_session(websocket)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        ws="websockets-sansio",
        ws_ping_interval=60.0,
        ws_ping_timeout=60.0,
    )
