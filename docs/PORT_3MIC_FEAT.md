# PORT_3MIC_FEAT — ESP32-P4 Mic DSP Features for NiNO

Detailed plan for **what audio DSP the ESP32-P4 can run**, **what NiNO uses today**, **what we can enable**, and **the proper order to implement** — including how this relates to Adaptive EOU ([ADVA.md](ADVA.md) / [ADVA_PLAN.md](ADVA_PLAN.md)).

---

## 1. Short answers


| Question                                                            | Answer                                                                                                     |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Does P4 have a separate DSP chip?                                   | **No.** DSP runs as **software** on the dual RISC-V CPU via **ESP-SR AFE** (+ esp-dsp).                    |
| Can P4 run AEC / NS / SE / VADNet / AGC / WakeNet?                  | **Yes** (with limits — see §3).                                                                            |
| Does NiNO use those today?                                          | **Almost no** — wake AFE is wake-only; capture is energy VAD.                                              |
| Is ReSpeaker beamforming already done?                              | **Yes** — firmware uses **ch0** (beamformed ASR mono).                                                     |
| Should we enable “all AFE features” before Option A (adaptive EOU)? | **No — not all.** Do **light, high-value DSP first** (optional NS), then **Option A**. Heavy AEC/SE later. |
| Does Option A require AFE DSP?                                      | **No.** Different problem: pause cutoffs vs noise/echo quality.                                            |


---



## 2. Hardware / DSP reality on ESP32-P4

Older Espressif voice boards (e.g. some LyraTD lines) paired an MCU with a **dedicated DSP ASIC** (e.g. DSPG).  

**ESP32-P4 does not.** Voice front-end work is:

```text
USB / I2S PCM
      │
      ▼
┌─────────────────────────────┐
│  ESP-SR AFE (software DSP)  │  ← runs on P4 CPU cores
│  + esp-dsp helpers          │
│  AEC / NS / SE / VAD / AGC  │
│  + WakeNet                  │
└─────────────────────────────┘
```

Implications:

- Enabling more AFE blocks costs **CPU time + RAM**, not a free DSP chip.
- Must stay within NiNO’s other load (camera, face track, USB host, Wi-Fi, eyes, servos).
- Prefer **LOW_COST / selective** enablement — not “turn everything on.”

---



## 3. ESP-SR AFE feature matrix (P4)


| Feature                                                     | Supported on P4? | Typical need                                      | Notes for NiNO                                                                                      |
| ----------------------------------------------------------- | ---------------- | ------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| **AEC** (acoustic echo cancellation)                        | Yes              | Speaker reference (playback PCM aligned with mic) | Up to **2-mic** in AFE docs; hard because TTS plays on ES8311 while mic is USB                      |
| **NS** (noise suppression)                                  | Yes              | Mono (or processed) stream                        | Best on **stationary** noise (fan, AC); helps STT + energy VAD                                      |
| **BSS / SE** (blind source separation / speech enhancement) | Yes              | **Dual-mic** input                                | Needs 2 channels into AFE; we currently feed **mono ch0**                                           |
| **VAD (neural / VADNet)**                                   | Yes              | Optional in AFE                                   | We use **energy VAD** in `voice_assist.c` today                                                     |
| **AGC**                                                     | Yes              | Level normalize                                   | Can help quiet / loud speakers; can also pump noise                                                 |
| **WakeNet**                                                 | Yes              | Always-on wake                                    | **Already in use** (“Hi ESP” / “Jarvis”)                                                            |
| **Beamforming**                                             | Partial          | Multi-mic array                                   | AFE is **not** a full 4-mic BF engine; closest is dual-mic BSS/MISO. **ReSpeaker already BF → ch0** |


---



## 4. What NiNO does today



### 4.1 Mic path

```text
ReSpeaker 4-mic (USB UAC)
  → 6 ch @ 16 kHz (typical)
  → usb_mic.c picks ch0 (beamformed ASR mono)
  → 16 kHz mono ring buffer
  → consumers: wake_feed OR VAD capture (exclusive)
```

Speaker path is separate: **ES8311 DAC only** (no mic ADC on that codec for voice capture).

