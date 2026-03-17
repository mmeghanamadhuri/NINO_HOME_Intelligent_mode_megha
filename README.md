# USB Camera Complete Flow

## Overview

This project runs on the ESP32-P4 Function EV Board and does the following:

1. Starts the ESP32-P4 application
2. Brings up Wi-Fi in AP mode
3. Starts an HTTP server
4. Waits for a USB UVC camera on the host port
5. Opens an MJPEG stream from the camera
6. Copies the latest JPEG frame into RAM
7. Serves the frame stream to a browser over HTTP
8. Exposes a terminal command to dump CPU/task runtime stats

---

## Runtime Data Path

USB Camera -> USB Host -> UVC Host Driver -> Frame Queue -> Latest Frame Buffer -> HTTP Server -> Browser

---

## Boot Flow

When `app_main()` runs in [`main/main.c`](/d:/Sirena%20Stuff/USB_Camera/main/main.c):

1. NVS is initialized
2. Shared mutex and UVC frame queue are created
3. Wi-Fi AP is started
4. HTTP server is started
5. Console REPL is started
6. USB host stack is installed
7. USB host event task is started
8. UVC host driver is installed
9. The app waits for a UVC camera on port `J18`

---

## Wi-Fi Flow

The project starts a SoftAP with:

- SSID: `ESP32_P4_CAM`
- Password: `12345678`

After boot:

1. Connect your phone or laptop to `ESP32_P4_CAM`
2. Open `http://192.168.4.1/`

---

## HTTP Flow

The HTTP server exposes these endpoints:

- `/`
  - Simple web page with an `<img>` pointing to `/stream`
- `/stream`
  - Live MJPEG multipart stream
- `/snapshot.jpg`
  - One JPEG frame

The browser page continuously reads MJPEG frames from `/stream`.

---

## Camera Flow

When a USB camera is connected:

1. The UVC driver reports the device connection
2. The app reads the supported frame formats
3. It selects an MJPEG mode
4. It prefers `640x480`
5. It caps the stream target to `5 FPS`
6. It opens the camera stream
7. It starts receiving frames

The callback does not process the frame directly for HTTP.
It pushes the frame pointer into a queue.

Then the UVC stream task:

1. Receives the frame from the queue
2. Copies frame bytes into the shared latest-frame buffer
3. Logs periodic frame info
4. Returns the frame to the UVC driver using `uvc_host_frame_return()`

That last step is important. Without returning frames, the driver starves and starts printing underflow warnings.

---

## Latest Frame Buffer Flow

The project keeps only one shared latest JPEG frame in memory.

Why:

- simpler than buffering many frames
- good enough for browser MJPEG viewing
- reduces memory pressure

How it works:

1. A new camera frame arrives
2. The app copies it into the latest-frame buffer
3. The old frame is overwritten
4. HTTP handlers always serve the newest available frame

This means the web page is not guaranteed to see every frame.
It always gets the most recent one.

---

## CPU Dump Terminal Flow

The project also starts a console REPL on the terminal.

Prompt:

```text
usb_cam>
```

Available command:

```text
cpu_dump
```

What `cpu_dump` shows:

- task name
- core ID
- priority
- task state
- stack high-water mark
- runtime counter
- approximate CPU percentage

Use it while the app is running to inspect task behavior at runtime.

---

## Typical Usage

### 1. Build and flash

```powershell
idf.py build
idf.py flash monitor
```

### 2. Connect camera

- Plug the USB webcam into the USB host port `J18`

### 3. Connect to Wi-Fi

- SSID: `ESP32_P4_CAM`
- Password: `12345678`

### 4. View stream

Open:

```text
http://192.168.4.1/
```

### 5. Check CPU stats

In terminal:

```text
cpu_dump
```

---

## Important Notes

### Wi-Fi backend

This project is configured for:

- ESP32-P4 as host
- on-board ESP32-C6 as Wi-Fi coprocessor
- `esp_wifi_remote` + `esp_hosted`

It should not use the old `esp-extconn` path for this board setup.

### Camera format

The browser stream depends on MJPEG.
If the connected camera does not support MJPEG, HTTP video streaming will not work correctly.

### Throughput and warnings

If you still see repeated:

```text
Frame buffer underflow, processing is too slow
```

then possible causes are:

- stream FPS too high
- camera bandwidth too high
- HTTP serving too slow
- frame copy/processing taking too long

In that case reduce resolution or FPS further.

---

## Summary

The complete flow is:

1. ESP32-P4 boots
2. Wi-Fi AP starts
3. HTTP server starts
4. USB camera is detected
5. UVC MJPEG stream starts
6. Latest frame is copied into RAM
7. Browser reads frames from `/stream`
8. You can inspect CPU/task usage anytime with `cpu_dump`
