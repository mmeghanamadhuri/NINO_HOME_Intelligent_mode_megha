# ESP32-P4 mDNS Discovery (Current Firmware State)

Last checked: 2026-06-23

## Purpose

After BLE/HTTP Wi-Fi provisioning succeeds and STA gets an IP, firmware advertises the robot over mDNS so clients can discover it without hardcoded IP.

---

## Implemented in Firmware

mDNS integration is now active in `main/main.c` and dependency is added in:

- `main/CMakeLists.txt`
- `main/idf_component.yml` (`espressif/mdns`)

### Runtime behavior

- On `IP_EVENT_STA_GOT_IP`:
  - `mdns_start_service()` runs
  - hostname + instance + service + TXT are registered
- On `WIFI_EVENT_STA_DISCONNECTED`:
  - `mdns_stop_service()` runs (`mdns_free()`)

This keeps mDNS aligned with real STA connectivity.

---

## Active mDNS Configuration

- Hostname: `NINO-HOME`
- Resolved host: `NINO-HOME.local`
- Instance name: `NINO-HOME`
- Service name: `NiNO Robot`
- Service type: `_nino._tcp`
- Port: `443`

### TXT records currently advertised

- `device=nino`
- `ble_name=NINO - HOME`
- `transport=https`

---

## Expected Serial Logs

When station gets IP and mDNS starts:

```text
STA: Got IP ...
mDNS ready: NINO-HOME.local service _nino._tcp port 443
```

On Wi-Fi disconnect:

```text
STA: Disconnected (reason ...)
mDNS stopped
```

If component/header is missing in current build environment:

```text
mDNS headers not available in current build environment
```

---

## LAN Validation Checklist

1. Build and flash:
   - `idf.py build`
   - `idf.py -p <PORT> flash monitor`
2. Provision Wi-Fi over BLE (or existing fallback flow) and wait for STA IP.
3. From another device on same LAN:
   - `ping NINO-HOME.local`
   - browse `_nino._tcp`
   - open `https://NINO-HOME.local/status`
4. Disconnect/reconnect AP and confirm stop/start logs are printed.

---

## Relationship to Existing UDP Discovery

Existing custom UDP discovery (`discover` on `239.255.255.250:1900`) remains in firmware for backward compatibility.  
mDNS is now the preferred discovery path for new client integrations.

---

## Pending / Optional Next Improvements

- Decide whether fixed hostname `NINO-HOME` is final.
  - For multi-bot LAN use, switch to unique hostnames (for example MAC suffix).
- Expand TXT metadata for client filtering:
  - example: firmware version, feature flags, hardware profile.
- Update client app/server discovery logic to prefer mDNS first, UDP fallback second.
- Add a small runtime command (`mdns status`) for quick field debugging (optional).