### 4.2 Wake AFE (current config)

From `voice_wake.cpp` → `configure_wake_afe()`:


| Flag         | Value               | Meaning              |
| ------------ | ------------------- | -------------------- |
| Input layout | `"M"` (mono)        | Matches USB ch0      |
| Mode         | `AFE_MODE_LOW_COST` | CPU-friendly         |
| `aec_init`   | **false**           | No echo cancel       |
| `se_init`    | **false**           | No dual-mic SE       |
| `ns_init`    | **false**           | No noise suppress    |
| `vad_init`   | **false**           | No neural VAD in AFE |
| `agc_init`   | **false**           | No AGC               |
| WakeNet      | **on**              | Recognition only     |


Comment in code (accurate): USB array already provides beamformed mono; wake needs recognition, not a second full DSP pipeline.

### 4.3 Query capture

```text
after wake → pause wake feed → exclusive mic
  → voice_assist.c energy VAD
  → WAV → WebSocket → PC STT (ElevenLabs / Whisper)
```

No AFE on the capture path today.

### 4.4 Summary diagram (today)

```text
                    ┌──────────────┐
 USB ch0 mono ─────►│ wake_feed    │──► AFE (WakeNet only) ──► wake_fetch
                    └──────────────┘
                           │ mic_capture_hold
                           ▼
                    ┌──────────────┐
                    │ voice_assist │──► energy VAD ──► WAV ──► WS ──► STT
                    └──────────────┘

 ES8311 speaker ◄── audio_queue / TTS   (no AEC reference today)
```

---



## 5. Can we implement the features above on P4?

**Yes — selectively.** Not “enable all at once.”


| Feature                     | Feasible on NiNO P4? | Difficulty | Worth it?                                     | Blockers                                                |
| --------------------------- | -------------------- | ---------- | --------------------------------------------- | ------------------------------------------------------- |
| **NS**                      | Yes                  | Low–medium | **High** if room noise hurts STT/VAD          | CPU; tune aggressiveness                                |
| **AGC**                     | Yes                  | Low        | Medium                                        | Can amplify noise; test carefully                       |
| **VADNet**                  | Yes                  | Medium     | Medium                                        | Dual path with energy VAD / EOU; don’t replace EOU need |
| **AEC**                     | Possible, harder     | **High**   | High **if** barge-in / TTS bleed is a problem | Need **playback reference** + sync USB mic vs ES8311    |
| **BSS / SE**                | Limited              | High       | Low–medium now                                | Needs **2+ raw mics** into AFE; we discard ch1–5 today  |
| **Extra beamforming on P4** | Low value            | —          | **Low**                                       | ReSpeaker ch0 already beamformed                        |
| **Adaptive EOU (Option A)** | Yes                  | Low        | **High** for pause cutoffs                    | Independent of AFE DSP                                  |




### 5.1 Why beamforming on P4 is mostly redundant

```text
ReSpeaker firmware: 4 mics → beamformed ASR → UAC ch0
NiNO: uses ch0 only
P4 AFE: cannot magically rebuild full 4-mic BF from mono
```

Do **not** plan “P4 beamforming” as a project. If you want on-device SE/BSS, you must **change** `usb_mic.c` **to pass 2 channels** (e.g. ch0+ch1 or two raw mics) into AFE — a larger change.

### 5.2 Why AEC is the hard one

AEC needs:

```text
mic PCM  ──┐
           ├──► AEC ──► cleaner near-end speech
ref PCM  ──┘   (what the speaker is playing, time-aligned)
```

NiNO today:

- Mic: USB host UAC (GPIO 24/25)
- Speaker: ES8311 I2S DAC
- Different clocks / paths → **reference delay calibration** required
- TTS / beep often runs while wake is paused; echo during **query capture** after chime is the main risk if speaker still audible or for future barge-in

Implement AEC only when echo is a measured problem (TTS leaking into next listen, or always-on duplex later).

---



## 6. Does “DSP first, then Option A” make sense?



### Nuance


