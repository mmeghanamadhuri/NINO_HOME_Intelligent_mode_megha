# USB 4-Mic Integration — Change Log

This document tracks the firmware changes that **replace the onboard ES8311 microphone** with the **USB 4-mic array on the 40-pin GPIO header** (GPIO 24/25), while leaving camera, servos, speaker, touch, and eyes unchanged.

Reference guide: [Integrate-4mic.md](Integrate-4mic.md)

---

## Summary

| Before | After |
|--------|-------|
| Voice input via ES8311 codec (`bsp_audio_codec_microphone_init`) | Voice input via USB UAC on GPIO header (`usb_mic_read`) |
| Mic + speaker shared ES8311 I2S bus lock | Speaker only uses ES8311; mic is independent USB path |
| A/D conversion on board (ES8311 ADC → I2S) | A/D inside USB mic; ESP receives digital UAC PCM only |
| J18 USB hub: camera + U2D2 | **Unchanged** — still J18 only |
| USB mic power/data | **Separate** — 5V + GND + GPIO 24 (D−) + GPIO 25 (D+) on header |

---

## Hardware wiring (unchanged from Integrate-4mic.md)

| USB wire | Board connection |
|----------|------------------|
| Red (VCC) | **5V** on header |
| Black (GND) | **GND** |
| White (D−) | **GPIO 24** |
| Green (D+) | **GPIO 25** |

Use a **different USB port** on the PC for flash/serial debug — not the mic header.

---

## Analog-to-digital conversion (where A/D happens now)

### Old path (ES8311 onboard mic)

```text
Sound (analog)
    → ES8311 codec ADC          ← A/D on the board
    → I2S digital bus
    → esp_codec_dev_read()
    → int16 PCM → wake / VAD → WAV
```

### New path (USB 4-mic on GPIO header)

```text
Sound (analog)
    → USB mic internal hardware   ← A/D inside the mic (MEMS + ADC / USB audio chip)
    → Digital USB Audio Class stream (GPIO 24/25)
    → ESP USB host + uac_host driver
    → uac_host_device_read()      ← already 16-bit PCM bytes
    → usb_mic.c (mono mix, resample to 16 kHz)
    → usb_mic_read() → wake / VAD → WAV
```

The ESP32-P4 **does not perform ADC for voice capture** anymore. Firmware only receives and processes **digital PCM** from USB.

| Stage | Module | What it does |
|-------|--------|--------------|
| A/D | USB mic hardware | Analog sound → digital PCM (outside ESP) |
| USB RX | `usb_mic.c` | UAC isochronous read via `uac_host_device_read()` |
| Format | `process_uac_rx_chunk()` | ReSpeaker: **ch0** beamformed mono; others: average; resample to **16 kHz** |
| Buffer | Ring buffer (`StreamBuffer`) | ~**2 s** of 16 kHz mono `int16` samples |
| Consumer | `usb_mic_read()` | Wake word + VAD (mutex-serialized; one reader at a time) |

**ReSpeaker 4-mic (2886:0018):** UAC **interface 2**, 6 ch @ 16 kHz → firmware uses **channel 0** only.

**ES8311 is speaker-only now** (beep, TTS playback uses the codec **DAC**, not ADC).

---

## Firmware flow: “Hi ESP” → playback

Always-on background tasks after boot:

| Task / module | Role |
|---------------|------|
| `usb_mic.c` | USB mic on GPIO 24/25 → 16 kHz mono ring buffer |
| `wake_feed` (`voice_wake.cpp`) | `usb_mic_read()` → AFE feed (**paused** during beep/VAD) |
| `wake_fetch` (`voice_wake.cpp`) | WakeNet **“Hi ESP”** — must never block on speaker I/O |
| `after_wake_task` | Beep → listening eyes → flush → VAD + WebSocket |
| `audio_queue.c` | Plays server/TTS WAV on ES8311 speaker |

### Step-by-step (firmware only)

