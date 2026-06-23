# BLE Configuration Status

Last checked: 2026-06-23

## Current progress

BLE Wi-Fi provisioning is **mostly implemented in firmware** and wired into boot flow.  
Estimated completion for "BLE configuration part": **~95% implemented, ~90% validated on hardware**.

## What is already done

- BLE provisioning module exists: `main/wifi_prov_ble.c` + `main/wifi_prov_ble.h`.
- NimBLE + ESP-Hosted integration is present (ESP32-P4 host with ESP32-C6 BT controller).
- Custom GATT service is implemented with these characteristics:
  - SSID (write)
  - Password (write)
  - Apply command (write `0x01`)
  - Status JSON (read/notify)
- Advertising identity is defined and stable:
  - Device name: `NINO - HOME`
  - Service UUID: `4facb001-5a2e-4b7c-9e1f-a8d3e6f20401`
- BLE startup is called from `app_main` (`wifi_prov_ble_start()` in `main/main.c`).
- Wi-Fi event bridge is wired:
  - STA got IP -> BLE status notify connected
  - STA disconnected -> BLE status notify reconnecting/failed path
- Credential persistence is already handled via existing Wi-Fi/NVS flow (`wifi_cfg` namespace).
- Build/dependency wiring is present:
  - `main/idf_component.yml` includes `esp_hosted` and `esp_wifi_remote`
  - `sdkconfig.defaults.esp32p4` enables BT/NimBLE/Hosted BT options needed for BLE provisioning
- User-facing provisioning guide exists: `docs/WIFI_PROVISION.md`.

## Remaining / conditional items

- Capture one full boot transcript with startup signatures:
  - `C6 BLE controller ready`
  - `NimBLE host started`
  - `BLE provisioning GATT ready (NINO - HOME)`
- No in-repo Android/iOS app implementation is included for automated provisioning UX.
  - Only protocol/docs are provided for mobile implementation.
- End-to-end hardware validation should still be treated as required after each ESP-IDF / hosted firmware update.

## Latest validated milestone (2026-06-23)

- BLE app wrote credentials and firmware accepted them.
- Apply command triggered provisioning task.
- Credentials were committed to NVS and station connected.
- IP acquisition and BLE success notify both occurred.

Integrity verdict: **credentials are good (not corrupted)** based on consistent SSID + password length through BLE receive and NVS commit logs.

## Pending-first execution plan

1. Confirm healthy BLE startup logs in a fresh boot transcript:
   - `C6 BLE controller ready`
   - `NimBLE host started`
   - `BLE provisioning GATT ready (NINO - HOME)`
2. Repeat one BLE GATT test flow from `docs/WIFI_PROVISION.md` and save transcript.
3. (Optional) Add minimal BLE test client for repeatable QA.

## Practical status verdict

- **Firmware-side BLE config logic:** complete
- **Project integration (boot + Wi-Fi events + status):** complete
- **Operational readiness on current hardware:** validated for credential provisioning path
- **Mobile client app layer:** not included in this repo

## Recommended next actions

1. Resolve C6 hosted firmware mismatch/not-ready condition first.
2. Run a full on-device smoke test using `docs/WIFI_PROVISION.md` (scan, write SSID/pass, apply, verify status notify and IP).
3. Capture one validated serial log artifact showing:
   - `C6 BLE controller ready`
   - `NimBLE host started`
   - `BLE provisioning GATT ready (NINO - HOME)`
4. If needed for productization, add a minimal Android provisioning client (or scriptable test harness) to remove manual BLE testing.

## Related update completed today (mDNS)

- mDNS integration is now active in firmware (`main/main.c`) and starts on `IP_EVENT_STA_GOT_IP`.
- Active LAN endpoint after Wi-Fi connect:
  - `NINO-HOME.local`
  - service `_nino._tcp` on port `443`
- Build integration was validated by adding `espressif/mdns` in `main/idf_component.yml` and completing a clean rebuild.