| Problem                                      | Fixed by DSP (NS/AEC/…)? | Fixed by Option A (EOU)?             |
| -------------------------------------------- | ------------------------ | ------------------------------------ |
| Mid-sentence **thinking pauses** cut the WAV | **No**                   | **Yes**                              |
| Fan / AC noise confuses energy VAD           | **Yes (NS)**             | Partially (may wait longer on noise) |
| TTS echo into mic                            | **Yes (AEC)**            | No                                   |
| Quiet / loud level swings                    | **AGC** helps            | No                                   |
| Wake in noise                                | NS / better mic          | No                                   |


So:

- **Do not block Option A on “full AFE.”** Pause cutoffs happen on clean audio too.
- **Do optionally add light NS first** if serial/logs show noise false-speech or mushy STT — cleaner frames make both energy VAD and later EOU more stable.
- **Do not** turn on AEC + SE + VADNet + AGC all before Option A — high risk, high CPU, slow learning loop.



### Recommended product order

```text
Phase 0  Measure (noise? echo? pause cuts?)
    │
    ▼
Phase 2  Adaptive EOU (Option A)     ← DONE in voice_assist.c — ADVA_PLAN.md
    │
    ▼
Phase 1  Light DSP — NS on capture (and/or wake) only   ← next when noise hurts
    │
    ▼
Phase 3  AGC (optional), retune energy VAD
    │
    ▼
Phase 4  AEC (only if echo proven) + playback reference
    │
    ▼
Phase 5  Dual-mic SE / BSS (only if mono ch0 insufficient)
    │
    ▼
Phase 6  VADNet experiment (optional) + keep EOU layer
```

**Current decision:** ship **Option A first**; defer NS / AEC / SE / AGC until measured need.

---



## 7. Target architecture (after phased DSP)



### 7.1 Near-term (Phase 1–2) — recommended

```text
USB ch0 mono
      │
      ▼
┌─────────────┐
│ optional NS │  ← AFE or lightweight NS on capture buffer
└──────┬──────┘
       │
       ├──► WakeNet (AFE wake-only + optional NS)
       │
       └──► energy VAD → Adaptive EOU → WAV → WS → STT
```

Still **no AEC**, still **mono**, still **ReSpeaker BF**.

### 7.2 Later (Phase 4+) — if echo / multi-mic needed

```text
USB mic (1–2 ch) ──┐
                   ├──► AFE: AEC? + NS + (SE?) + (VADNet?)
ES8311 ref PCM ────┘              │
                                  ▼
                         clean PCM → WakeNet / EOU capture
```

---



## 8. Proper implementation flow (detailed)



### Phase 0 — Measure before coding


| Check            | How                                                            | Decision                       |
| ---------------- | -------------------------------------------------------------- | ------------------------------ |
| Pause cutoffs    | Speak with 0.8–1.5 s mid-gaps; inspect STT text                | If truncated → Option A needed |
| Stationary noise | Fan/AC on; compare STT WER / VAD false starts                  | If bad → Phase 1 NS            |
| Echo             | Play TTS loud; start listen; see if reply bleeds into next WAV | If yes → Phase 4 AEC           |
| Level            | Soft vs loud talkers                                           | If wild → Phase 3 AGC          |
| CPU headroom     | `idf.py monitor` / task stats with camera+wake                 | Cap how much AFE you enable    |


**Exit:** written notes — which phases are justified.

---



### Phase 1 — Noise suppression (first DSP win)

**Goal:** Cleaner mono for wake + capture without redesigning mic topology.

**Options:**


| Approach                                                          | Pros                                 | Cons                       |
| ----------------------------------------------------------------- | ------------------------------------ | -------------------------- |
| **1a** Enable `ns_init` on **wake AFE** only                      | Small change in `configure_wake_afe` | Capture path still raw     |
| **1b** Separate short-lived AFE (or NS) on **capture** after wake | Helps STT WAV quality                | More code; mic exclusivity |
| **1c** Both wake + capture NS                                     | Best quality                         | More CPU                   |


**Recommended start: 1a or 1b alone**, not both on day one.

**Steps (1a — wake NS):**

1. In `voice_wake.cpp` `configure_wake_afe()`: set `afe_cfg->ns_init = true` (keep AEC/SE/VAD/AGC false).
2. Confirm models / menuconfig include NS model if required by ESP-SR version.
3. Flash; test wake distance + false wakes with fan on.
4. Watch CPU: feed/fetch delays, AFE ring full warnings.
5. If good, consider 1b for capture.

