# ESP32-P4 USB Camera Streaming Reference

## Purpose

This document is a clean reference for explaining the project in a video.

It covers:

- what the project does
- the complete system flow
- the important `menuconfig` / configuration items
- how the runtime works
- how to demonstrate it

---

## Project Summary

This project runs on the **ESP32-P4 Function EV Board** and does the following:

1. Uses the **ESP32-P4** as the main host processor
2. Uses the on-board Wi-Fi path to create a network connection
3. Uses the **USB Host** interface to connect a **UVC USB camera**
4. Receives **MJPEG frames** from the camera
5. Stores the latest frame in memory
6. Serves the camera output over **HTTP**
7. Lets a browser view the camera feed
8. Exposes a terminal command to inspect runtime CPU/task usage

---

## High-Level Flow

### Full data flow

`USB Camera -> USB Host -> UVC Host Driver -> Latest Frame Buffer -> HTTP Server -> Wi-Fi -> Browser`

### Control and debug flow

`Terminal -> Console REPL -> cpu_dump -> Runtime task/CPU statistics`

---

## Hardware Flow

### Hardware used

- ESP32-P4 Function EV Board
- USB UVC webcam
- USB cable
- Host USB port on the board
- Power supply / USB power for the board

### Physical connection

`USB Webcam -> J18 USB Host Port -> ESP32-P4`

---

## Software Flow

## 1. Boot Phase

When the board boots:

1. Bootloader starts
2. PSRAM is initialized
3. Application starts
4. `app_main()` runs
5. Core software resources are created:
   - mutex for latest frame access
   - frame queue for UVC frames

---

## 2. Wi-Fi Phase

The project starts a Wi-Fi access point so another device can connect directly.

### AP settings

- SSID: `ESP32_P4_CAM`
- Password: `12345678`

### Why AP mode is used

- simple demo setup
- no external router required
- direct browser access from phone or laptop

---

## 3. HTTP Server Phase

The HTTP server starts after Wi-Fi initialization.

### HTTP endpoints

- `/`
  - loads a simple HTML page
  - displays the live image stream

- `/stream`
  - serves multipart MJPEG stream
  - browser continuously reads JPEG frames

- `/snapshot.jpg`
  - serves one current JPEG frame

---

## 4. USB Host Phase

The project installs the USB Host stack and creates a USB library task.

### Purpose

- detect USB camera connection
- handle host-level USB events
- allow the UVC class driver to work

---

## 5. UVC Camera Phase

When a USB UVC camera is connected:

1. The UVC host driver detects the device
2. The project reads the camera frame descriptors
3. Available camera modes are printed
4. The project chooses a suitable streaming mode

### Selected target mode

- Format: `MJPEG`
- Resolution: `640x480`
- FPS target: `5`

### Why MJPEG is selected

The browser endpoint uses MJPEG streaming, so the camera must provide JPEG-compatible frames.

---

## 6. Frame Processing Phase

The frame pipeline works like this:

1. Camera frame arrives from UVC driver
2. Callback pushes the frame pointer into a queue
3. Stream task receives the frame from the queue
4. Frame data is copied into a shared latest-frame buffer
5. Frame is returned to the UVC driver with `uvc_host_frame_return()`

### Why returning the frame is important

If the frame is not returned:

- UVC frame buffers get exhausted
- streaming stalls
- repeated underflow warnings appear

This was an important implementation detail during debugging.

---

## 7. Browser Streaming Phase

When a phone or laptop connects to the AP:

1. Open browser
2. Visit `http://192.168.4.1/`
3. Browser loads the HTML page
4. `<img src="/stream">` starts fetching MJPEG data
5. Browser shows live camera frames

---

## Runtime Terminal Support

The project also starts a UART console REPL.

### Prompt

```text
usb_cam>
```

### Runtime stats command

```text
cpu_dump
```

### What `cpu_dump` shows

- task name
- core
- priority
- state
- stack high-water mark
- runtime count
- approximate CPU percentage

This is useful for showing that the system is running comfortably in real time.

---

## Important Configuration / Menuconfig Reference

This section is the most useful one for video explanation.

## Main project configuration areas

### 1. USB Host

Enabled / required:

- USB Host stack support
- USB host control transfer size
- USB host hub support

Relevant project config:

- `CONFIG_USB_HOST_CONTROL_TRANSFER_MAX_SIZE=4096`
- `CONFIG_USB_HOST_HW_BUFFER_BIAS_IN=y`
- `CONFIG_USB_HOST_HUBS_SUPPORTED=y`

### 2. UVC Host

Enabled through component dependency:

- `espressif/usb_host_uvc`

Used by code for:

- UVC device detection
- frame format listing
- stream open/start
- frame callback handling

### 3. Wi-Fi / Hosted Wi-Fi path

For this board we used the hosted Wi-Fi/coprocessor path, not the wrong native host path that caused crashes earlier.

Important config items:

- `CONFIG_ESP_WIFI_REMOTE_ENABLED=y`
- `CONFIG_ESP_HOSTED_ENABLED=y`
- `CONFIG_SLAVE_IDF_TARGET_ESP32C6=y`
- `CONFIG_ESP_HOSTED_CP_TARGET_ESP32C6=y`
- `CONFIG_ESP_HOSTED_P4_DEV_BOARD_FUNC_BOARD=y`

