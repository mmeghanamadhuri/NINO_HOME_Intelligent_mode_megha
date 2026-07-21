"""Cached current-weather lookups for a device's reported coordinates."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from device_registry import DeviceRecord

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_CACHE_TTL_SECONDS = 600.0

_WEATHER_DESCRIPTIONS = {
    0: "clear skies",
    1: "mostly clear skies",
    2: "partly cloudy skies",
    3: "overcast skies",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "light freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "a thunderstorm",
    96: "a thunderstorm with light hail",
    99: "a thunderstorm with heavy hail",
}


class WeatherUnavailableError(RuntimeError):
    """The provider did not return usable current weather data."""


class DeviceLocationUnavailableError(RuntimeError):
    """The device has not supplied a latitude and longitude."""


@dataclass(frozen=True)
class _CachedWeather:
    expires_at: float
    payload: dict[str, Any]


def weather_description(code: int | None) -> str:
    return _WEATHER_DESCRIPTIONS.get(code, "unknown conditions")


class WeatherService:
    def __init__(
        self,
        *,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        request_timeout_seconds: float = 5.0,
    ) -> None:
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self.request_timeout_seconds = request_timeout_seconds
        self._cache: dict[tuple[float, float], _CachedWeather] = {}
        self._lock = threading.Lock()

    def current_for_device(self, device: DeviceRecord) -> dict[str, Any]:
        if device.latitude is None or device.longitude is None:
            raise DeviceLocationUnavailableError(
                f"No location configured for device '{device.device_id}'"
            )
        return self.current_for_coordinates(device.latitude, device.longitude)

    def current_for_coordinates(self, latitude: float, longitude: float) -> dict[str, Any]:
        key = (round(latitude, 5), round(longitude, 5))
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expires_at > now:
                return {**cached.payload, "cached": True}

        try:
            response = requests.get(
                OPEN_METEO_FORECAST_URL,
                params={
                    "latitude": key[0],
                    "longitude": key[1],
                    "current": (
                        "temperature_2m,apparent_temperature,weather_code,"
                        "wind_speed_10m"
                    ),
                    "timezone": "auto",
                },
                timeout=self.request_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            current = body.get("current")
            if not isinstance(current, dict):
                raise WeatherUnavailableError("Weather provider returned no current conditions")
            code = _optional_int(current.get("weather_code"))
            payload = {
                "temperature_c": _required_float(current.get("temperature_2m"), "temperature"),
                "apparent_temperature_c": _required_float(
                    current.get("apparent_temperature"), "apparent temperature"
                ),
                "wind_speed_kph": _required_float(current.get("wind_speed_10m"), "wind speed"),
                "weather_code": code,
                "description": weather_description(code),
                "observed_at": str(current.get("time") or ""),
                "timezone": str(body.get("timezone") or ""),
                "cached": False,
            }
        except WeatherUnavailableError:
            raise
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning("Current-weather request failed: %s", exc)
            raise WeatherUnavailableError("Could not retrieve current weather") from exc

        with self._lock:
            self._cache[key] = _CachedWeather(
                expires_at=time.monotonic() + self.cache_ttl_seconds,
                payload=payload,
            )
        return payload


def weather_voice_reply(device: DeviceRecord, weather: dict[str, Any]) -> str:
    location = device.display_name or device.device_id
    return (
        f"For {location}, it is {weather['temperature_c']:.0f} degrees Celsius, "
        f"feels like {weather['apparent_temperature_c']:.0f}, with "
        f"{weather['description']}. Wind speed is {weather['wind_speed_kph']:.0f} "
        f"kilometres per hour."
    )


def _required_float(value: object, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise WeatherUnavailableError(f"Weather provider returned invalid {name}") from exc


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_WEATHER_SERVICE: WeatherService | None = None


def get_weather_service() -> WeatherService:
    global _WEATHER_SERVICE
    if _WEATHER_SERVICE is None:
        _WEATHER_SERVICE = WeatherService()
    return _WEATHER_SERVICE
