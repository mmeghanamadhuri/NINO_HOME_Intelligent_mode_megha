# Voice assistant changes (18 Aug 2026)

Work on this pass: make Aux-in wake usable at low energy, stop the silence / “sorry” loop, run STT and LLM on GPU, and make P4 + server logs match.

Firmware must be flashed. Server must be restarted so `.env` and Whisper reload.

---

## Why it was unstable

| Problem | What happened |
| --- | --- |
| Energy door 400 | Real “Ok Nino” on Aux sat at energy 1–2, so wake never started |
| Quiet door 200 | Speech at 5–10 counted as silence; VAD ended too early or never heard a question |
| Session always stayed open | One false wake → record → reply → listen again forever |
| Empty STT still talked | “Sorry, I didn't catch that” + mic reopen |
| Whisper `tiny` | Hallucinations on noise / silence |
| Mic 900 ms after TTS | Speaker tail captured as the user |
| Server never saw energy | Could not reject a silent WAV |
| Old “5 s capture” wording | Looked like a 5-second poll; wake is VAD, not a timer |

---

## Firmware (`main/voice_assist.c`, `main/audio_capture.c`, `main/main.c`)

### Energy gates (Aux-in)

Was:

- start = `max(400, noise × 4)` for 160 ms
- quiet = `max(200, noise × 1.5)`

Now:

- start = `max(5, noise + 4)` for **60 ms** (3 × 20 ms frames)
- quiet = `max(3, noise + 2)`, always **below** the start gate
- upload only if peak frame energy **≥ 5**

Idle `energy=1–2` does not trigger. Speech must reach about **5–10**. If Ok Nino stays at 1–2, Aux is still not connected — the gate cannot invent signal.

### Capture / session

- Wake: 500 ms preroll + 1 s gap + wait up to 4 s for the question + stop on ~800 ms quiet (max 8 s).
- Continue-listen now **waits for speech** (up to 4 s). It no longer ends because energy is 1.
- After TTS, wait **1.8 s** before opening the mic (was 900 ms) to avoid speaker echo.
- Silent clips are **not uploaded** (`SKIP` / `SILENT_SKIP`). LED goes idle, not red.
- Peak energy and a **turn id** are appended to the WebSocket URL: `?session=wake&turn=3&energy=12`.

### Lights (unchanged scenes, same mapping)

Hardware: common-anode RGB on GPIO 2 / 3 / 4.

| Scene | Light | When |
| --- | --- | --- |
| `IDLE` | Off | Waiting, thinking, TTS, silent skip |
| `LISTEN` | Solid green | Mic open (energy hit through end of capture) |
| `DONE` | Green blink ×3, then off | TTS finished **and** session closed |
| `ERROR` | Fast red blink | Capture or WebSocket failed |

Green = mic open, not “energy is high.” Blink only when the session ends. Follow-up questions go green again with no blink.

### Logs (P4)

One line, shared with the server. Grep `NINO VOICE`.

```
NINO VOICE | turn=3 | TRIGGER  | energy=12 noise=1 th=5 led=green
NINO VOICE | turn=3 | UPLOAD   | session=wake peak=12 bytes=48000
NINO VOICE | turn=3 | REPLY    | continue=1 led=off
```

Stages: `ARMED` `IDLE` `TRIGGER` `WAKE` `CONV` `CAPTURE` `QUESTION` `SENTENCE` `NO_Q` `WAV` `SKIP` `UPLOAD` `WAIT_PC` `REPLY` `REARM` `FAIL`.

---

## Server (`server/voice_service.py`, `server/app.py`)

### Silent / empty STT — no sorry loop

- `energy < VOICE_MIN_ENERGY` (default 5): skip STT, return a tiny silent WAV, **do not** reopen the mic.
- Empty STT, garbled STT, or TTS-echo: same silent close.
- Session-end paths now include `stt_empty`, `stt_silent`, `stt_rejected`, `wake_reject`, `goodbye`.
- A good reply still keeps the conversation open until goodbye.
- Wake “energy fallback” (command without “Ok Nino” in STT) only if peak energy is above the gate.

### Energy on the server

- Reads `energy` from the WebSocket query.
- Also measures peak mean-abs energy on the WAV (same 20 ms frames as the P4).
- Uses `max(reported, measured)` so a missing query param still works.

