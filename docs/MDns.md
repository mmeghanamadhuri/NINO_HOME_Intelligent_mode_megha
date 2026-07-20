# ESP32-P4 mDNS Discovery (Current Firmware State)

Last checked: 2026-07-17

## Purpose

After BLE/HTTP Wi-Fi provisioning succeeds and STA gets an IP, firmware advertises the robot over mDNS so the PC server (and mobile apps) can discover it without hardcoded IPs — including **multiple robots on the same LAN**.

---

## Implemented in Firmware

mDNS integration is active in `main/main.c` and dependency is added in:

- `main/CMakeLists.txt`
- `main/idf_component.yml` (`espressif/mdns`)

### Runtime behavior

- On `IP_EVENT_STA_GOT_IP`:
  - `mdns_start_service()` runs
  - hostname + instance + service + TXT are registered
- On `WIFI_EVENT_STA_DISCONNECTED`:
  - `mdns_stop_service()` runs (`mdns_free()`)
- On `device id <new>` CLI change while online: mDNS restarts so hostname/TXT update

This keeps mDNS aligned with real STA connectivity and the stable `device_id`.

---

## Active mDNS Configuration

| Field | Value |
|-------|--------|
| Hostname | **`device_id`** (DNS-safe, e.g. `nino-a1b2c3`) — unique per robot |
| Resolved host | `<device_id>.local` |
| Instance name | Friendly `device_name` (e.g. `NINO - HOME`) |
| Service type | `_nino._tcp` |
| Port | `80` (HTTP status / stream / play_wav) |

### TXT records advertised

| Key | Example | Purpose |
|-----|---------|---------|
| `device` | `nino` | Filter NiNO services |
| `device_id` | `nino-a1b2c3` | Stable id for PC `devices.json` upsert |
| `ble_name` | `NINO - HOME` | Display / BLE name |
| `transport` | `http` | How to talk to the bot |
| `path` | `/status` | Confirm endpoint |

---

## Expected Serial Logs

When station gets IP and mDNS starts:

```text
STA: Got IP ...
mDNS ready: nino-a1b2c3.local (device_id=nino-a1b2c3 name=NINO - HOME) service _nino._tcp port 80
```

On Wi-Fi disconnect:

```text
STA: Disconnected (reason ...)
mDNS stopped
```

---

## UDP discovery (fallback)

Same firmware also answers UDP `"discover"` on `239.255.255.250:1900`.

Reply includes:

```text
hi
mac=AA:BB:CC:DD:EE:FF
name=NINO - HOME
device_id=nino-a1b2c3
192.168.1.42:8888
```

Prefer mDNS; use UDP when mDNS is blocked on the LAN.

---

## Confirm with HTTP

```text
GET http://<ip-or-device_id.local>/status
```

JSON includes `device_id`, `device_name`, `ip`, `mdns_host`.

---

## LAN Validation Checklist

1. Build and flash:
   - `idf.py build`
   - `idf.py -p <PORT> flash monitor`
2. Provision Wi-Fi and wait for STA IP.
3. From another device on same LAN:
   - `ping <device_id>.local`
   - browse `_nino._tcp` — confirm TXT has `device_id`
   - open `http://<ip>/status`
4. With two boards: confirm **different** hostnames (`nino-xxxxxx.local`) and different `device_id` TXT values.
5. Disconnect/reconnect AP and confirm stop/start logs.

---

## Server discovery (next)

PC server should:

1. Browse `_nino._tcp`
2. Read TXT `device_id` (or `GET /status`)
3. Upsert `devices.json` with `base_url` / camera / play_wav URLs
4. Fall back to UDP `"discover"` if mDNS finds nothing

See `docs/multires.md` for the multi-robot registry model.