```text
1. Wake word (voice_wake.cpp)
   USB mic → wake_feed → AFE (wake-only) → wake_fetch detects "Hi ESP"
   → s_after_wake_busy = true; wake_feed pauses

2. Chime + query (after_wake_task)
   nino_voice_play_wake_chime() → nino_eye_listening() → usb_mic_flush()
   → nino_voice_assist_run_query_only()

3. VAD (voice_assist.c)
   mic_capture_hold + usb_mic_read(); 450 ms trailing silence → WAV

4. WebSocket (voice_ws_client.c) → reply WAV + JSON metadata

5. Playback (audio_queue.c) → ES8311 + eyes + done beep
```

**Do not** play beep inside `wake_fetch` — blocks `fetch()` while feed runs → AFE ring full + delayed chime.

```mermaid
sequenceDiagram
    participant Mic as USB 4-mic (GPIO)
    participant Wake as voice_wake.cpp
    participant Eyes as nino_eye
    participant VAD as voice_assist.c
    participant WS as voice_ws_client.c
    participant Q as audio_queue.c
    participant Spk as ES8311 speaker

    Mic->>Wake: usb_mic_read → AFE (continuous)
    Wake->>Wake: WakeNet "Hi ESP"
    Wake->>Wake: pause AFE feed; spawn after_wake_task
    Wake->>Spk: beep.wav (cached)
    Wake->>Eyes: listening
    Wake->>VAD: run_query_only()

    loop VAD max ~10 s
        Mic->>VAD: usb_mic_read 20 ms frames
        VAD->>VAD: speech / silence → PCM buffer
    end

    VAD->>WS: send query WAV binary
    WS->>Eyes: thinking
    WS->>WS: receive reply WAV binary
    VAD->>Q: queue reply WAV
    Q->>Spk: play TTS WAV
    Q->>Eyes: idle when done
```

**Input:** USB 4-mic on GPIO header. **Output:** ES8311 onboard speaker. **PC bridge:** WebSocket only (WAV in, WAV out).

---

## Data when the user stops speaking

There are two distinct moments.

### Moment 1 — User stops talking (local capture, nothing received yet)

VAD detects **450 ms trailing silence** (`VAD_TRAILING_SILENCE_MS` in `voice_assist.c`) and ends recording.

The board is **not receiving data from the PC** yet. It **already built** a local buffer from the USB mic:

| Property | Value |
|----------|--------|
| Format | Standard **WAV file** in RAM |
| Audio | **16 kHz**, **mono**, **16-bit signed PCM** |
| Structure | 44-byte RIFF header + raw PCM (`data` chunk) |
| Content | Speech segment + ~200 ms pre-roll before speech started |
| Typical size | Few KB up to ~320 KB (max ~10 s speech) |

This is **digital audio only** — not text, not JSON.

### Moment 2 — Board sends query WAV, then receives the reply

`nino_voice_ws_exchange()` sends the query WAV as a **WebSocket binary frame**, then waits for the PC response.

**What the board receives back:**

| Frame type | WebSocket opcode | Content |
|------------|------------------|---------|
| **Text (optional)** | `0x01` | Small JSON, e.g. `{"prompt_medical_ack": false, "eye_expression": "happy"}` |
| **Binary (main)** | `0x02` | **Reply WAV** — TTS audio (typically 16 kHz mono PCM), reassembled from chunks (up to 4 MB buffer) |

Firmware parses JSON for:

- **`eye_expression`** → OLED eyes during playback
- **`prompt_medical_ack`** → schedule a second VAD listen after the reply (medical alarms)

The board does **not** receive transcribed text or LLM prose on the WebSocket — only **WAV audio** plus optional **JSON metadata**. STT / LLM / TTS run on the PC.

```text
User stops talking
       │
       ▼
[LOCAL]  VAD finishes → query WAV in RAM (16 kHz mono)
       │
       ▼
[SEND]   WebSocket binary → PC
       │
       ▼
[WAIT]   eyes: thinking
       │
       ▼
[RECEIVE]
   • Text JSON  (eye tag, medical flag) — optional
   • Binary WAV (TTS reply)             — main payload
       │
       ▼
[LOCAL]  audio_queue → ES8311 speaker plays reply WAV
```

---

## Files added

| File | Purpose |
|------|---------|
| `main/usb_mic.c` | USB FS host on header PHY, UAC RX capture, resample to 16 kHz mono, ring buffer |
| `main/usb_mic.h` | Public API: `usb_mic_start()`, `usb_mic_ready()`, `usb_mic_read()` |
| `main/Kconfig.projbuild` | `CONFIG_USB4MIC_*` GPIO menu |

