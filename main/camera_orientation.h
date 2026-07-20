#pragma once

/*
 * Clockwise rotation needed to make the physical USB camera upright.
 *
 * Keep all consumers of camera pixels (the browser preview and ESP-DL) on
 * this setting so detector coordinates match the visible orientation.
 */
#define NINO_CAMERA_ROTATION_DEG 90
