# Music streaming to the NiNO speaker — firmware changes

Server-side work is **done and on `main`**. This document lists everything that must
change in `main/` firmware to make "play believer" come out of the robot speaker.

Nothing here is implemented on the board yet. Until it is, the server answers music
commands with *"I cannot play music on my speaker yet. My music firmware is not
installed."* (the server detects HTTP 404 from `/music/play`).

---

## 1. Why `/play_wav` cannot be reused

| Constraint | Value | Source |
|---|---|---|
| Max body accepted | **384 KiB** | `MAX_PLAY_WAV_BYTES`, `main/main.c:130` |
| That equals, at 16 kHz mono | **~12 seconds** | 384 KiB / (16000 × 2 B) |
| Buffering model | whole clip into PSRAM, then decode, then play | `play_wav_handler`, `main/main.c:2020-2109` |
| Codec lifetime | opened per clip, **closed after** | `spk_stream_close_locked()`, `main/audio_playback.c:522` |
| Ring buffer between net and DAC | **none** | `audio_queue.c` queues whole-WAV pointers only |

`docs/AUDIO_STREAMING_FLOW.md` already states `/play_wav` must not be stretched into a
streaming path. A 3-minute song at 32 kHz is ~11 MB, so it must be streamed.

---

## 2. Architecture

Pull-based: the board fetches audio itself, so backpressure is handled by TCP and the
server never has to guess the board's consumption rate.

```
voice: "play believer"
   |
   v
[PC server] resolve track ---> POST http://<esp>/music/play {"url": "..."}
   |                                            |
   |                                            v
   |                              [ESP] esp_http_client GET stream
   |                                            |
   +---- GET /music/stream.wav <----------------+
             (44-byte WAV header, then continuous
              mono 16-bit PCM @ 32 kHz)
                                                |
                                                v
                                     PSRAM ring buffer (256 KiB)
                                                |
                                                v
                                  music_feed task -> esp_codec_dev_write -> ES8311
```

### Server endpoints that already exist

| Endpoint | Method | Purpose |
|---|---|---|
| `/music/stream.wav?device_id=<id>` | GET | The PCM stream the board pulls |
| `/api/music/play` | POST | `{"query": "believer", "device_id": "..."}` — resolve + tell board to start |
| `/api/music/stop` | POST | Stop and tear down the stream |
| `/api/music/status` | GET | Current track, elapsed, decoder health |

### Stream format

- **RIFF/WAVE, PCM, 1 channel, 16-bit little-endian**
- **Sample rate 32000 Hz** by default (`MUSIC_STREAM_HZ` in `server/.env`, clamped 8000–48000)
- Data rate **64 KB/s** (512 kbps)
- Both RIFF size fields are `0xFFFFFFFF` because length is unknown up front

> **Firmware must not trust the size fields.** Parse the 44-byte header for
> `sample_rate`/`channels`/`bits`, then read until the socket closes.

---

## 3. New firmware files

### `main/music_stream.h`

```c
#pragma once
#include "esp_err.h"
#include <stdbool.h>

esp_err_t nino_music_init(void);
esp_err_t nino_music_start(const char *url);   // copies url, starts puller
void      nino_music_stop(void);               // idempotent
bool      nino_music_is_playing(void);
void      nino_music_pause_for_speech(bool paused);  // duck for wake/TTS
```

### `main/music_stream.c`

Responsibilities:

1. `esp_http_client` GET on the URL, `esp_http_client_read()` in a loop.
2. Parse the 44-byte WAV header once, then push PCM into a ring buffer.
3. A `music_feed` task drains the ring into `esp_codec_dev_write()`.
4. Prebuffer before the first write to survive Wi-Fi jitter.

Suggested constants:

```c
#define MUSIC_RING_BYTES        (256 * 1024)  // PSRAM, ~4 s at 32 kHz mono
#define MUSIC_PREBUFFER_BYTES   (96 * 1024)   // ~1.5 s before first sample
#define MUSIC_HTTP_CHUNK        4096
#define MUSIC_WRITE_CHUNK       4096          // matches audio_playback.c:505
#define MUSIC_FEED_TASK_STACK   6144
#define MUSIC_FEED_TASK_PRIO    4             // > wake (3), < audio_play (6)
#define MUSIC_HTTP_TASK_STACK   8192
#define MUSIC_HTTP_TASK_PRIO    4
```

