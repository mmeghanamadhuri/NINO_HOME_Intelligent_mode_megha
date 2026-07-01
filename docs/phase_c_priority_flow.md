# Phase C — Priority Summary Flow (Plan)

How daily summaries **should** work: topic threads go into `summaries`; personal facts go to Phase B `memories`; noise and meta-queries are excluded.

This reflects the **target design** discussed for NiNO. Some steps (priority pre-filter before summarization) are **not fully implemented yet** — see [Current vs planned](#current-vs-planned) at the end.

---

## 1) Big picture — three memory lanes

```mermaid
flowchart TB
    subgraph Input["User speaks (recognized face)"]
        V[Voice turn]
    end

    subgraph Log["Phase A — log turn"]
        C[(conversations table)]
    end

    subgraph Split["Classify each turn"]
        P{Turn type?}
    end

    subgraph LaneB["Phase B — personal facts"]
        M[(memories table)]
    end

    subgraph LaneC["Phase C — topic threads"]
        S[(summaries table)]
    end

    subgraph Skip["Excluded from summary"]
        X[Drop: recap, recall, alarms, prefs, noise]
    end

    V --> C
    C --> P
    P -->|Preference / birthday / like-dislike| M
    P -->|Substantive discussion / explain / learn| LaneC
    P -->|Recap / recall / alarm / echo / joke| X

    LaneC -->|Nightly or startup catch-up| S
```

| Lane | Table | What belongs here |
|------|-------|-------------------|
| **Phase A** | `conversations` | Every logged turn (raw history) |
| **Phase B** | `memories` | Personal facts: prefs, dates, stable details |
| **Phase C** | `summaries` | **Topic themes** from the day (compressed) |
| **Excluded** | — | Meta-recap, recall questions, alarms, duplicates of Phase B |

---

## 2) Day X — conversation logging flow

Each voice turn is stored in `conversations` unless already skipped at log time.

```mermaid
flowchart TD
    A[ESP WAV → STT → user_text] --> B{Recognized face?}
    B -->|No| Z1[Generic LLM reply — often not logged]
    B -->|Yes| C{Skip logging?}

    C -->|Volume / alarm / servo / fragment| Z2[skipped_* — not in conversations]
    C -->|Recap question| Z3[skipped_recap]
    C -->|Memory recall e.g. what is my favorite food| Z4[skipped_recall]
    C -->|Alarm command| Z5[skipped_alarm]
    C -->|Normal chat| D[INSERT conversations]

    D --> E{Phase B enabled?}
    E -->|Yes| F[Background: extract personal facts]
    F --> G{Personal fact?}
    G -->|Yes e.g. coffee over tea| H[(memories)]
    G -->|No e.g. explain Gandhi| I[No memory row]

    D --> J[Turn waits for Phase C rollup]
```

**Examples from a busy day**

| User said | Logged to `conversations`? | Phase B `memories`? | Phase C summary later? |
|-----------|----------------------------|---------------------|-------------------------|
| Explain microcontroller simply | Yes | No | **Yes** — topic |
| I prefer coffee over tea | Yes | **Yes** | **No** — personal |
| Tell me five CEOs of India | Yes | No | **Yes** — topic |
| Few minutes back we discussed speakers | Often **No** (recap skip) | No | **No** — meta |
| What is demonetization? | Yes | No | **Yes** — topic (merge with follow-ups) |
| Set alarm for 7 AM | **No** (alarm path) | No | **No** |

---

## 3) End of day X → summary generation (Phase C write path)

Triggered when `MEMORY_SUMMARY_CRON=1` and server starts on day X+1 (startup catch-up for yesterday).

```mermaid
flowchart TD
    START[Server startup — memory ready] --> FLAG{MEMORY_SUMMARY_CRON=1?}
    FLAG -->|No| END1[No summary job]
    FLAG -->|Yes| T[target_date = yesterday]

    T --> U[For each user_id with conversations on target_date]
    U --> EXISTS{Summary row already exists?}
    EXISTS -->|Yes| U
    EXISTS -->|No| LOAD[Load all conversations for user + date]

    LOAD --> FILTER[**Priority filter each turn**]
    FILTER --> DROP[Drop low-priority turns]
    FILTER --> KEEP[Keep substantive turns]

    DROP --> D1[Preferences coffee/tea/chess]
    DROP --> D2[Recap / yesterday meta questions]
    DROP --> D3[Memory recall questions]
    DROP --> D4[Alarms / reminders]
    DROP --> D5[Jokes / TTS echo / fragments]

    KEEP --> K1[Explain / teach topics]
    KEEP --> K2[Follow-ups on same theme]
    KEEP --> K3[Comparisons e.g. Java vs JS]

    K1 & K2 & K3 --> TRANS[Build filtered transcript]
    TRANS --> EMPTY{Any turns left?}
    EMPTY -->|No| U
    EMPTY -->|Yes| LLM[Ollama: group by topic → 4–6 bullets]

    LLM --> INSERT[INSERT summaries one row per user per day]
    INSERT --> U
```

---

## 4) Priority filter — decision tree (per turn)

This is the core “trick”: **not every `conversations` row should influence the summary.**

```mermaid
flowchart TD
    T[One conversation turn user_text] --> R1{Recap or meta?<br/>few minutes back / yesterday we discussed}
    R1 -->|Yes| OUT[❌ Exclude from summary]
    R1 -->|No| R2{Memory recall?<br/>what is my favorite / birthday}
    R2 -->|Yes| OUT
    R2 -->|No| R3{Preference statement?<br/>I prefer coffee / I love chess}
    R3 -->|Yes| OUT2[❌ Exclude — already Phase B]
    R3 -->|No| R4{Alarm / reminder?}
    R4 -->|Yes| OUT
    R4 -->|No| R5{Joke / echo / fragment?}
    R5 -->|Yes| OUT
    R5 -->|No| R6{Substantive topic?<br/>explain / tell me about / difference between}
    R6 -->|Yes| IN[✅ Include in summary input]
    R6 -->|No| R7{Short greeting / noise?}
    R7 -->|Yes| OUT
    R7 -->|No| IN
```

---

## 5) Topic merging — many turns → few bullets

Multiple turns about the **same theme** collapse into **one** summary line.

```mermaid
flowchart LR
    subgraph Raw["Same day — conversations"]
        A1[Plan about speakers properly]
        A2[How does a speaker work?]
        A3[Components used in manufacturing?]
        A4[Discussion on speakers yesterday — explain again]
    end

    subgraph Filter["After priority filter"]
        B1[Plan about speakers]
        B2[How speaker works]
        B3[Manufacturing components]
        B4[❌ dropped — recap meta]
    end

    subgraph Summary["One bullet in summaries"]
        C["Speakers — operation, parts, manufacturing"]
    end

    A1 --> B1
    A2 --> B2
    A3 --> B3
    A4 --> B4
    B1 & B2 & B3 --> C
```

**Example day rollup (ideal output)**

```text
- Microcontrollers (simple explanation)
- CEOs of India
- Java vs JavaScript
- Mahatma Gandhi
- Demonetization
- Trigonometry
- Speakers (how they work, components, manufacturing)
```

**Not in summary:** coffee/tea, chess vs outdoor games, recap phrasing.

---

## 6) Day X+1 — read path (when user returns)

```mermaid
sequenceDiagram
    participant Cam as Camera
    participant VS as voice_service
    participant MS as memory_service
    participant PG as PostgreSQL
    participant LLM as Ollama

    Note over Cam,LLM: Face visible — greeting includes summary when available
    Cam->>VS: Face recognized Chakri
    VS->>MS: get_latest_summary_text(Chakri)
    MS->>PG: SELECT latest summary
    PG-->>MS: yesterday topic bullets
    VS->>LLM: greeting_for_face(name + summary)
    LLM-->>VS: "Hi Chakri — yesterday we talked about speakers…"

    Note over Cam,LLM: User speaks — summary + memories + recent turns
    Cam->>VS: WAV query
    VS->>MS: load_context(Chakri)
    MS->>PG: recent conversations (Phase A)
    MS->>PG: top memories (Phase B)
    MS->>PG: latest summary (Phase C)
    PG-->>MS: prefs + yesterday topic bullets
    MS->>MS: _format_prompt_block()
    MS-->>VS: prompt_block
    VS->>LLM: answer with Earlier session summary + Known facts
    LLM-->>VS: reply using yesterday themes
```

**Planned enhancement (not wired yet):** pass `summary_text` into `greeting_for_face()` so the first spoken line can say *“Welcome back — yesterday we talked about speakers and demonetization.”*

---

## 7) Full lifecycle — one diagram

```mermaid
flowchart TB
    subgraph DayX["Day X — during use"]
        V1[Voice turns] --> LOG[(conversations)]
        LOG --> BEXT[Phase B async extract]
        BEXT --> MEM[(memories<br/>coffee, chess, birthday)]
    end

    subgraph Night["Day X+1 — server startup"]
        CRON{MEMORY_SUMMARY_CRON=1}
        CRON -->|Yes| PICK[Pick yesterday's turns per user]
        PICK --> FILT[Priority filter]
        FILT --> TOP[Keep topic threads only]
        TOP --> SUMLLM[Ollama → merge topics]
        SUMLLM --> SUM[(summaries<br/>Gandhi, speakers, demonetization…)]
    end

    subgraph DayXp1["Day X+1 — user back"]
        GREET[Face greeting + summary] --> SUM
        ASK[User asks question] --> LOAD[load_context]
        LOAD --> MEM
        LOAD --> SUM
        LOAD --> LOG
        LOAD --> REPLY[LLM reply with full context]
    end

    LOG --> PICK
```

---

## 8) Responsibility matrix

| Content | `conversations` | `memories` (B) | `summaries` (C) |
|---------|-----------------|----------------|-----------------|
| Explain microcontroller | ✓ raw turn | — | ✓ topic bullet |
| I prefer coffee over tea | ✓ raw turn | ✓ fact | ✗ |
| CEOs of India + follow-up | ✓ raw turns | — | ✓ one merged bullet |
| Demonetization × 2 turns | ✓ raw turns | — | ✓ one merged bullet |
| Speakers × 3 explain turns | ✓ raw turns | — | ✓ one merged bullet |
| Recap: few minutes back speakers | ✗ or ✓ but filter out | — | ✗ |
| What is my favorite food | ✗ skipped_recall | — | ✗ |
| Set alarm 7 AM | ✗ skipped_alarm | — | ✗ |

---

## 9) Implementation checklist (to match this flow)

| Step | Status | Module |
|------|--------|--------|
| Log turns to `conversations` | ✅ Done | `memory_service`, `voice_service` |
| Skip recap / recall / alarms at log time | ✅ Done | `memory_filters.conversation_log_skip_reason` |
| Extract prefs to `memories` | ✅ Done | Phase B in `memory_service` |
| Startup catch-up for yesterday | ✅ Done | `_run_summary_catchup_safe` |
| **Priority filter before summarize** | ⬜ Planned | `_summarize_user_day` + `memory_filters` |
| **Topic-merge prompt** (group speakers, Gandhi, etc.) | ⬜ Planned | summarization prompt in `memory_service` |
| Inject summary into voice LLM | ✅ Done | `load_context` → `_format_prompt_block` |
| Inject summary into face greeting | ✅ Done | `greeting_for_face` + `tts_service` |

---

## Current vs planned

| Aspect | Today | This plan |
|--------|-------|-----------|
| Input to summarizer | **All** conversation rows for the day | **Filtered** topic rows only |
| Personal prefs in summary | May appear if LLM includes them | **Excluded** — live in `memories` |
| Recap / meta questions | May appear if logged | **Excluded** |
| Multiple turns same topic | LLM may or may not merge | **Explicit merge** into one bullet |
| Greeting uses summary | No | Yes (planned) |

See also: [`phase_c_detailed.md`](phase_c_detailed.md) for full Phase C reference.