### GPU / models

| Piece | Now |
| --- | --- |
| STT | Local **faster-whisper `small` on CUDA** (`float16`). Default is no longer ElevenLabs Scribe. |
| LLM | `OLLAMA_FORCE_GPU=1` (Windows Ollama on `:11434` uses the NVIDIA GPU). |
| TTS | Still **ElevenLabs** if the key is set (cloud). Piper remains the offline CPU fallback. |

`.env` / `.env.example` set:

```
STT_PROVIDER=whisper
WHISPER_MODEL=small
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
WHISPER_PRELOAD=1
VOICE_MIN_ENERGY=5
OLLAMA_FORCE_GPU=1
TTS_PROVIDER=elevenlabs
```

If logs say `CTranslate2 reports no GPU`, the faster-whisper / ctranslate2 install is CPU-only and needs a CUDA wheel.

### Logs (server)

Same `NINO VOICE | turn=N | STAGE |` line. `turn` comes from the P4 URL.

Stages: `WS_OPEN` `RECV` `STT` `WAKE` `CMD` `CONV` `SKIP` `SEND` `SESSION` `DONE` `REJECT`.

Example:

```
NINO VOICE | turn=3 | RECV     | session=wake energy=12 energy_th=5
NINO VOICE | turn=3 | STT      | engine=whisper text='ok nino what time is it'
NINO VOICE | turn=3 | WAKE     | ok=1 phrase=ok nino
NINO VOICE | turn=3 | SEND     | continue_listen=1 stt=0.4 llm=1.1 tts=0.5
NINO VOICE | turn=3 | DONE     | path=llm continue_listen=1
```

If a turn is on the P4 as `SKIP` and missing on the server, the clip was never uploaded.

---

## Pipeline (current)

```
Sirena analog → ES8311 AUX → P4 energy loop (idle, light off)
        │
   energy ≥ 5 for 60 ms
        │
   green + capture (preroll / gap / question / VAD)
        │
   peak < 5 → SKIP, light off, wait for next Ok Nino
   peak ≥ 5 → WS upload  session + turn + energy
        │
   server: energy gate → Whisper GPU → wake check → LLM GPU → TTS
        │
   empty / silent / garbled → silent WAV, session end, P4 blinks then off
   good reply → play TTS (light off)
        │
   continue → wait 1.8 s → green, listen for next sentence
   goodbye / reject → blink, then wait for Ok Nino
```

The P4 still does **not** detect the words “Ok Nino”. Energy is only a loudness door. The PC validates the wake from STT.

---

## Files touched

| File | Change |
| --- | --- |
| `main/voice_assist.c` | Low energy gates, silent skip, turn id, `energy=` URL, 1.8 s post-TTS, wait-for-speech continue, `NINO VOICE` logs |
| `main/voice_assist.h` | Comment: silent clips are not sent |
| `main/audio_capture.c` | `NINO VOICE` capture stages |
| `main/main.c` | Drop “5 s capture” boot / CLI text |
| `server/voice_service.py` | Energy gate, silent close, GPU Whisper defaults, shared logs |
| `server/app.py` | Parse `turn` + `energy`; `DONE` log line |
| `server/.env` / `server/.env.example` | Whisper CUDA + `VOICE_MIN_ENERGY` |
| `server/test_voice_stability.py` | Silent-clip and session-end tests |

---

## How to verify

1. Flash the P4. Restart the voice server.
2. Filter both consoles: `NINO VOICE`.
3. Idle should stay `energy=1–2 th=5` with light off.
4. Say **Ok Nino** + a question. Expect `TRIGGER` → `UPLOAD` on the P4 and `RECV` / `STT` / `WAKE` / `SEND` on the server with the **same `turn=`**.
5. Green only while recording. Off while thinking and answering. Blink only when the session ends.
6. If Aux never leaves 1–2, you will only see `IDLE` or `SKIP` — check the Sirena → ES8311 LIN cable.

---

## What this does *not* change

- No on-chip wake-word model (still energy + server STT).
- TTS still ElevenLabs (network). Piper is CPU if cloud fails.
- Dynamixel / U2D2 USB path was not touched.
- CLI `start [seconds]` is still a fixed-length debug capture.
