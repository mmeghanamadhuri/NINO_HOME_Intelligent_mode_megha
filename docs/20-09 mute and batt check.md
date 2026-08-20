# Mute + battery check — integration into the current P4 build

**Date:** 20 August 2026  
**Board:** ESP32-P4 Function EV  
**Source of behaviour:** [`U2D2_HUB_ADC_LOWBATT_MUTE.md`](U2D2_HUB_ADC_LOWBATT_MUTE.md)  
**This tree:** the current “good build” (voice, music stream, Aux-in / SD, GPIO48 demo/setup, U2D2-before-UVC). Do **not** paste that dump over live files.

This note is the integration playbook. It says **where**, **what**, and **how** so the existing audio / USB / Wi-Fi pipeline stays as it is.

---

## 1. Goal

Bring in two user-facing features from the earlier session:

1. **Speaker mute** — GPIO47 single press (and `speaker mute` on the console). Solid **red** RGB while muted.
2. **Pack voltage + low-battery alert** — on-chip ADC on **GPIO20**. At ≤ **10.0 V** the RGB **blinks red** and `low_battery.wav` plays every **20 s**, **even if the speaker is muted**. Clears at ≥ **10.4 V**.

Everything else in this firmware stays: wake/VAD, audio queue, music stream, Aux-in, SD record, GPIO48 double/triple, Wi-Fi / BLE, U2D2 + camera boot order, HTTP, eyes.

---

## 2. What we are **not** taking from that dump

The dump mixed several jobs. Only mute + battery belong in this pass.

| Dump item | This pass | Why |
|-----------|-----------|-----|
| USB-C U2D2 FTDI-SIO + hub skip list | Skip | Already in this tree (`nino_servo_dxl_start()` before `uvc_host_install()`). |
| GPIO48 single press = hardware soak (`battery_endurance.c`) | Skip | Would steal GPIO48. Keep **double = DEMO**, **triple = Wi-Fi setup**. |
| RGB scenes `SERVER_WAIT` / `SERVER_OK` | Skip | This tree uses `NINO_RGB_SHOW_SERVER_FAIL`. Keep it. |
| Wholesale replace of `audio_playback.c` / `rgb_led.c` / `main.c` | Skip | Live files have Aux-in reopen, `nino_audio_cut_speaker()`, music gapless, server-fail LED. Patch only. |

---

## 3. How it fits the current architecture

Mute and battery sit **beside** the playback pipeline. They do not add a second speaker path.

```text
GPIO47 mute ──► nino_mute_set() ──► nino_audio_set_muted()
                                      │
                                      ├─ ES8311 out-mute (codec)
                                      └─ RGB MUTE (solid red), unless battery alert

GPIO20 ADC  ──► battery_adc.c poll (2 s)
                  ├─ pack mV / %
                  └─ low alert ≤ 10.0 V / clear ≥ 10.4 V
                       ├─ nino_audio_refresh_mute()  ← charge clip still audible
                       ├─ RGB BATTERY (red blink)
                       └─ nino_audio_queue_wav_copy(..., PRIORITY_NONE)
                            existing audio_queue worker plays it
```

**Why the pipeline is undisturbed**

- Clips still go through `audio_queue.c`. Mute is `esp_codec_dev_set_out_mute` on the ES8311, not a skip in the queue.
- Low-battery uses the **existing** priority path: `NINO_AUDIO_SERVO_PRIORITY_NONE` (same as `WIFI.wav` / `Server_Unable.wav`).
- Voice, music, and Aux-in keep their current bus lock / reopen rules. Mute is applied on every speaker open.

**LED priority (highest wins)**

1. Low battery → blinking red (`NINO_RGB_SHOW_BATTERY`)
2. User mute → solid red (`NINO_RGB_SHOW_MUTE`)
3. Existing scenes: listen, Wi-Fi, server-fail, error, done, idle

While (1) or (2) is active, `nino_rgb_led_show()` **ignores** listen / Wi-Fi / server-fail so those tasks cannot steal the LED. They keep calling as they do today; the call becomes a no-op until mute/battery clears.

---

## 4. Hardware (must be wired before the ADC reads a real pack)

### 4.1 Free GPIO20 — move display RST 20 → 6

Today both eye drivers reset on **GPIO20**:

- `main/st7735.h` — `TFT_PIN_RST 20`
- `main/ssd1351.h` — `OLED_PIN_RST 20`

Battery divider needs GPIO20 as analog. **Physically move the shared RST wire to GPIO6**, then change those two `#define`s to `6`. `st7735.c` / `ssd1351.c` already pulse `*_PIN_RST`; no extra GPIO code.

