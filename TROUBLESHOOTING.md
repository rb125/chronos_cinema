# Troubleshooting

## Gemini Live session fails (404 / 1006)

- Ensure `GEMINI_LIVE_MODEL=gemini-live-2.5-flash-native-audio` — other model name variants return 404 on Vertex AI.
- The Live API requires `v1beta1`. The app uses an isolated client for this; do not change `api_version` in the main client.
- Verify ADC is valid: `gcloud auth application-default print-access-token`

## Images return 404

- Ensure the main client uses `v1` (not `v1alpha`). `v1alpha` does not support `generate_content` on Vertex AI.

## No background music

- Lyria requires `VERTEX_AUTH_MODE=project` with ADC. API-key mode falls back to an ambient oscillator.
- Check logs for `[GCP] Lyria` lines — if the endpoint call fails, the error is printed there.

## Quiz appears before narration finishes

- The frontend waits for 1s of audio silence then for `isNarrationActive()` to return false. If audio is not playing at all (AudioContext suspended), check browser autoplay permissions.

## Live session drops mid-documentary (1006 abnormal closure)

- The backend sends keepalive pings to the Live session every 5s while waiting for images. If you see this error, check that the keepalive loop is running (look for no `[DEBUG] render_image_for_beat` logs stalling).

## Frontend build fails

- Run `npm install` inside `frontend/` before `npm run dev`.
- Node 18+ required.
