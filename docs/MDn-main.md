# mDNS Main Integration Status (BLE -> Wi-Fi -> mDNS)

Last checked: 2026-06-23

## Goal

Keep mDNS status in sync with firmware progress so client integration and validation can proceed without ambiguity.

---

## What is already there (implemented)

- BLE Wi-Fi provisioning flow is implemented (`main/wifi_prov_ble.c`) and started from `app_main`.
- Wi-Fi events are already wired in `main/main.c`:
  - `IP_EVENT_STA_GOT_IP`
  - `WIFI_EVENT_STA_DISCONNECTED`
- HTTP server is already running on port `80` with existing endpoints.
- HTTPS server now serves status endpoint on port `443`.
- Device already has a custom network discovery path:
  - UDP multicast listener/responding on `239.255.255.250:1900` (`discover` -> `hi...`)
  - TCP message server on port `8888`
- Wi-Fi credentials persistence and reconnect flow are already present via NVS (`wifi_cfg`).

---

## Newly implemented (this pass)

- Added mDNS dependency in `main/CMakeLists.txt` (`mdns` in `main_requires`).
- Added mDNS lifecycle in `main/main.c`:
  - `mdns_start_service()` on `IP_EVENT_STA_GOT_IP`
  - `mdns_stop_service()` on `WIFI_EVENT_STA_DISCONNECTED`
- Added hostname + instance name as requested:
  - `NINO-HOME.local`
  - instance `NINO-HOME`
- Added service advertisement:
  - service type `_nino._tcp`
  - port `443`
- Added TXT records:
  - `device=nino`
  - `ble_name=NINO - HOME`
  - `transport=https`

---

## mDNS implementation checklist (current)

1. Add mDNS component dependency
   - Status: done

2. Add a small mDNS module
   - Recommended files:
     - `main/mdns_service.h`
     - `main/mdns_service.c`
   - Keep mDNS logic isolated from camera/voice logic in `main.c`.

3. Define naming strategy
   - Current status: fixed name `NINO-HOME` (as requested).
   - Later option: switch to unique hostname suffix for multi-device LAN.

4. Register service
   - Current status: done (`_nino._tcp`, port `443`).
   - If needed, register additional services later (`_http._tcp`, `_ws._tcp`, etc.).

5. Add TXT records
   - Current status: baseline TXT is present.
   - Optional additions:
     - `fw=<version>`
     - `features=camera,audio,touch,servo`

6. Wire lifecycle to Wi-Fi events
   - Current status: done with stop/start on disconnect/reconnect.
   - Duplicate init is guarded (`s_mdns_started`).

7. Add startup/health logs
   - Current status: done (`init`, `ready`, `stopped`, and failure logs).

8. Validate on LAN
   - From another machine on same Wi-Fi:
    - `ping NINO-HOME.local`
     - service browse for `_nino._tcp`
    - open `https://NINO-HOME.local/status`

---

## Suggested next validation
  
- Build and flash firmware once with fresh config.
- Verify serial log contains mDNS ready line for `NINO-HOME.local`.
- From another LAN device:
  - `ping NINO-HOME.local`
  - browse `_nino._tcp`
  - open `https://NINO-HOME.local/status`
- Decide whether to keep fixed hostname or move to unique hostname for multi-bot networks.

### Completed build milestone (today)

- Added `espressif/mdns` to `main/idf_component.yml` to resolve:
  - `Failed to resolve component 'mdns' required by component 'main'`
- Ran clean rebuild (`idf.py fullclean` then `idf.py build`) successfully.

---

## Notes for current architecture

- Existing UDP multicast discovery can remain as backward compatibility.
- mDNS should become the primary discovery mechanism for new app/client flow.
- BLE provisioning already gives network access; mDNS should begin only after successful STA IP acquisition.