GPIO6 is unused in this tree. Do not use GPIO54 for the divider (C6 slave reset pull-up). Do not wire buttons to GPIO7 (I2C SDA) or GPIO53 (speaker PA).

### 4.2 Battery divider on GPIO20

```text
Battery+ -- 22k --+-- GPIO20
                  |
               3.3k
                  |
               3.3k
                  |
Battery- ---------+-- ESP GND
```

`battery_mv = adc_mv * (22000 + 6600) / 6600`

A **12 V** pack should read ~**2.77 V** GPIO20-to-GND. Never put pack voltage on the pad.

### 4.3 Mute button on GPIO47

Same as GPIO48: active-low to GND, internal pull-up. J1 pin 37 on the Function EV board.

GPIO48 stays as it is.

---

## 5. File map — where to change

### 5.1 New files (copy behaviour from the dump, adapt names)

| File | Role |
|------|------|
| `main/battery_adc.h` | Public API: init / read / `nino_battery_low_alert_active()` / CLI |
| `main/battery_adc.c` | ADC1 oneshot on GPIO20, 16-sample average, low-battery task |
| `main/low_battery.wav` | Spoken charge prompt (~110 KB PCM). Embed via CMake. **Not in this tree yet — copy from the session that built the dump.** |

Dump source: dump §13.5. Adapt one name: this tree has `nino_rgb_led_current()`, not `nino_rgb_led_current_show()`. Call `nino_rgb_led_current()` in the low-battery task.

Do **not** add `battery_endurance.c` / `.h`.

### 5.2 Surgical edits (keep current files)

| File | What | How |
|------|------|-----|
| `main/CMakeLists.txt` | Build + embed | Add `esp_adc` to `REQUIRES`. Add `battery_adc.c` to `SRCS`. Keep `fatfs` / `sdmmc`. Add `"low_battery.wav"` to `EMBED_FILES`. |
| `main/st7735.h` | Free GPIO20 | `TFT_PIN_RST` **20 → 6**. Comment: battery ADC is GPIO20. |
| `main/ssd1351.h` | Same if OLED build | `OLED_PIN_RST` **20 → 6**. |
| `main/audio_playback.h` | Mute API | Add `nino_audio_set_muted`, `nino_audio_is_muted`, `nino_audio_refresh_mute`. |
| `main/audio_playback.c` | Codec mute | See §6.1. Do not rewrite play/decode/cut. |
| `main/rgb_led.h` | Mute scene | Add `NINO_RGB_SHOW_MUTE` after `BATTERY`. Keep `nino_rgb_led_current` and `SERVER_FAIL`. |
| `main/rgb_led.c` | Solid red + gates | See §6.2. Keep existing Wi-Fi / server-fail / listen scenes. |
| `main/push_buttons.h` | Mute helper | Document GPIO47. Add `void nino_mute_set(bool muted);` |
| `main/push_buttons.c` | GPIO47 poll | See §6.3. Do **not** add soak / `BTN_EVT_SOAK`. |
| `main/main.c` | Boot + CLI | See §6.4. Do not reorder USB / UVC / voice. |
| `sdkconfig.defaults` | Comment only | RST is GPIO **6**, not 20. No Kconfig change required for ADC. |
| `docs/TFT_RGB_WIRING.md` | Pin table | RES **20 → 6**. Note GPIO20 = battery ADC. |

### 5.3 Files that stay untouched

`audio_queue.c` / `.h` — already has `nino_audio_queue_wav_copy` + `NINO_AUDIO_SERVO_PRIORITY_NONE` + `nino_audio_queue_preempt_for_wake()`.  
`voice_assist.c`, `music_stream.c`, `sd_record.c`, `mic_input.c`, `servo_dxl.c`, `nino_eye.c` — LED calls keep working because of the RGB gate.  
`audio_queue.c` LED calls (`DONE` / `IDLE` / `ERROR`) become no-ops while muted or in battery alert; that is intended.

---

## 6. How to patch each live file

### 6.1 `audio_playback` — mute without changing PCM flow

Add `#include "battery_adc.h"` and:

```c
static volatile bool s_user_muted;
```

**Open path** — `spk_stream_open_locked()` today always does `set_out_mute(s_spk, false)`. Change to:

```c
(void)esp_codec_dev_set_out_mute(s_spk, s_user_muted &&
                                     !nino_battery_low_alert_active());
```

**Helpers** (after `nino_audio_get_volume_percent`):