Allocate the ring in PSRAM, matching the existing pattern in `main.c:2036`:

```c
uint8_t *ring = heap_caps_malloc(MUSIC_RING_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
```

Use a FreeRTOS `StreamBuffer` — `usb_mic.c` already does this for the 64 KiB mic ring
and is the closest working reference.

---

## 4. Changes to existing files

### 4.1 `main/audio_playback.c` — implement the missing incremental write

`nino_audio_write_pcm16_mono_locked()` is **declared in `audio_playback.h` but has no
implementation**. This is the hook the music feed needs.

```c
esp_err_t nino_audio_write_pcm16_mono_locked(const void *pcm, size_t bytes) {
  if (!s_ready || s_spk == NULL || pcm == NULL || bytes == 0) {
    return ESP_ERR_INVALID_STATE;
  }
  size_t offset = 0;
  while (offset < bytes) {
    int block = (int)(bytes - offset);
    if (block > MUSIC_WRITE_CHUNK) block = MUSIC_WRITE_CHUNK;
    int cr = esp_codec_dev_write(s_spk, (void *)((const uint8_t *)pcm + offset), block);
    if (cr != ESP_CODEC_DEV_OK) return ESP_FAIL;
    offset += (size_t)block;
  }
  return ESP_OK;
}
```

The caller must already hold the bus mutex (see §5).

### 4.2 Keep the codec open across the whole track

`spk_stream_open_locked(rate, leave_open=true)` already exists for the wake chime
(`audio_playback.c:263-283`). Reuse it: open once at 32 kHz when the track starts,
close only on stop/end. Do **not** open/close per chunk — that reconfigures I2S and
audibly clicks.

Note `spk_stream_open_locked()` calls `nino_voice_wake_drop_mic_locked()`
(`audio_playback.c:79-82`), so the ES8311 mic handle is dropped for the whole track.

### 4.3 `main/main.c` — new HTTP endpoints

Register alongside the existing handlers in `start_http_server()` (`main.c:3427`):

| Route | Method | Body | Response |
|---|---|---|---|
| `/music/play` | POST | `{"url":"http://<pc>:8000/music/stream.wav?device_id=x"}` | `{"ok":true}` |
| `/music/stop` | POST | *(empty)* | `{"ok":true}` |
| `/music/status` | GET | — | `{"ok":true,"playing":bool}` |

The handler should copy the URL, call `nino_music_start()`, and return immediately —
do not block the httpd task for the track duration. Follow the existing async pattern
of `play_wav_handler`, which returns `{"ok":true,"queued":true}` right away.

**Important:** the server treats HTTP 404 on `/music/play` as "firmware too old" and
says so out loud. Any other failure should return a non-200 with a short JSON reason.

### 4.4 `main/CMakeLists.txt`

`esp_http_client` is already in `REQUIRES` (used for the Wi-Fi report POST at
`main.c:865-880`), so no new dependency. Just add `music_stream.c` to `SRCS`.

---

## 5. Coexistence with voice — the part most likely to bite

All speaker access is serialized by `nino_audio_bus_lock()`. Music must **not** hold
that mutex for the whole track, or TTS and the ES8311 mic starve.

Rule: acquire the mutex per write chunk, release immediately.

```c
nino_audio_bus_lock();
nino_audio_write_pcm16_mono_locked(chunk, n);
nino_audio_bus_unlock();
```

### Required interactions

| Event | Required music behaviour |
|---|---|
| Wake word detected | `nino_music_pause_for_speech(true)` before the wake chime |
| TTS reply playing (`audio_queue` job) | stay paused; `audio_play` is prio 6 and will win anyway |
| VAD capture window | stay paused — `voice_assist.c:331` sets `set_mic_capture_hold(true)` |
| Reply finished, `continue_listen` false | resume music |
| Voice command was "stop the music" | tear the stream down, do not resume |
| Track ends (socket closes) | close codec, notify server via `/music/status` polling |

Hook the pause into `nino_audio_queue_preempt_for_wake()` (`audio_queue.c:391-406`),
which already stops normal clips within a 150 ms deadline.