**Steps (1b — capture NS sketch):**

1. After `mic_capture_hold`, feed frames through AFE fetch (NS-only config) **or** a minimal NS API if exposed.
2. Run energy VAD on **NS output** PCM, not raw USB.
3. Keep exclusive mic rules identical.
4. Do **not** enable AEC here yet.

**Files likely touched:** `voice_wake.cpp`, possibly `voice_assist.c`, `sdkconfig` / ESP-SR model selection.

**Test:**

- Wake: “Hi ESP” at 1 m / 3 m, quiet and with fan  
- Capture: same phrases; compare STT before/after  
- No new AFE ring-full during beep/VAD

**Exit:** NS on or off based on measured gain; document CPU cost.

---



### Phase 2 — Adaptive EOU (Option A)

**Goal:** Stop mid-sentence pause cutoffs.

Follow **[ADVA_PLAN.md](ADVA_PLAN.md)** exactly:

- Only `voice_assist.c` trailing-silence logic  
- Clamped adaptive timeout  
- No dependence on NS being perfect

**Why after (or parallel to) Phase 1:**

- Cleaner audio → fewer false “speech” blips during pauses → EOU timeouts more meaningful  
- But Option A still helps even without NS

**Exit:** T1–T8 from ADVA_PLAN pass.

---



### Phase 3 — AGC (optional)

**Goal:** Stabilize levels for soft speakers without clipping loud ones.

1. Enable `agc_init` on the path that already has NS (prefer capture or unified AFE).
2. Test quiet speech + loud speech + medical “yes.”
3. If noise floor pumps up during silence → disable or lower AGC; rely on STT instead.

**Exit:** keep only if STT improves on quiet talkers without hurting VAD.

---



### Phase 4 — AEC (only if echo is real)

**Goal:** Cancel speaker audio from mic so listen/TTS overlap doesn’t corrupt capture.

**Prerequisites:**

1. Capture a **reference** stream: PCM written to ES8311 (same samples, or a delayed copy).
2. Measure **delay** between ref and USB mic (buffer + USB + acoustic path).
3. Feed AFE with mic + ref per ESP-SR AEC API (channel layout per docs — often not simple `"M"`).
4. Keep LOW_COST / verify P4 CPU with camera on.

**Steps:**

1. Instrument playback path (`audio_playback` / `audio_queue`) to tee ref PCM into a ring.
2. Prototype AEC **only during capture** after chime (narrow scope).
3. Compare WAV spectrograms with TTS playing in room.
4. If barge-in / continuous duplex is a future product goal, widen AEC to always-on later.

**Do not start Phase 4** until Phase 0 shows echo problems.

**Exit:** measurable echo reduction; no xruns; wake still reliable.

---



### Phase 5 — Dual-mic SE / BSS (optional, larger)

**Goal:** Better separation than ReSpeaker ch0 alone in hard rooms.

**Requirements:**

1. Change `usb_mic.c` to expose **2 channels** (not only ch0 mono mix).
2. Reconfigure AFE input from `"M"` to dual-mic layout per ESP-SR.
3. Enable `se_init`; keep NS as needed.
4. Retune wake + capture consumers for stereo feed chunk sizes.

**Cost:** high touch area (USB mic, wake feed, capture, AFE config).  
**Only if** ch0 + NS still fails far-field / multi-speaker cases.

---



### Phase 6 — Neural VAD (optional)

**Goal:** Replace or assist energy VAD speech/noise classification.


| Approach                  | Notes                                              |
| ------------------------- | -------------------------------------------------- |
| AFE `vad_init` on capture | Speech flags from VADNet                           |
| Keep **EOU layer**        | Neural VAD ≠ end-of-utterance                      |
| Hybrid                    | VADNet for speech frame; Option A for stop timeout |


**Do not** expect VADNet alone to fix pause-y users — that remains EOU (Option A).

---



## 9. What not to do


