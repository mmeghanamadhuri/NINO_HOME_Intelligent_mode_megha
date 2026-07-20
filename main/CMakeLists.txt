set(main_requires esp_psram esp_wifi esp_event esp_netif nvs_flash esp_http_server mdns
    console esp32_p4_function_ev_board esp_websocket_client esp-sr driver usb
    esp_hosted)

set(main_priv_requires bt)

idf_component_register(
    SRCS "usb_mic.c" "mic_input.c" "wifi_prov_ble.c" "servo_dxl.c" "servo_motion.c" "face_tracker.c" "face_detect.cpp" "main.c" "audio_playback.c" "audio_capture.c" "audio_queue.c" "voice_ws_client.c" "voice_assist.c" "voice_wake.cpp"
           "touch_sensor.c" "bsp_qt2120.c" "nino_eye.c" "ssd1351.c"
    INCLUDE_DIRS "."
    EMBED_FILES "PDTM.wav" "beep.wav" "WIFI.wav" "Hello-home.wav" "WIFI-UNABLE.wav" "GO-APP.wav"
    REQUIRES ${main_requires}
    PRIV_REQUIRES ${main_priv_requires}
)
