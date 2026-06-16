# NiNO Voice Test Script

Say **"Hi ESP"** before each question (except medical alarm acks, where the mic re-opens automatically).
Watch `server/data/latency_log.json` to confirm the `reply_path` and timings per query.

## 1. General questions (reply_path: `llm`)

- "What is the capital of France?"
- "Tell me a joke."
- "Count numbers from one to ten."
- "What is photosynthesis?"
- "How far is the Moon from the Earth?"
- "Give me a fun fact about robots."
- "What day is it today?"
- "Explain gravity in one sentence."

Expect: short spoken answer (max ~55 words). With a registered face in frame, ~18% of replies should use your name.

## 2. Identity questions (reply_path: `identity_llm`)

Stand in front of the camera (registered face):

- "Who am I?"
- "What's my name?"
- "Do you know me?"
- "Do you know who I am?"

Then test the negative cases:

- Step out of frame → "Who am I?" (should say it can't see you)
- Have an unregistered person ask → "Do you know me?" (should say it doesn't recognize them)

## 3. Servo 360 spin (reply_path: `servo_360`)

- "Make a 360."
- "Do a 360."
- "Spin 360."
- "Rotate 360 degrees."
- "Do a full 360."

Expect: fixed reply "OK, doing the spin now", then the head does the full 512 → 0 → 1023 → 512 sweep **while/after the reply plays** (the spin must not stop early — check the ESP log shows all three "moving to" lines and no "timed out" warning).

Edge cases:

- Say "make a 360" again **while a spin is running** → "A spin is already running."
- Unplug the U2D2 and try → "The servos are not ready..."

## 4. Normal alarms (reply_path: `alarm`)

- "Set an alarm at 7:30 AM tomorrow."
- "Remind me to go to school at 8 AM."
- "Remind me to call mom at 6 PM."
- "Set a coffee reminder at 5 PM."
- "List my alarms."
- "Cancel alarm at 7:30 AM."
- "Delete my coffee reminder."
- "Cancel all alarms."

Expect: spoken confirmation; at fire time the ESP plays TTS + beep. Check the web UI at `http://localhost:8000` shows the pending rows.

Tricky phrasings (NLP fallback):

- "Wake me up in 10 minutes."
- "Remind me to drink water at half past six in the evening."

## 5. Medical (P0) reminders

- "Remind me to take medicines at 9 PM."
- "Remind me to take my tablets at 8 AM."

At fire time: spoken TTS only (no beep), then the board **auto-listens** (no wake word needed):

- Say **"yes"** / "I took it" → confirms and clears.
- Say **"no"** / "not yet" → asks *reschedule or cancel?*, mic re-opens:
  - "Reschedule for 10 PM."
  - or "Cancel it."
- Say **nothing** → repeats every 3 minutes until confirmed.

Also test the web UI **Yes / No** buttons on an awaiting-ack row.

## 6. Vision greetings (no wake word)

- Walk into frame with a registered face → personalized greeting once.
- Leave for 10+ minutes, come back → "welcome back" style greeting.
- Ask a voice question, then walk in → no greeting for ~90 s after a voice reply.

## 7. Touch priority

- While a voice reply or greeting is playing, touch the QT2120 sensor.

Expect: warning clip interrupts immediately (with nod L/R motion), then the original audio **resumes from where it stopped**.

## 8. Eyes

- Wake word accepted → **listening** (wide eye, fast blink)
- Question sent to server → **thinking** (eye rolls upward)
- Reply playing → back to **idle** (slow blink)
- Serial test: `eye listening`, `eye thinking`, `eye idle`

## 9. Robustness / abuse cases

- Say gibberish / hum → should reply gracefully or log "No speech recognized".
- Ask something inappropriate → polite refusal.
- Very long rambling question → reply still capped at ~55 words.
- Speak very quietly from 2–3 m away → tests mic + STT.
- Disconnect the PC's Wi-Fi mid-question → ESP eyes must return to idle (never stuck in thinking).
- Kill ElevenLabs (set a bad `ELEVENLABS_API_KEY`) → STT must silently fall back to Whisper (check `stt_engine` in the latency log).

## 10. Latency spot-checks

After a test session, check `server/data/latency_log.json`:

| Field | Healthy value |
|-------|---------------|
| `stt_seconds` (elevenlabs) | ~0.9–1.5 s |
| `reply_seconds` (llm) | ~1–5 s (longer answers take longer) |
| `tts_seconds` | ~1–3 s |
| `stt_engine` | `elevenlabs` (not `whisper`, unless offline) |
