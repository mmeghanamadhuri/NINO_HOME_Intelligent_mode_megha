idf_component_register(
    SRCS "main.c" "audio_playback.c" "audio_capture.c" "voice_ws_client.c" "voice_assist.c" "voice_wake.cpp"
    INCLUDE_DIRS "."
    REQUIRES esp_psram esp_wifi esp_event esp_netif nvs_flash esp_http_server console esp32_p4_function_ev_board esp_websocket_client esp-sr
)
