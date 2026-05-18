# For more information about build system see
# https://docs.espressif.com/projects/esp-idf/en/latest/api-guides/build-system.html
# The following five lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(USB_Camera)

# Use ESP32-P4 Function EV Board BSP without LVGL (audio + pins only for this app).
add_compile_definitions(BSP_CONFIG_NO_GRAPHIC_LIB=1)