- `apply_codec_mute_locked()` — `mute = s_user_muted && !nino_battery_low_alert_active()`
- `nino_audio_is_muted()` — return `s_user_muted`
- `nino_audio_refresh_mute()` — take mutex, apply, give (battery task calls this on enter/exit)
- `nino_audio_set_muted(bool)` — store flag, apply, log

Leave `nino_audio_cut_speaker()`, gapless write, Aux-in `s_spk_force_reopen`, and decode/play as they are. Temporary mute-on-close inside `play_decoded` / `cut_speaker` is pause/stop, not user mute; do not remove it.

`nino_battery_low_alert_active()` is false until ADC init, so speaker boot is unchanged.

### 6.2 `rgb_led` — add MUTE, keep current scenes

**Enum** (insert only MUTE; do not drop `SERVER_FAIL`):

```c
NINO_RGB_SHOW_BATTERY,    /* red blink 400 ms — pack ≤ 10 V */
NINO_RGB_SHOW_MUTE,       /* solid red — speaker muted */
NINO_RGB_SHOW_OTA,
...
NINO_RGB_SHOW_SERVER_FAIL,
```

**`nino_rgb_led_show()` gates** at the top (include `audio_playback.h` + `battery_adc.h`):

```c
if (nino_battery_low_alert_active() && show != NINO_RGB_SHOW_BATTERY) {
  return ESP_OK;
}
if (nino_audio_is_muted() && show != NINO_RGB_SHOW_MUTE &&
    show != NINO_RGB_SHOW_BATTERY) {
  return ESP_OK;
}
```

Do **not** add `nino_battery_endurance_owns_actuators()` — that module is not in this pass.

**Scenes**

- `NINO_RGB_SHOW_MUTE` — `nino_rgb_led_set_named("red", 255)` (solid).
- `NINO_RGB_SHOW_BATTERY` — change from **orange 800 ms** to **red 400 ms** forever (`r=255,g=0,b=0`, period `400 * 1000`, count `-1`) so it is distinct from mute (solid) and error (red 200 ms).

Keep `nino_rgb_led_current()` (used by `apply_server_watch_led` in `main.c`). Battery code should call this, not a new name.

CLI: `rgb show mute` in the `show` switch and help text.

### 6.3 `push_buttons` — GPIO47 only

Keep GPIO48 debounce / 350 ms gap / 2=demo / 3=setup.

Add:

- `BTN_MUTE_GPIO GPIO_NUM_47`
- `BTN_EVT_MUTE`
- second `btn_state_t mute` polled in the same 20 ms task
- On mute **release**, post `BTN_EVT_MUTE` immediately (do not wait for the multi-click gap)
- `gpio_config` bit mask: GPIO48 **and** GPIO47

```c
static void apply_user_mute(bool muted) {
  (void)nino_audio_set_muted(muted);
  if (muted) {
    nino_audio_queue_preempt_for_wake(); /* cut current clip */
    if (!nino_battery_low_alert_active()) {
      (void)nino_rgb_led_show(NINO_RGB_SHOW_MUTE);
    }
  } else if (!nino_battery_low_alert_active()) {
    (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  }
}

void nino_mute_set(bool muted) { apply_user_mute(muted); }
```

Worker handles `BTN_EVT_MUTE` → `toggle_user_mute()`. No soak event. Includes: `audio_playback.h`, `battery_adc.h`, `rgb_led.h`.

### 6.4 `main.c` — boot + console only

**Includes:** `#include "battery_adc.h"`

**Boot** — after `nino_audio_init()`, before `nino_audio_queue_start()`:

```c
if (nino_battery_adc_init() != ESP_OK) {
  ESP_LOGW(TAG, "GPIO20 battery ADC init failed");
}
```

The low-battery task already waits **8 s** so the audio queue exists before the first `low_battery.wav`. Do not move eye init, Wi-Fi, USB, U2D2, or UVC.

**Console** — in `console_init()`, next to `speaker_cli_register()`:

```c
nino_battery_adc_cli_register();
```

**`cmd_speaker`** — keep `speaker volume`. Add mute (same as dump §13.8 fragment):

- `speaker mute [on|off|toggle]` → `nino_mute_set(...)`
- `speaker unmute` → `nino_mute_set(false)`
- no args: print volume **and** mute state

Help string: `speaker volume [0-100] | speaker mute [on|off|toggle]`

**`server_fail_led_can_takeover()`** — already limited to idle / done / wifi-ok / server-fail. MUTE and BATTERY are not in that list, so server-watch will not overwrite them even before the RGB gate. No change required; the gate is the safety net for listen / Wi-Fi callers.

### 6.5 `battery_adc.c` — copy then two local edits

