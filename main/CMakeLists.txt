set(main_requires esp_psram esp_wifi esp_event esp_netif nvs_flash esp_http_server esp_http_client mdns
    console esp32_p4_function_ev_board esp_websocket_client driver usb
    esp_hosted)

set(main_priv_requires bt)

set(main_srcs
    audio_loopback.c servo_recplay.c mic_input.c wifi_prov_ble.c servo_dxl.c servo_motion.c
    face_tracker.c face_detect.cpp main.c audio_playback.c audio_capture.c audio_queue.c
    voice_ws_client.c voice_assist.c sd_record.c touch_sensor.c bsp_qt2120.c nino_eye.c
    push_buttons.c rgb_led.c)

if(CONFIG_NINO_EYE_DISPLAY_TFT)
    list(APPEND main_srcs st7735.c tft_neutral.c)
else()
    list(APPEND main_srcs ssd1351.c)
endif()

idf_component_register(
<<<<<<< HEAD
    SRCS ${main_srcs}
=======
    SRCS "servo_recplay.c" "usb_mic.c" "mic_input.c" "wifi_prov_ble.c" "servo_dxl.c" "servo_motion.c" "face_tracker.c" "face_detect.cpp" "main.c" "audio_playback.c" "audio_capture.c" "audio_queue.c" "voice_ws_client.c" "voice_assist.c" "voice_wake_stub.c"
           "nino_eye.c" "ssd1351.c" "push_buttons.c"
>>>>>>> b63091e (Fixed the reply path from Ptron Mic source to P4 speaker out)
    INCLUDE_DIRS "."
    EMBED_FILES "PDTM.wav" "beep.wav" "WIFI.wav" "Hello-home.wav" "Wifi_Unable.wav" "NiNO-Home_Wifi.wav"
                "schedule_dinnner.wav" "Bday_Surprise.wav" "DEMO_main.wav"
    REQUIRES ${main_requires}
    PRIV_REQUIRES ${main_priv_requires}
)