### 4. Wi-Fi remote performance tuning

Important settings used:

- `CONFIG_WIFI_RMT_STATIC_RX_BUFFER_NUM=16`
- `CONFIG_WIFI_RMT_DYNAMIC_RX_BUFFER_NUM=64`
- `CONFIG_WIFI_RMT_DYNAMIC_TX_BUFFER_NUM=64`
- `CONFIG_WIFI_RMT_AMPDU_TX_ENABLED=y`
- `CONFIG_WIFI_RMT_TX_BA_WIN=32`
- `CONFIG_WIFI_RMT_AMPDU_RX_ENABLED=y`
- `CONFIG_WIFI_RMT_RX_BA_WIN=32`

### 5. LWIP / Network buffers

Important settings used:

- `CONFIG_LWIP_TCP_SND_BUF_DEFAULT=65534`
- `CONFIG_LWIP_TCP_WND_DEFAULT=65534`
- `CONFIG_LWIP_TCP_RECVMBOX_SIZE=64`
- `CONFIG_LWIP_UDP_RECVMBOX_SIZE=64`
- `CONFIG_LWIP_TCPIP_RECVMBOX_SIZE=64`
- `CONFIG_LWIP_TCP_SACK_OUT=y`

### 6. Flash and partition size

Needed because the application became larger after adding Wi-Fi + HTTP + camera support.

Important settings:

- `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`
- `CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE=y`

### 7. FreeRTOS runtime statistics

Enabled so runtime CPU usage can be shown from terminal.

Important settings:

- `CONFIG_FREERTOS_USE_TRACE_FACILITY=y`
- `CONFIG_FREERTOS_USE_STATS_FORMATTING_FUNCTIONS=y`
- `CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS=y`
- `CONFIG_FREERTOS_VTASKLIST_INCLUDE_COREID=y`
- `CONFIG_FREERTOS_RUN_TIME_STATS_USING_ESP_TIMER=y`

### 8. Console support

Enabled to use terminal commands during runtime.

Important area:

- ESP console over UART

Used for:

- monitor interaction
- `cpu_dump`

---

## What We Learned During Debugging

This is good material for explaining engineering decisions in the recording.

### 1. Wrong Wi-Fi backend caused boot crash

At one stage, the project was using the wrong Wi-Fi backend:

- `ESP_HOST_WIFI`

That caused:

- `esp_adapter: Not support read mac`
- Wi-Fi init crash

The fix was to use:

- `esp_wifi_remote`
- `esp_hosted`
- ESP32-C6 coprocessor config for the P4 board

### 2. App partition became too small

After adding camera + Wi-Fi + HTTP features:

- binary no longer fit in the old partition layout

The fix was:

- 16 MB flash setting
- large single-app partition table

### 3. UVC frame underflow happened because frames were not returned

When the HTTP path was first added, frames were copied but not returned back to the UVC driver.

That caused:

- repeated frame buffer underflow warnings

The fix was:

1. queue incoming UVC frames
2. process them in the stream task
3. always call `uvc_host_frame_return()`

---

## Runtime Result Interpretation

From the runtime stats:

- `USB-UVC` uses the largest share of active CPU time
- `httpd`, `uvc_stream`, and SDIO tasks stay low
- `IDLE0` and `IDLE1` remain high

### Meaning

- system has healthy CPU headroom
- camera streaming is working
- HTTP serving is not saturating the CPU
- Wi-Fi task load is manageable

This is a good result for the demo.

---

## Demo Script for Recording

You can use this sequence in the video:

### Step 1. Introduce the board

Explain:

- ESP32-P4 Function EV Board
- USB camera connected on host port
- Wi-Fi AP created by the board
- browser used as display

### Step 2. Explain the flow

Say:

`USB camera -> UVC host -> frame buffer -> HTTP server -> Wi-Fi -> browser`

### Step 3. Show monitor logs

Point out:

- USB host stack installation
- UVC driver installation
- camera connected
- stream started
- frame logs appearing

### Step 4. Show Wi-Fi connection

Connect a phone or laptop to:

- `ESP32_P4_CAM`

### Step 5. Show browser output

Open:

```text
http://192.168.4.1/
```

Then mention:

- `/stream` is the MJPEG stream
- `/snapshot.jpg` is the single frame endpoint

### Step 6. Show runtime stats

In terminal:

```text
cpu_dump
```

Explain:

- UVC task is the main active worker
- large idle percentage means the system still has headroom

### Step 7. Explain key implementation points

Mention:

- MJPEG selected for browser compatibility
- latest frame buffering used for simple HTTP streaming
- FreeRTOS runtime stats enabled for profiling
- frame return to UVC driver is necessary for stable streaming

---

## Commands to Mention

### Build

```powershell
idf.py build
```

### Flash and monitor

```powershell
idf.py flash monitor
```

### Runtime stats

```text
cpu_dump
```

### Browser

```text
http://192.168.4.1/
http://192.168.4.1/stream
http://192.168.4.1/snapshot.jpg
```

---

## Final One-Line Explanation

This project turns the ESP32-P4 Function EV Board into a USB camera host that captures MJPEG frames from a UVC webcam and serves them live to a browser over Wi-Fi using an onboard HTTP server.
