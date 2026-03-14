# Troubleshooting (Vertex AI + Gemini Live)

## 1. Live API Connection Fails

### Symptoms
- WebSocket connects to backend, but backend cannot establish Gemini Live session.

### Checks
1. Verify env vars are set:
```bash
echo "$VERTEX_AUTH_MODE"      # auto|project|api_key
echo "$GOOGLE_CLOUD_PROJECT"
echo "$GOOGLE_CLOUD_LOCATION"
echo "$GOOGLE_CLOUD_API_KEY"   # optional
echo "$GOOGLE_API_KEY"         # optional alias
```
In API-key mode, `GOOGLE_CLOUD_PROJECT` may be empty or ignored by design.
For full Live interleaving, prefer `VERTEX_AUTH_MODE=project`.
2. Verify ADC auth:
```bash
gcloud auth application-default print-access-token >/dev/null && echo "ADC OK"
```
Skip ADC check when `VERTEX_AUTH_MODE=api_key`.
3. Verify APIs enabled:
```bash
gcloud services list --enabled | rg 'aiplatform|run|cloudbuild|storage'
```
4. Run live connectivity test:
```bash
cd backend
python3 check_live_api.py
```
5. If using `gemini-live-2.5-flash-native-audio`, keep `response_modalities=["AUDIO"]` and use output transcription for text pane.

## 2. Veo Video Does Not Render

### Common Causes
- Model access/quota constraints for Veo.
- No downloadable bytes returned.
- Missing permissions on `VEO_OUTPUT_GCS_URI` bucket.

### Checks
1. Ensure optional output bucket is valid:
```bash
gsutil ls "$VEO_OUTPUT_GCS_URI"
```
2. Ensure your runtime identity can read/write the bucket.
3. Inspect backend logs for fallback message:
- If Veo fails, app auto-falls back to Imagen still images.

## 3. Images Not Showing

### Checks
1. Verify backend receives `[DIAGRAM: ...]` markers from live stream.
2. Confirm `imagen` model access in your project/region.
3. Check backend logs for `Image generation failed`.

## 4. No Voice Interruption

### Checks
1. Microphone permissions are granted in browser.
2. Header should show `Mic Active`.
3. Speak while narration is playing; sidebar should log interruption events.

## 5. Frontend Build Fails (Fonts)

If `next build` fails fetching Google Fonts in restricted networks, build on a networked environment/CI runner, or switch to local fonts.
