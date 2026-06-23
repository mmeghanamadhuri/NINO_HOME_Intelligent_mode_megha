# Mobile App Communication Flow (mDNS -> WebSocket Status)

Last updated: 2026-06-23

## Goal

Define a simple two-way discovery + status flow between mobile app and firmware.
Status is now consumed over HTTP and WebSocket (WS), with HTTP `/status` as the simplest primary check.

1. App starts
2. mDNS finds bot
3. User taps bot
4. App calls `HTTP GET /status`
5. (Optional live path) App opens `WS /ws/status` and sends `status`
6. Bot returns current information as JSON

---

## Target App Flow

```text
App starts
   |
   v
Browse mDNS service _nino._tcp
   |
   v
Show discovered bots (name + host + ip)
   |
   v
User taps one bot
   |
   v
GET http://<selected-host-or-ip>/status
   |
   v
Render current device state in app UI
   |
   v
(Optional) Open WS ws://<selected-host-or-ip>/ws/status
   |
   v
Send text frame: "status"
   |
   v
Render current device state in app UI
```

---

## mDNS Discovery Contract (already in firmware)

- Hostname: `espressif.local` (mDNS label: `espressif`)
- Service: `_nino._tcp`
- Port: `80`
- TXT includes:
  - `device=nino`
  - `ble_name=NINO - HOME`
  - `transport=http`

App can use:

- Service browse: `_nino._tcp.local`
- Endpoint base URL: `http://espressif.lan/` (when LAN DNS resolves it) or `http://espressif.local/` via mDNS

---

## Status Payload Contract

Mobile app wants:

```json
{
  "device_name": "ESP Assistant",
  "wifi_ssid": "MyHomeWifi",
  "volume": 75,
  "firmware": "1.0.0"
}
```

Recommended final payload (with `ok` + optional fields):

```json
{
  "ok": true,
  "device_name": "ESP Assistant",
  "wifi_ssid": "MyHomeWifi",
  "volume": 75,
  "firmware": "1.0.0",
  "mdns_host": "espressif.local",
  "sta_connected": true,
  "ip": "192.168.1.42"
}
```

---

## Firmware Implementation (done)

Implemented endpoints in firmware:

- `GET /status` (HTTP snapshot for app)
- `GET /ws/status` (WS request/response frames, when `CONFIG_HTTPD_WS_SUPPORT` is enabled)

WebSocket usage:

- connect to `ws://<host>/ws/status`
- send text: `status` (or `get_status`, `ping`)
- firmware replies with the same status JSON contract

Returned fields are sourced from current runtime:

- `device_name`: fixed value (`ESP Assistant`)
- `wifi_ssid`: current STA SSID (`s_sta_ssid`)
- `volume`: `nino_audio_get_volume_percent()`
- `firmware`: build version (`PROJECT_VER`)
- plus `sta_connected`, `ip`, `mdns_host`

## Response behavior

- If STA is disconnected, status still returns with:
  - `sta_connected: false`
  - `ip: "0.0.0.0"`
- Unknown WS command returns:
  - `{"ok":false,"error":"send 'status'"}`
- Existing `/api/wifi/status` and `/api/wifi/config` remain unchanged.

---

## Suggested app strategy

- Use `HTTP GET /status` for first load and simple status refresh.
- Use `WS /ws/status` when you want request/response over a persistent channel.
- Keep this as local-development flow for now.

---

## Minimal Test Plan

1. Boot board and connect to Wi-Fi.
2. Verify mDNS discoverability (`_nino._tcp`).
3. Verify HTTP snapshot:
   - `GET http://espressif.lan/status`
4. Verify WebSocket path:
   - connect `ws://espressif.lan/ws/status`
   - send `status`
   - confirm JSON reply
5. Confirm JSON has:
   - `device_name`
   - `wifi_ssid`
   - `volume`
   - `firmware`
6. Disconnect/reconnect Wi-Fi and verify fields update correctly.

---

## Next Increment (after `/status`)

- Add `POST /volume` (or reuse `/speaker/volume`) from app.
- Add simple `POST /device/name` if app should rename bot.
- Add heartbeat timestamp in `/status` for online freshness.

## HTTPS re-enable checklist (later)

- Move app calls back to `https://` and `wss://`.
- Ensure certificate SAN includes app hostname.
- Ensure Android trust chain is configured.
- Validate:
  - `https://espressif.lan/status`
  - `wss://espressif.lan/ws/status`