### Microphone bleed — no AEC

`voice_wake.cpp:48-55` explicitly disables AEC, NS, AGC:

```c
afe_cfg->aec_init = false;
afe_cfg->ns_init  = false;
afe_cfg->agc_init = false;
```

Consequences you must plan for:

- Music will bleed into the mic and **can false-trigger WakeNet**. Test at your
  intended volume; consider capping music volume, or ducking to ~30% rather than
  fully pausing, and re-test wake reliability.
- The **ES8311 fallback mic is unusable during playback** because the speaker holds
  the shared duplex I2S. **Barge-in requires the USB 4-mic array** (`usb_mic.c`),
  which bypasses that mutex (`mic_input.c:92-122`).

---

## 6. Volume and amplifier

No changes needed. `/speaker/volume` (`main.c:2253-2341`) already calls
`esp_codec_dev_set_out_vol()` and persists to NVS (`nino_audio`/`vol`, default 80).
The power amp on **GPIO53** (`BSP_POWER_AMP_IO`) is driven by the ES8311 driver and
stays enabled while the codec is open.

Consider a separate music volume so a loud song does not also make TTS shout.

---

## 7. Bandwidth and memory budget

| Item | Value |
|---|---|
| Stream bitrate | 512 kbps (64 KB/s) at 32 kHz mono 16-bit |
| Ring buffer | 256 KiB PSRAM |
| Peak extra PSRAM | < 300 KiB |
| Wi-Fi | via ESP32-C6 over SDIO — 512 kbps is well within capacity |

If Wi-Fi proves marginal (RSSI on `nino-home-147` is **-51 dBm**, which is healthy),
drop `MUSIC_STREAM_HZ` to 22050 (44 KB/s) or 16000 (32 KB/s). The server clamps to
8000–48000 and the board should honour whatever the WAV header declares.

---

## 8. Bring-up order

1. **Verify the stream from a PC first** — no firmware needed. Pass
   `"push_to_device": false` so the server arms the stream without calling
   `/music/play` on the board (which would 404 until step 5 is done):
   ```bash
   curl -X POST localhost:8000/api/music/play \
        -H 'Content-Type: application/json' \
        -d '{"query":"kalyani","device_id":"nino-home-147","push_to_device":false}'
   ffplay -autoexit "http://localhost:8000/music/stream.wav?device_id=nino-home-147"
   ```
   If that plays, the server half is correct and every remaining bug is on the board.
   Each `play` call arms exactly one stream; re-run the `curl` before each `ffplay`.

2. Implement `nino_audio_write_pcm16_mono_locked()` and prove it with a locally
   generated sine wave — no networking involved.

3. Add the ring buffer + `music_feed` task, fed by that same sine generator.

4. Add `esp_http_client` pulling from the server. Watch for underruns in the log.

5. Add `/music/play` and `/music/stop`, then test by voice.

6. Test coexistence last: play a track, say the wake word, confirm the reply is
   audible and music resumes.

---

## 9. Audio source

Songs resolve through **JioSaavn** (`music_source._resolve_via_saavn`). Its search
needs no authentication, and the CDN URLs it returns are **not bound to the requesting
IP** — which matters here, because this site's egress address rotates
(`103.103.209.146` → `103.103.209.141` within seconds).

Media URLs arrive DES-ECB encrypted; `decrypt_saavn_url()` unwraps them and the
resolver upgrades the rendition to 320 kbps when the CDN carries it.

Measured on this network, resolve plus first audio:

| Query | Resolved | Time |
|---|---|---|
| believer | Believer by Imagine Dragons | 0.5 s |
| finding her kushagra | Finding Her by Kushagra | 0.4 s |
| shape of you | Shape of You by Ed Sheeran | 0.2 s |
| tum hi ho | Tum Hi Ho by Mithoon | 0.3 s |
| blinding lights | Blinding Lights by The Weeknd | 0.2 s |

**yt-dlp is a disabled fallback.** Set `MUSIC_ENABLE_YTDLP=1` to try YouTube when
JioSaavn has no match, but note it currently fails here: YouTube requires a PO Token
and every player client returns HTTP 403 from this network.

`music_source.resolve_track()` remains the single swap point — any source yielding a
URL ffmpeg can open works unchanged.