| Anti-pattern                                  | Why                          |
| --------------------------------------------- | ---------------------------- |
| Enable AEC+NS+SE+VAD+AGC in one commit        | Undebuggable; CPU spike      |
| “Add P4 beamforming” on mono ch0              | No array geometry left       |
| Replace Option A with VADNet                  | Different problem            |
| AEC without playback reference                | Will not work                |
| Dual AFE consumers on mic without exclusivity | Breaks wake/VAD mutex design |
| Assume DSP chip offload                       | All cost is on P4 CPU        |


---



## 10. Suggested default roadmap for this repo


| Order | Work                          | Effort       | Depends on                              |
| ----- | ----------------------------- | ------------ | --------------------------------------- |
| 0     | Measure pause / noise / echo  | Hours        | —                                       |
| **2** | **Option A Adaptive EOU**     | Small        | **Done** — [ADVA_PLAN.md](ADVA_PLAN.md) |
| 1     | **NS** on wake and/or capture | Small–medium | Next when noise hurts                   |
| 3     | AGC optional                  | Small        | Phase 1 stable                          |
| 4     | AEC if echo proven            | Large        | Ref path + delay                        |
| 5     | Dual-mic SE                   | Large        | USB 2-ch + AFE layout                   |
| 6     | VADNet hybrid                 | Medium       | EOU already done                        |


**Shipped first:** Option A (`voice_assist.c`). **Next when needed:** NS, then AEC / SE / AGC.

---



## 11. File / module impact map


| Module                     | Phase 1 NS | Phase 2 EOU | Phase 4 AEC       | Phase 5 SE     |
| -------------------------- | ---------- | ----------- | ----------------- | -------------- |
| `voice_wake.cpp`           | Yes        | No          | Maybe             | Yes            |
| `voice_assist.c`           | Maybe (1b) | **Yes**     | Maybe             | Yes            |
| `usb_mic.c`                | No         | No          | No                | **Yes** (2 ch) |
| `audio_playback.c` / queue | No         | No          | **Yes** (ref tee) | No             |
| Server / WS                | No         | No          | No                | No             |
| ReSpeaker hardware         | No         | No          | No                | No             |


---



## 12. CPU / RAM guardrails

Before enabling each block:

1. Note idle + wake + camera CPU baseline.
2. Enable one flag.
3. Reject if: AFE ring full returns, wake latency jumps, USB mic drops increase, face-track stutters.
4. Prefer `AFE_MODE_LOW_COST` until proven otherwise.

P4 is capable; NiNO is already a busy board — **budget DSP like a feature, not a free lunch.**

---



## 13. Relation to ADVA docs


| Doc                                                | Role                                                |
| -------------------------------------------------- | --------------------------------------------------- |
| **This file**                                      | Mic / AFE DSP features on P4 — what, whether, order |
| [ADVA.md](ADVA.md)                                 | Adaptive EOU decision (Option A vs B/C/D)           |
| [ADVA_PLAN.md](ADVA_PLAN.md)                       | How to implement Option A in `voice_assist.c`       |
| [USB-4MIC-INTEGRATION.md](USB-4MIC-INTEGRATION.md) | Current USB mic + wake/VAD flow                     |


Together:

```text
ReSpeaker BF (already) + optional NS/AEC (this doc)
        →
energy / optional VADNet speech frames
        →
Adaptive EOU (ADVA) → WAV → STT
```

---



## 14. Summary

1. **P4 can run AEC, NS, SE, VADNet, AGC, WakeNet** as **software AFE**, not a separate DSP ASIC.
2. **NiNO today:** WakeNet only; USB **ch0** already beamformed; energy VAD capture.
3. **Implementable now with good ROI:** **NS** (and later optional AGC).
4. **Hard / later:** **AEC** (needs speaker reference), **SE** (needs 2-ch USB).
5. **Skip as a goal:** extra P4 beamforming on mono.
6. **Order:** measure → light NS → **Option A EOU** → optional AGC → AEC/SE/VADNet only with proof.
7. **Option A does not require full DSP** — but light NS before/with it can make EOU more reliable in noisy rooms.

When ready to code: start with **Phase 0 notes**, then either **Phase 1 NS** or **Phase 2 Option A** depending on which pain dominates.