if(NOT DEFINED PYTHON)
    set(PYTHON python3)
endif()
set(_nino_ws_c "${CMAKE_SOURCE_DIR}/managed_components/espressif__esp_websocket_client/esp_websocket_client.c")
if(EXISTS "${_nino_ws_c}")
    execute_process(
        COMMAND ${PYTHON} "${CMAKE_CURRENT_LIST_DIR}/patches/esp_websocket_client_write0.py" "${_nino_ws_c}"
        RESULT_VARIABLE _nino_ws_patch
    )
    if(NOT _nino_ws_patch EQUAL 0)
        message(FATAL_ERROR "esp_websocket_client write-0 patch failed")
    endif()
endif()

set(main_requires esp_psram esp_wifi esp_event esp_netif nvs_flash esp_http_server esp_http_client mdns
    console esp32_p4_function_ev_board esp_websocket_client driver usb
    esp_hosted fatfs sdmmc esp_adc)

set(main_priv_requires bt)

set(main_srcs
    servo_recplay.c mic_input.c wifi_prov_ble.c servo_dxl.c servo_motion.c
    face_tracker.c face_detect.cpp main.c audio_playback.c audio_capture.c audio_queue.c
    voice_ws_client.c voice_assist.c nino_eye.c push_buttons.c rgb_led.c
    music_stream.c sd_record.c battery_adc.c)

if(CONFIG_NINO_EYE_DISPLAY_TFT)
    list(APPEND main_srcs st7735.c tft_neutral.c)
else()
    list(APPEND main_srcs ssd1351.c)
endif()

idf_component_register(
    SRCS ${main_srcs}
    INCLUDE_DIRS "."
    EMBED_FILES "beep.wav" "WIFI.wav" "Hello-home.wav" "NiNO-Home_Wifi.wav"
                "schedule_dinnner.wav" "Bday_Surprise.wav" "DEMO_main.wav"
                "low_battery.wav"
    REQUIRES ${main_requires}
    PRIV_REQUIRES ${main_priv_requires}
)