Start from dump §13.5. Then:

1. Use `nino_rgb_led_current()` instead of `nino_rgb_led_current_show()`.
2. Keep `say_low_battery()` on `nino_audio_queue_wav_copy(..., NINO_AUDIO_SERVO_PRIORITY_NONE, false)` — already matches this tree.
3. Thresholds stay: enter **10000 mV**, clear **10400 mV**, ignore below **8000 mV** (open divider), poll **2000 ms**, repeat clip **20000 ms**.

On enter: `s_low_alert = true` → `nino_audio_refresh_mute()` → `NINO_RGB_SHOW_BATTERY` → queue clip.  
On exit: clear alert → refresh mute → MUTE if still muted, else IDLE.

---

## 7. Boot order after integration

Unchanged except the one ADC call:

1. Eyes (`nino_display_init` — RST now GPIO6)
2. RGB, Wi-Fi, BLE
3. `nino_audio_init()` (ES8311)
4. SD / music (unchanged)
5. **`nino_battery_adc_init()`** ← new
6. `nino_audio_queue_start()` + GPIO48/47 buttons
7. Console + HTTP
8. USB host → `usb_lib_task` → U2D2 → face track → UVC (unchanged)

---

## 8. Console after integration

| Command | Action |
|---------|--------|
| `adc` | One-shot GPIO20 pack voltage |
| `adc log [ms]` | Periodic log (default 1000 ms) |
| `adc stop` | Stop log |
| `speaker mute [on\|off\|toggle]` | Mute + solid red (unless battery blink) |
| `speaker unmute` | Unmute |
| `rgb show mute` | Preview solid red |
| `rgb show battery` | Preview blinking red |

GPIO48 commands / behaviour unchanged.

---

## 9. Integration order (do this, then flash)

1. Place `main/low_battery.wav` (PCM 16-bit, playable by the existing WAV decoder).
2. Add `battery_adc.h` / `battery_adc.c` and CMake (`esp_adc` + embed wav).
3. Move RST `#define` 20 → 6 (and the physical wire).
4. Patch `audio_playback` mute API.
5. Patch `rgb_led` MUTE + gates + battery blink colour.
6. Patch `push_buttons` GPIO47.
7. Patch `main.c` init + CLI.
8. Build. Confirm no missing `low_battery.wav` embed symbols.

Suggested build check: `idf.py build` and grep the map / log for `battery_adc` and `_binary_low_battery_wav_start`.

---

## 10. How to test (without breaking the current demo)

1. Flash. Eyes still reset (RST on GPIO6). Camera + U2D2 + voice still boot as today.
2. GPIO48 double / triple still demo / Wi-Fi setup. Single press still ignored.
3. `speaker mute` / GPIO47 → speaker silent, **solid red**. Unmute → LED off (or previous Wi-Fi/server LED on the next watch tick).
4. While muted, `Ok Nino` / TTS / music still run in the queue but are silent. Servos and eyes unchanged.
5. Meter GPIO20-GND ≈ 2.77 V at 12 V. `adc` matches (within calibration).
6. Pack ≤ 10.0 V → **red blink** + `low_battery.wav` every 20 s, **also while muted**. Charge above 10.4 V → alert ends; if still muted, solid red returns.
7. `adc` with the divider unplugged should sit near 0 V and **must not** fire low-battery (threshold `LOW_MIN_VALID_MV` 8.0 V).

---

## 11. Risks / keep-in-mind

- **RST not moved on the PCB** → eyes stay in reset or garbage, and GPIO20 is not a clean ADC.
- **Do not paste dump `audio_playback.c`** → you would lose Aux-in reopen and pause/cut.
- **Do not paste dump `rgb_led.c`** → you would drop `SERVER_FAIL` and pull in soak-test locks.
- **Do not paste dump `push_buttons.c`** → GPIO48 single press would start hwtest.
- Mute + battery both want the RGB: battery wins; mute LED is restored on recovery.
- `nino_audio_queue_preempt_for_wake()` on mute will cut the current clip (including music). That matches the dump and is the right UX.

---

## 12. Dump cross-reference

Full source of the *old* tree (for copy-paste of `battery_adc` and the mute snippets only):

- Dump §3 hardware / divider
- Dump §7–9 behaviour and thresholds
- Dump §13.5 `battery_adc.h` / `battery_adc.c`
- Dump §13.6 mute button + RGB MUTE + `nino_audio_set_muted`
- Dump §13.8 `cmd_speaker` mute fragment (around the `speaker mute` block)

Use those as the spec. Patch **this** tree; do not replace it.
