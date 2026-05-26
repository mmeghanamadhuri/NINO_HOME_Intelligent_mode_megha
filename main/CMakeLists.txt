idf_component_register(
    SRCS "main.c" "audio_playback.c" "audio_capture.c" "audio_queue.c" "voice_ws_client.c" "voice_assist.c" "voice_wake.cpp"
           "touch_sensor.c" "bsp_qt2120.c"
    INCLUDE_DIRS "."
    EMBED_FILES "PDTM.wav"
    REQUIRES esp_psram esp_wifi esp_event esp_netif nvs_flash esp_http_server console esp32_p4_function_ev_board esp_websocket_client esp-sr driver
)
