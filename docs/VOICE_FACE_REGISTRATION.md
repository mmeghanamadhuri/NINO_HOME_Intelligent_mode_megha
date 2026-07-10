# Voice-Triggered Automatic Face Registration

Automatic face registration when the camera sees an **unknown** person. The user does **not** say “register my face” — vision triggers the flow; voice is only used to supply a **name**.

---

## Pipeline

```text
1. Vision     Unknown primary face stable 2–5 s (configurable)
2. Server     TTS → ESP POST /play_wav  (X-Nino-Prompt-Ack: 1)
3. ESP        Play prompt → beep → USB mic VAD (no "Hi ESP")
4. User       "My name is Sirena"
5. ESP        WAV → WebSocket /voice-query
6. Server     STT → parse name → register_sample × N → train()
7. Server     TTS reply → ESP speaker
```

Camera (face samples) and USB mic (name) are separate hardware paths; the server joins them in software.

---

## Firmware (already supported)

| Step | Mechanism |
|------|-----------|
| Proactive TTS | `POST /play_wav` from PC (`esp_playback.post_wav_to_esp`) |
| Listen after speech | Header `X-Nino-Prompt-Ack: 1` → `nino_voice_assist_prompt_medical_ack()` |
| Beep + VAD | Wake chime → `run_ws_and_queue()` without wake word |
| Mic | USB 4-mic on GPIO 24/25 (`usb_mic_read`) — not camera mic |

Same pattern as **medical alarm** yes/no after `prompt_ack`.

---

## Server modules

| File | Role |
|------|------|
| `face_registration_voice.py` | Parse name from phrases (“my name is …”, “I’m …”, “call me …”) |
| `face_registration_service.py` | State machine, unknown-face timer, capture + train |
| `app.py` | MJPEG hook (`on_frame`), `/api/register` shared capture, `/api/status` |
| `voice_service.py` | `handle_face_registration_voice()` before alarm/LLM routing |

---

## Registration states

| State | Meaning |
|-------|---------|
| `idle` | Normal operation |
| `awaiting_name` | Prompt played; next voice WS utterance should contain a name |
| `capturing` | Saving face samples (brief, during voice turn) |

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FACE_REG_ENABLED` | `1` | Set `0` to disable automatic registration |
| `FACE_REG_UNKNOWN_SECONDS` | `3` | Seconds unknown face must stay in frame before prompt |
| `FACE_REG_PROMPT_COOLDOWN_SECONDS` | `600` | Min seconds between prompts for the same session |
| `FACE_REG_SAMPLES` | `15` | Face crops per registration (same as web UI) |
| `FACE_REG_INTERVAL_MS` | `150` | Delay between sample captures |
| `FACE_REG_AWAIT_NAME_SECONDS` | `45` | Drop `awaiting_name` if no voice response |

Requires `ESP_PLAY_WAV_URL` or `CAMERA_SOURCE=http://<ESP_IP>/stream` for proactive prompts.

On each `prompt_ack` `play_wav`, the server also sends:

| Header | Purpose |
|--------|---------|
| `X-Nino-Voice-Ws-Url` | Auto-configure ESP → PC WebSocket |
| `X-Nino-Prompt-Ack-Chime` | `0` = listen right after prompt (no extra beep) |

Set `VOICE_WS_URL` or `NINO_SERVER_LAN_HOST` if LAN auto-detection is wrong.

**Firmware:** flash after pulling — `play_wav` handler saves the voice WS URL from the server.

---

## Voice routing priority

In `process_voice_wav()` (after STT):

1. Volume commands  
2. STT echo/garbage rejection  
3. **Face registration** (if `awaiting_name`)  
4. Alarm voice  
5. Servo / recap / identity / LLM  

---

## Web UI registration

`POST /api/register` still works unchanged. Both paths call the same `capture_face_samples()` helper.

---

## Testing

```bash
cd server
python -m unittest test_face_registration.py -v
```

Manual: stand in front of camera as unknown person for ~3 s → hear prompt → after beep say “My name is TestUser” → face box should show name on next recognition.

---

## Related docs

- [README.md](../README.md) — architecture overview  
- [USB-4MIC-INTEGRATION.md](USB-4MIC-INTEGRATION.md) — voice mic wiring  
- [ALARM.md](ALARM.md) — `prompt_ack` listen-after-TTS pattern  
- [SERVER_ARCHITECTURE.md](SERVER_ARCHITECTURE.md) — voice WebSocket pipeline  
