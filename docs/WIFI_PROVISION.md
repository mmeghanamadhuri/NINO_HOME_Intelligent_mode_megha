# Wi‑Fi provisioning — BLE GATT (primary) and HTTP (fallback)

The ESP32-P4 advertises a BLE GATT service for Wi‑Fi setup. Credentials are saved to NVS and the board reconnects on every boot. A soft AP and HTTP API remain available as a fallback.

## BLE discovery

| Field | Value |
|-------|--------|
| Advertised name | `PROV_NINO` |
| Service UUID | `4facb001-5a2e-4b7c-9e1f-a8d3e6f20401` |
| Soft AP (optional) | `ESP32_P4_CAM` / `12345678` |

Scan for **PROV_NINO** or filter by the service UUID above.

## GATT layout

All characteristics belong to service `4facb001-5a2e-4b7c-9e1f-a8d3e6f20401`.

| Characteristic UUID | Properties | Max length | Description |
|---------------------|------------|------------|-------------|
| `...0201` `4facb001-5a2e-4b7c-9e1f-a8d3e6f20402` | Write | 32 | Home Wi‑Fi SSID (UTF-8) |
| `...0301` `4facb001-5a2e-4b7c-9e1f-a8d3e6f20403` | Write | 64 | Home Wi‑Fi password (UTF-8, may be empty) |
| `...0401` `4facb001-5a2e-4b7c-9e1f-a8d3e6f20404` | Write | 1 | Command: `0x01` = apply and connect |
| `...0501` `4facb001-5a2e-4b7c-9e1f-a8d3e6f20405` | Read, Notify | 96 | JSON status (see below) |

Full UUIDs:

- SSID: `4facb001-5a2e-4b7c-9e1f-a8d3e6f20402`
- Password: `4facb001-5a2e-4b7c-9e1f-a8d3e6f20403`
- Command: `4facb001-5a2e-4b7c-9e1f-a8d3e6f20404`
- Status: `4facb001-5a2e-4b7c-9e1f-a8d3e6f20405`

### Android provisioning sequence (GATT)

1. Scan and connect to `PROV_NINO` (no pairing required).
2. Discover service `4facb001-5a2e-4b7c-9e1f-a8d3e6f20401`.
3. Enable notifications on the status characteristic (`...0501`).
4. Write SSID to `...0201`.
5. Write password to `...0301` (zero-length write for open networks).
6. Write `0x01` to command characteristic `...0401`.
7. Wait for status notify/read:
   - `state` **1** = connecting
   - `state` **2** = connected (`connected` true, `ip` set)
   - `state` **3** = failed
8. Move the phone to the same home Wi‑Fi; the robot is reachable at `http://<ip>/`.

Example status JSON:

```json
{"state":2,"connected":true,"ip":"192.168.1.42"}
```

`state` values: `0` idle, `1` connecting, `2` connected, `3` failed.

### Kotlin / Android BLE hints

- Use `BluetoothLeScanner` with optional `ScanFilter` on service UUID `4facb001-5a2e-4b7c-9e1f-a8d3e6f20401`.
- After `connectGatt`, run operations on a background thread; use `WRITE_TYPE_DEFAULT` for SSID/password/command.
- Subscribe to CCCD on status char before writing command.
- No bonding/pairing is required (`sm_bonding` disabled on device).

## NVS persistence

Namespace `wifi_cfg`:

| Key | Content |
|-----|---------|
| `mode` | `2` = STA after BLE/HTTP provision |
| `sta_ssid` | Router SSID |
| `sta_pass` | Router password |

On reboot the firmware loads these keys and calls `esp_wifi_connect()` automatically.

## HTTP fallback (soft AP)

If BLE is unavailable, join `ESP32_P4_CAM` and use:

- `POST http://192.168.4.1/api/wifi/config` — body `{"ssid":"...","password":"..."}`
- `GET http://192.168.4.1/api/wifi/status` — poll until `sta_connected` is true

## Build requirements (ESP32-P4)

`sdkconfig.defaults.esp32p4` enables NimBLE host + ESP-Hosted VHCI to the on-board ESP32-C6.

**After pulling BLE changes you must regenerate sdkconfig**, or the build fails looking for `host/ble_gap.h`:

```powershell
idf.py set-target esp32p4
idf.py fullclean
idf.py build
```

In menuconfig, if **ESP-Hosted config** is missing: set **Wi-Fi Remote** → implementation to **ESP-HOSTED**, then enable **Bluetooth Support → Enable Hosted Bluetooth support**.

Verify these are set (e.g. `idf.py menuconfig`):

- `Component config` → `Bluetooth` → **Bluetooth** enabled, **NimBLE** host, controller **Disabled**
- `Component config` → `ESP-Hosted config` → **Enable Hosted Bluetooth support**

## Serial console

```text
wifi connect <SSID> [password]
wifi status
```

## Reset Wi‑Fi

Erase NVS namespace `wifi_cfg`, or `idf.py erase-flash`, or `wifi mode ap` then provision again.

## Boot loop: `vApplicationGetIdleTaskMemory` assert

If the serial log reboots with:

```text
assert failed: vApplicationGetIdleTaskMemory port_common.c:53 (pxStackBufferTemp != NULL)
```

right after `sleep_gpio` / before `app_main`, **internal DRAM is exhausted** before FreeRTOS can create idle tasks. Common causes: ESP-Hosted `constructor` init plus large Wi‑Fi static buffers and NimBLE. This project defers hosted init to `app_main` and trims buffers in `sdkconfig.defaults.esp32p4` (NimBLE → **External SPIRAM**, dynamic Wi‑Fi TX, smaller buffer counts). Rebuild with:

```powershell
idf.py reconfigure
idf.py build
idf.py flash monitor
```

A healthy boot should reach `app_main` logs and eventually `BLE provisioning GATT ready (PROV_NINO)` or `NimBLE host started`.

## BLE: `bt_controller_init failed` / co-processor version `0.0.0`

If the log shows:

```text
W transport: Version mismatch: Host [2.12.0] > Co-proc [0.0.0]
W wifi_prov_ble: bt_controller_init failed
```

the **ESP32-C6** (Wi‑Fi/BLE slave) is not running a matching **ESP-Hosted** firmware. BLE provisioning over GATT will not work until the C6 is flashed. **HTTP provisioning over soft AP still works.**

Flash the hosted slave image for the Function EV board (from Espressif `esp-hosted` / board docs), then reboot the P4. After that you should see `C6 BLE controller ready` and advertising without `adv_set_fields rc=4`.

## UVC: `Could not allocate USB transfers` / `heap_caps_free`

If the camera fails to open when wake word, BLE, and USB start together, rebuild with `CONFIG_USB_HOST_DWC_DMA_CAP_MEMORY_IN_PSRAM` (set in `sdkconfig.defaults.esp32p4`). Wake word is delayed until after USB init so it does not race the camera stream.