---

## Files modified

| File | Changes |
|------|---------|
| `main/idf_component.yml` | Added `espressif/usb_host_uac: ^1.3.3` |
| `main/CMakeLists.txt` | Added `usb_mic.c` to `SRCS` |
| `sdkconfig.defaults` | `CONFIG_USB4MIC_USB_PHY_ON_HEADER=y`, GPIO 24/25, `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y` |
| `main/main.c` | `#include "usb_mic.h"`, dual USB host, `usb_mic_start()`; **blocks UAC on UVC camera addr** |
| `main/voice_wake.cpp` | USB mic wake feed/fetch; AFE wake-only tuning; beep in `after_wake_task` |
| `main/voice_assist.c` | VAD via `usb_mic_read()`; beep preload; medical ack listen |
| `main/audio_playback.c` | ES8311 speaker; `nino_audio_play_chime_pcm16_mono()` fast path |
| `main/audio_capture.c` | Generic capture uses USB mic @ 16 kHz |
| `main/voice_wake.h` | `nino_voice_wake_set_mic_capture_hold()` — exclusive mic during VAD |

---

## What was **not** changed

- **ES8311 speaker** — `audio_playback.c`, `audio_queue.c`, touch WAV, TTS playback
- **UVC camera** — `main.c` UVC pipeline on J18
- **Dynamixel / U2D2** — `servo_dxl.c` on J18 hub
- **Wake word model** — same WakeNet **"Hi ESP"**; AFE config tuned for USB mono (AEC/NS/AGC off)
- **PC server** — still receives 16 kHz mono WAV over WebSocket; reply protocol unchanged
- **OLED eyes, touch, Wi-Fi, BLE prov** — unchanged

---

## Boot flow (after integration)

```text
app_main
  ├─ Speaker init (ES8311) — playback only
  ├─ nino_audio_init() + nino_voice_preload_wake_chime()  ← decode beep + warm ES8311
  ├─ usb_mic_phy_init_for_header()          ← GPIO 24/25 FS PHY select
  ├─ usb_host_install(BIT0|BIT1)            ← ONE install: HS J18 + FS header mic
  ├─ usb_lib_task                           ← single event loop (J18 + header)
  ├─ usb_mic_start()                        ← UAC driver only (no 2nd host install)
  ├─ uvc_host_install()                     ← camera on J18
  └─ delayed_voice_wake_task (5 s) → nino_voice_wake_init()
```

**Important:** Do not call `usb_host_install()` twice. ESP-USB 1.3+ dual-host uses `peripheral_map = BIT0 | BIT1` in a **single** install. Pin `espressif/usb: ^1.3.0` in `idf_component.yml`.

---

## Expected serial logs

```
I (...) usb_mic: USB phys: HS UTMI (J18) + FS INT header D-=GPIO24 D+=GPIO25
I (...) usb_mic: UAC driver installed — waiting for USB mic on GPIO header
I (...) usb_mic: ReSpeaker 4-mic: addr=2 speed=FS parent=root (GPIO header OK)
I (...) usb_mic: USB mic UAC started addr=2 iface=2 2886:0018: 16000 Hz, 6 ch
I (...) usb_mic: USB mic isochronous RX active (addr=2 iface=2)
I (...) voice_ast: beep.wav cached: ... samples @ 16000 Hz
I (...) nino_audio: Chime path warm @ 16000 Hz
I (...) voice_wake: WakeNet model: ... (AFE: wake-only, no AEC/NS/AGC)
I (...) voice_wake: wake feed (USB mic): chunksize=... nch=... sr=16000
I (...) voice_wake: USB mic streaming — say "Hi ESP"
I (...) voice_wake: Wake word ready (USB mic) — say "Hi ESP"
```

Console: `voice status` → `usb header mic: streaming`, ReSpeaker addr/iface, `rx_chunks`, `peak`.

---

## Build / flash

```bash
idf.py set-target esp32p4
idf.py build flash monitor
```

