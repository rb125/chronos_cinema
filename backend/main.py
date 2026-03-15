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
            f"Invalid VERTEX_AUTH_MODE: {auth_mode}. Use 'project', 'api_key', or 'auto'."
        )

    # Force project mode if explicitly requested.
    if auth_mode == "project":
        if not project_id:
            raise RuntimeError("VERTEX_AUTH_MODE='project' requires GOOGLE_CLOUD_PROJECT.")
        if not location:
            location = "us-central1"
        return project_id, location, None

    # Force API key mode if explicitly requested.
    if auth_mode == "api_key":
        if not api_key:
            raise RuntimeError("VERTEX_AUTH_MODE='api_key' requires GOOGLE_API_KEY.")
        if not location:
            location = "global"
        return None, location, api_key

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
    documentary_flow: List[ScriptBeat] = Field(min_length=1, max_length=10)


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
        
        auth_mode_env = (os.getenv("VERTEX_AUTH_MODE") or "auto").strip().lower()
        self.auth_mode = "express_api_key" if api_key and not project_id else "adc"
        
        client_kwargs: Dict[str, Any] = {"http_options": {"api_version": "v1alpha"}}
        
        # If we have an API key (regardless of auth_mode), use standard Gemini API endpoint.
        if api_key:
            client_kwargs["api_key"] = api_key
        else:
            # Standard Vertex AI Project mode (ADC)
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
        self.image_model = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
        self.text_model = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
        self.lyria_model = os.getenv("GEMINI_MUSIC_MODEL", "lyria-002")
        self.video_only_mode = os.getenv("VIDEO_ONLY_MODE", "true").strip().lower() != "false"
        self.generate_video = os.getenv("GENERATE_VIDEO", "true").strip().lower() != "false"

        try:
            self.video_render_concurrency = max(
                1,
                int(os.getenv("VIDEO_RENDER_CONCURRENCY", "1")),
            )
        except ValueError:
            self.video_render_concurrency = 1

        self._storage_client: Optional["storage.Client"] = None
        self._video_guard = asyncio.Semaphore(self.video_render_concurrency)
        self._image_guard = asyncio.Semaphore(4)  # allow all beats to render in parallel
        self._out_queue: Optional[asyncio.Queue] = None
        self._client_kwargs = client_kwargs # Store for per-session isolation

        self.live_tools = [
            {
                "function_declarations": [
                    {
                        "name": "queue_scene_image",
                        "description": "Queue a high-detail cinematic visual for the current narration beat.",
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
        """Pushes a message into the outgoing queue for the client WebSocket.
        This is non-blocking to ensure the caller (e.g. Gemini receiver) can
        immediately return to processing incoming data."""
        if self._out_queue is not None:
            await self._out_queue.put(payload)
        else:
            # Fallback if queue not initialized (should not happen in start_session)
            try:
                text = json.dumps(payload)
                await websocket.send_text(text)
            except Exception as e:
                print(f"Direct WebSocket send failed: {e}")

    async def _process_websocket_queue(self, websocket: WebSocket):
        """Dedicated background task to drain the outgoing queue and send to the client.
        Ensures WebSocket.send_text is called serially and does not block other tasks."""
        while True:
            try:
                payload = await self._out_queue.get()
                # For large payloads, stringify in a thread to keep the event loop responsive.
                if any(isinstance(v, str) and len(v) > 50000 for v in payload.values()):
                    text = await asyncio.to_thread(json.dumps, payload)
                else:
                    text = json.dumps(payload)

                await websocket.send_text(text)
                self._out_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in websocket queue processor: {e}")
                await asyncio.sleep(0.1)

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
                beat_durations.append(duration_val)

        music_prompt = str(raw.get("music_prompt") or fallback["music_prompt"]).strip()

        return {
            "title": title,
            "narration_outline": narration_outline or fallback["narration_outline"],
            "scene_cues": scene_cues or fallback["scene_cues"],
            "image_cues": image_cues or fallback["image_cues"],
            "beat_durations": beat_durations or fallback["beat_durations"],
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
        for i, narration in enumerate(outline):
            visual = cues[i % len(cues)].get("prompt", "Cinematic shot.") if cues else "Cinematic shot."
            outline_lines.append(f"Beat {i+1}:\nVisual: {visual}\nScript: {narration}")

        return "\n\n".join(outline_lines)

    async def _generate_lyria_audio_bytes(self, prompt: str) -> Optional[bytes]:
        """Call Lyria 002 via Vertex AI predict (multimodal engine)."""
        url = f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project_id}/locations/{self.location}/publishers/google/models/{self.lyria_model}:predict"

        # Obtain ADC credentials
        credentials, _ = google.auth.default()
        auth_request = GoogleAuthRequest()
        credentials.refresh(auth_request)
        token = credentials.token

        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "sample_rate_hz": 44100,
                "duration_seconds": 60,
            },
        }

        req = urllib_request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib_request.urlopen(req) as resp:
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

            b64_data = await asyncio.to_thread(base64.b64encode, audio_bytes)
            await self._send(
                websocket,
                {
                    "type": "bgm_audio",
                    "data": b64_data.decode("utf-8"),
                    "prompt": prompt,
                },
            )
            await self._send(websocket, {"type": "status", "content": "[Music Agent] Orchestral score delivered."})
        except Exception as e:
            print(f"Lyria generation failed: {e}")
            await self._send(websocket, {"type": "status", "content": f"[Music Agent] Composition interrupted: {e}"})

    @staticmethod
    def _extract_first_audio_base64(predict_response: dict) -> Optional[str]:
        predictions = predict_response.get("predictions", [])
        if not predictions:
            return None
        # Lyria output is typically a list of dicts with 'audio_bytes' (base64)
        return predictions[0].get("audio_bytes")

    def _spawn_task(self, task_set: Set[asyncio.Task], coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        task_set.add(task)
        task.add_done_callback(task_set.discard)
        return task

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

            if name == "queue_scene_image":
                if prompt:
                    self._spawn_task(
                        task_set,
                        self.generate_image_scene(prompt, websocket),
                        "image",
                    )
                    result = {"status": "queued", "prompt": prompt}
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
                            b64_data = await asyncio.to_thread(base64.b64encode, video_bytes)
                            return {
                                "type": "video",
                                "data": b64_data.decode("utf-8"),
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
                        "content": f"{status_prefix} Visual failed: {e}. Attempting fallback storyboard...",
                    },
                )
                return None

    async def _render_image_payload(
        self,
        prompt: str,
        websocket: WebSocket,
        *,
        status_prefix: str = "[Image Agent]",
    ) -> Optional[Dict[str, Any]]:
        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                await self._send(
                    websocket,
                    {"type": "status", "content": f"{status_prefix} Generating storyboard frame: {prompt[:70]}..."},
                )
                async with self._image_guard:
                    # Nano Banana models (multimodal native) use generate_content
                    response = await asyncio.to_thread(
                        self.client.models.generate_content,
                        model=self.image_model,
                        contents=prompt,
                        config=genai.types.GenerateContentConfig(
                            response_modalities=["IMAGE", "TEXT"],
                        ),
                    )
                    for part in (response.candidates or [{}])[0].content.parts if response.candidates else []:
                        inline = getattr(part, "inline_data", None)
                        if inline and inline.data:
                            b64_data = await asyncio.to_thread(base64.b64encode, inline.data)
                            return {
                                "type": "image",
                                "data": b64_data.decode("utf-8"),
                                "mime_type": inline.mime_type or "image/png",
                                "uri": None,
                            }
                    raise RuntimeError("Gemini image model returned no image part.")
            except Exception as e:
                error_text = str(e)
                is_quota = "RESOURCE_EXHAUSTED" in error_text or "429" in error_text
                print(f"Image generation error (attempt {attempt + 1}/{max_attempts}): {e}")
                if is_quota:
                    if attempt < max_attempts - 1:
                        backoff = 10 * (2 ** attempt)  # 10s, 20s, 40s
                        await self._send(websocket, {"type": "status", "content": f"{status_prefix} Quota limit hit, retrying in {backoff}s... (attempt {attempt + 1}/{max_attempts})"})
                        await asyncio.sleep(backoff)
                    else:
                        await self._send(websocket, {"type": "status", "content": f"{status_prefix} Imagen quota exhausted after {max_attempts} attempts. Skipping visual for this beat."})
                        return None
                elif attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
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
                model=self.script_model,
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
            print(f"[DEBUG] [Quiz Agent] Requesting quiz from {self.script_model}...")
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.script_model,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            raw_text = response.text or ""
            parsed = self._parse_json_loose(raw_text)
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
                
                print(f"[DEBUG] [Quiz Agent] Successfully generated {len(valid)} valid questions.")
                if valid:
                    return valid[:5]
            
            print(f"[DEBUG] [Quiz Agent] No valid questions parsed from LLM response. Text: {raw_text[:200]}...")
        except Exception as e:
            print(f"Quiz generation failed: {e}")
        
        # Fallback quiz if generation fails
        print(f"[DEBUG] [Quiz Agent] Using fallback quiz for {topic}")
        return [
            {
                "question": f"What was the main focus of this documentary?",
                "options": [f"A. The history of {topic}", f"B. The future of {topic}", f"C. The impact of {topic}", f"D. All of the above"],
                "correct": "D",
                "explanation": f"The documentary provided a comprehensive overview of {topic} from multiple angles."
            },
            {
                "question": f"Which concept was explored in the most depth?",
                "options": [f"A. Core principles of {topic}", f"B. Advanced theories", f"C. Philosophical implications", f"D. Common misconceptions"],
                "correct": "A",
                "explanation": f"Establishing a solid foundation in {topic} was the primary educational goal."
            }
        ]

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
        # Simple extraction: remove "show me" etc.
        p = user_text.lower()
        p = re.sub(r"\bshow me\b", "", p)
        p = re.sub(r"\bcan i see\b", "", p)
        p = p.strip()
        if not p:
            return f"Photorealistic cinematic documentary still of {topic}"
        return f"Photorealistic cinematic documentary still of {p}, high detail, nature documentary style"

    def _build_beat_prompt(self, topic: str, title: str, beat_index: int, beat_count: int, beat: Dict[str, Any], user_name: str) -> str:
        arc_labels = {
            1: "HOOK — Open with a stunning fact or question",
            2: "FOUNDATION — Establish core concepts",
            3: "MECHANISM — Explain how it works",
            4: "REFLECTION — Connect to a deeper meaning",
        }
        arc_label = arc_labels.get(beat_index, "EXPLORATION")
        personalization = f"Personalize for {user_name} if appropriate." if user_name else ""
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

    async def _run_tts_fallback_session(self, websocket: WebSocket, initial_message: Optional[dict]):
        """Handle a full documentary session using TTS when Gemini Live is unavailable."""
        active_tasks: Set[asyncio.Task] = set()
        self._spawn_task(active_tasks, self._process_websocket_queue(websocket), "ws-sender")
        try:
            # Process the initial start message, then keep listening for more
            msg = initial_message
            while True:
                if msg is None:
                    try:
                        data = await websocket.receive_text()
                        msg = json.loads(data)
                    except (WebSocketDisconnect, json.JSONDecodeError):
                        break

                if msg.get("type") == "start":
                    topic = str(msg.get("topic") or "").strip()
                    user_name = str(msg.get("name") or "").strip()
                    if not topic:
                        await self._send_error(websocket, "Missing topic.")
                        msg = None
                        continue

                    await self._send(websocket, {"type": "status", "content": "[Script Agent] Drafting narrative arc (TTS mode)..."})
                    blueprint = await self.build_story_blueprint(topic)
                    beats = self._build_story_beats(blueprint)
                    title = blueprint.get("title", topic)
                    await self._send(websocket, {"type": "topic_received", "content": topic, "title": title})

                    image_tasks = {}
                    for beat_idx, beat in enumerate(beats):
                        async def _render(idx=beat_idx, b=beat):
                            prompt = b.get("image_prompt") or b.get("video_prompt", "")
                            payload = await self._render_image_payload(prompt, websocket, status_prefix=f"[Image][Beat {idx+1}]")
                            if payload:
                                payload["beat_index"] = idx
                                await self._send(websocket, payload)
                        image_tasks[beat_idx] = self._spawn_task(active_tasks, _render(), f"image-beat-{beat_idx+1}")
                        await asyncio.sleep(0.5)

                    self._spawn_task(active_tasks, self.generate_background_score_audio(blueprint.get("music_prompt", ""), websocket), "music")

                    # Wait for beat 0 image
                    if image_tasks.get(0) and not image_tasks[0].done():
                        with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                            async with asyncio.timeout(28):
                                await asyncio.shield(image_tasks[0])

                    for beat_index, beat in enumerate(beats):
                        if beat_index > 0:
                            t = image_tasks.get(beat_index)
                            if t and not t.done():
                                with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                                    async with asyncio.timeout(20):
                                        await asyncio.shield(t)

                        await self._send(websocket, {"type": "beat_start", "beat_index": beat_index,
                                                      "total_beats": len(beats), "title": title,
                                                      "target_duration_seconds": beat.get("target_duration_seconds", 15)})
                        beat_prompt = self._build_beat_prompt(topic=topic, title=title, beat_index=beat_index+1,
                                                               beat_count=len(beats), beat=beat, user_name=user_name)
                        await self._tts_narrate_beat(beat_prompt, websocket)
                        await self._send(websocket, {"type": "beat_end", "beat_index": beat_index})

                    script_content = "\n\n".join(f"Beat {i+1}: {b.get('narration','')}" for i, b in enumerate(beats))
                    quiz_questions = await self.build_quiz(topic, script_content)
                    if quiz_questions:
                        await self._send(websocket, {"type": "quiz_data", "questions": quiz_questions, "topic": topic})
                    await self._send(websocket, {"type": "story_complete"})

                msg = None
        except WebSocketDisconnect:
            pass
        finally:
            for task in list(active_tasks):
                task.cancel()
            for task in list(active_tasks):
                with suppress(asyncio.CancelledError):
                    await task

    async def _tts_narrate_beat(self, text: str, websocket: WebSocket) -> bool:
        """Fallback: synthesise speech for one beat via generate_content AUDIO modality."""
        try:
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.text_model,
                contents=text,
                config={
                    "response_modalities": ["AUDIO"],
                    "speech_config": {
                        "voice_config": {
                            "prebuilt_voice_config": {"voice_name": "Charon"},
                        },
                    },
                },
            )
            for part in (response.candidates or [{}])[0].content.parts if response.candidates else []:
                inline = getattr(part, "inline_data", None)
                if inline and inline.data:
                    b64_data = await asyncio.to_thread(base64.b64encode, inline.data)
                    await self._send(websocket, {"type": "audio_chunk", "data": b64_data.decode("utf-8")})
                text_part = getattr(part, "text", None)
                if text_part:
                    await self._send(websocket, {"type": "narration", "content": text_part})
            return True
        except Exception as e:
            print(f"TTS synthesis failed: {e}")
            return False

    async def start_session(self, websocket: WebSocket, initial_message: Optional[dict] = None):
        # Isolate the Live session client to prevent resource contention with background workers.
        live_client = genai.Client(**self._client_kwargs)

        config = {
            "system_instruction": {
                "parts": [{
                    "text": """You are Chronos Delegator, a live creative director in an agentic multimodal system.

ROLE:
- Speak to the user with natural, cinematic narration.
- Coordinate specialist worker agents via tool calls:
  - queue_scene_image(prompt)
  - switch_music_mood(prompt)

INTERLEAVING RULES:
- Treat each narration segment as one beat tied to one visual cue.
- Keep language visually grounded in the currently shown shot.
- Use tool calls only when the user explicitly requests a visual/music change.
- Never read tool names/prompts aloud.

STYLE:
- Deep, measured, authoritative documentary tone — gravitas of a nature documentary narrator.
- Speak slowly and deliberately, with weight on key words.
- Prioritize clarity, momentum, and educational value.
- For full topic runs, target a 1-2 minute educational experience with progressive depth.""",
                }],
            },
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": "Charon"},
                },
            },
            "output_audio_transcription": {},
            "tools": self.live_tools,
        }

        self._out_queue = asyncio.Queue()
        try:
            async with live_client.aio.live.connect(model=self.live_model, config=config) as session:
                active_tasks: Set[asyncio.Task] = set()
                # Start the background sender task to process the client websocket queue
                self._spawn_task(
                    active_tasks,
                    self._process_websocket_queue(websocket),
                    "ws-sender",
                )

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
                    {"type": "status", "content": "Delegator + Script + Image + Music agents ready (image-first mode)"},
                )

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

                        # ── Phase 1: Fire media renders (staggered to prevent burst timeouts) ──
                        image_tasks: Dict[int, asyncio.Task] = {}
                        for beat_idx, beat in enumerate(beats):
                            image_tasks[beat_idx] = self._spawn_task(
                                active_tasks,
                                render_image_for_beat(beat_idx, beat),
                                f"image-beat-{beat_idx + 1}",
                            )
                            # Stagger each beat's generation to prevent 16+ simultaneous API bursts.
                            await asyncio.sleep(1.5)

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
                                        async with asyncio.timeout(20):
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
                                print(f"[DEBUG] Sending beat {beat_index + 1} prompt to Gemini Live")
                                await send_turn_input(beat_prompt)
                                print(f"[DEBUG] Beat {beat_index + 1} prompt sent OK")
                            except Exception as send_err:
                                print(f"Beat {beat_index + 1} session error: {send_err}")
                                await self._send(websocket, {"type": "status", "content": f"[Studio] Session interrupted at beat {beat_index + 1} — ending documentary early."})
                                break
                            beat_timeout = max(int(beat.get("target_duration_seconds", 15)) + 60, 90)
                            got_turn = await wait_for_turn_complete(timeout_seconds=beat_timeout)
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
                            print(f"[DEBUG] [Quiz Agent] Sending {len(quiz_questions)} questions to client.")
                            await self._send(websocket, {"type": "quiz_data", "questions": quiz_questions, "topic": topic})
                        else:
                            print("[DEBUG] [Quiz Agent] No questions generated, even fallback failed.")
                        
                        try:
                            print("[DEBUG] [Studio] Requesting closing reflection from Gemini...")
                            await self._send(
                                websocket,
                                {"type": "status", "content": "[Studio] Documentary complete. Delivering closing reflection."},
                            )
                            turn_complete_event.clear()
                            await send_turn_input(self._build_follow_up_prompt(topic, user_name))

                            print("[DEBUG] [Studio] Waiting for reflection turn_complete...")
                            got_reflection = await wait_for_turn_complete(timeout_seconds=60)
                            print(f"[DEBUG] [Studio] Reflection turn_complete received: {got_reflection}")
                        except Exception as reflection_err:
                            print(f"[DEBUG] [Studio] Closing reflection failed (non-fatal): {reflection_err}")
                        finally:
                            print("[DEBUG] [Studio] Sending story_complete signal to client.")
                            await self._send(websocket, {"type": "story_complete"})

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
                        while True:
                            async for message in session.receive():
                                try:
                                    tool_call = getattr(message, "tool_call", None)
                                    function_calls = getattr(tool_call, "function_calls", None) if tool_call else None
                                    if function_calls:
                                        await self._handle_live_function_calls(
                                            function_calls=function_calls,
                                            session=session,
                                            websocket=websocket,
                                            task_set=active_tasks,
                                            seen_call_ids=seen_call_ids,
                                            send_lock=None,
                                        )

                                    server_content = message.server_content
                                    if not server_content:
                                        continue

                                    if server_content.model_turn:
                                        for part in server_content.model_turn.parts:
                                            inline_data = getattr(part, "inline_data", None)
                                            if inline_data and inline_data.data:
                                                b64_data = await asyncio.to_thread(base64.b64encode, inline_data.data)
                                                await self._send(
                                                    websocket,
                                                    {
                                                        "type": "audio_chunk",
                                                        "data": b64_data.decode("utf-8"),
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
                                        print(f"[DEBUG] turn_complete received from Gemini")
                                        turn_complete_event.set()
                                        output_transcript_state = ""
                                except asyncio.CancelledError:
                                    raise
                                except Exception as e:
                                    print(f"Error processing Gemini message: {type(e).__name__}: {e}")
                                    turn_complete_event.set()
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        print(f"Gemini receive loop fatal error: {e}")
                        await self._send_error(websocket, f"Live stream receive error: {e}")
                        turn_complete_event.set()
                    finally:
                        print(f"[DEBUG] receive_from_gemini loop exited")

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

                        if msg.get("type") == "start":
                            topic = str(msg.get("topic") or "").strip()
                            user_name = str(msg.get("name") or "").strip()
                            if not topic:
                                await self._send_error(websocket, "Missing topic.")
                                continue

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
            await self._send(websocket, {"type": "status", "content": "Gemini Live unavailable — switching to TTS fallback mode."})
            await self._run_tts_fallback_session(websocket, initial_message)
        except Exception as e:
            print(f"Error in live session: {e}")
            if any(k in str(e).lower() for k in ("model", "not found", "permission", "quota", "unavailable", "403", "404", "429")):
                await self._send(websocket, {"type": "status", "content": f"Gemini Live error ({type(e).__name__}) — switching to TTS fallback."})
                await self._run_tts_fallback_session(websocket, initial_message)
            else:
                await self._send_error(websocket, f"Session error: {str(e)}")
        finally:
            self._out_queue = None


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

    agent = ChronosAgent(
        project_id=PROJECT_ID,
        location=LOCATION,
        api_key=effective_api_key,
    )
    await agent.start_session(websocket, initial_message=first_msg)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