After changing Kconfig defaults, run `idf.py fullclean` once if menuconfig symbols are missing.

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| No `USB mic isochronous RX active` | Wiring 5V/GND/GPIO 24/25; swap D+/D−; short leads |
| Wake never fires | `voice status` — `rx_chunks` increasing? `peak` when speaking? `voice wake on` |
| Wake weak vs onboard ES8311 | AFE must be wake-only (no AEC/NS/AGC); ReSpeaker **ch0** on iface **2**; 16× SW gain in `usb_mic.c` |
| Slow beep after "Hi ESP" | Beep must not block `wake_fetch`; feed paused during beep; preload + warm chime at boot |
| `AFE: Ringbuffer of AFE(FEED) is full` | Feed still running while fetch blocked — fixed by pausing feed during beep/VAD |
| `PCM ring full — dropped N bytes` | USB still RX while consumer paused (beep/VAD); ~9 ms drops; OK if occasional |
| Mic dies when camera connects | `usb_mic_block_dev_addr()` must only close mic if same USB addr — not ReSpeaker when blocking camera |
| Camera/servo broken | J18 hub still powered; header mic must **not** share J18 |
| `usb header mic: not ready` | Mic not enumerated — wiring / power |
| `uac-host: Control Transfer Timeout` | UAC tried J18 camera mic — use `usb_mic_block_dev_addr()` on UVC connect |
| `usb_host_install failed` | `idf.py fullclean` + rebuild (`espressif/usb` ^1.3.0) |
| Weak VAD | Retune `VAD_MIN_*_ENERGY` in `voice_assist.c`; trailing silence = **450 ms** |
| Medical ack crash | VAD + wake must not `usb_mic_read()` concurrently — `mic_capture_hold` mutex |

### Dual USB host note

ESP32-P4 needs **one** `usb_host_install()` with `peripheral_map = BIT0 | BIT1`:

| Bit | Port | Devices |
|-----|------|---------|
| BIT0 | USB HS | J18 hub → UVC camera + FTDI U2D2 |
| BIT1 | USB FS | GPIO 24/25 → USB 4-mic |

Requires **ESP-USB component 1.3.0+** (`idf.py` will fetch via `main/idf_component.yml`). After pulling this fix, run:

```bash
idf.py fullclean
idf.py build flash monitor
```

On boot you should see:

```
I (...) usb_mic: USB FS PHY on header: D-=GPIO24 D+=GPIO25
I (...) usb_mic: UAC driver installed — waiting for USB mic on GPIO header
I (...) usb_mic: USB mic ready: 16000 Hz ...
```

`voice status` should show `usb header mic: streaming`.

### UVC camera vs USB mic (crash fix)

Many UVC webcams expose a **UAC microphone interface** on the J18 hub. With dual-host (`BIT0 | BIT1`), the UAC driver sees those interfaces and may try to open them **before** the GPIO-header mic enumerates. Control transfers to hub/camera audio often **timeout**, then the USB host stack can **panic**.

**Fix:**

1. **GPIO header only** — accept UAC only from **Full-Speed devices on the FS root port** (`parent == NULL`, speed FULL). Skip hub children and the High-Speed J18 root (hub).
2. **`usb_mic_block_dev_addr()`** — `main.c` blocks the UVC camera USB address when it connects (does **not** close an already-open ReSpeaker on a different address).
3. **Deferred open** — UAC connect queued to `usb_mic_open` worker (120 ms settle).
4. **UVC descriptor scan** — skip devices with `USB_CLASS_VIDEO` interfaces.
5. **Exclusive read** — `usb_mic_read()` mutex; `nino_voice_wake_set_mic_capture_hold()` pauses wake feed during VAD.

Expected logs when J18 hub + header mic are both connected:

```
I (...) usb_mic: Skip UAC addr=2 — behind USB hub (J18), not GPIO header
I (...) usb_mic: UAC RX on GPIO header: addr=5 iface=1 — opening mic
I (...) usb_mic: USB mic ready: 16000 Hz ...
```

If you still see `UAC mic start failed` without `USB mic ready`, the header mic is not enumerating — check 5V/GND and GPIO 24/25 wiring.

---

## Date

Integrated: 2026-07-08  
Doc updated: 2026-07-08 (wake-only AFE, beep latency, feed/fetch pause, VAD 450 ms, ReSpeaker ch0, camera block fix, medical ack mic lock)
