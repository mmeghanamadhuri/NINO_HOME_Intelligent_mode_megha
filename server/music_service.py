"""Per-device music sessions and the PCM stream the ESP32-P4 pulls.

Flow: voice command -> resolve track -> tell the board to open
GET /music/stream.wav -> board pulls PCM until the track ends or is stopped.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator

from music_source import (
    MusicError,
    MusicNotConfiguredError,
    MusicUnavailableError,
    Track,
    open_pcm_process,
    resolve_track,
    stream_sample_rate,
)

logger = logging.getLogger(__name__)

# Board-side ring buffer is small, so send audio in modest bursts.
PCM_CHUNK_BYTES = 4096
# Streaming WAV: real length is unknown, so advertise the maximum.
_STREAMING_SIZE = 0xFFFFFFFF
ESP_CONTROL_TIMEOUT_SECONDS = 5.0


class MusicNoDeviceError(MusicError):
    """The robot has no reachable base URL for music control."""


def wav_stream_header(sample_rate: int, *, channels: int = 1, bits: int = 16) -> bytes:
    """44-byte WAV header for a stream of unknown length."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", _STREAMING_SIZE),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits),
            b"data",
            struct.pack("<I", _STREAMING_SIZE),
        ]
    )


@dataclass
class Session:
    device_id: str
    track: Track
    sample_rate: int
    started_at: float = field(default_factory=time.time)
    process: object | None = None
    stopped: threading.Event = field(default_factory=threading.Event)
    bytes_sent: int = 0

    def elapsed_seconds(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self.bytes_sent / (self.sample_rate * 2)


class MusicService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._last_tracks: dict[str, Track] = {}

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _key(device_id: str | None) -> str:
        return (device_id or "").strip() or "default"

    def stream_url_for(self, device_id: str | None) -> str:
        host = os.environ.get("MUSIC_STREAM_HOST", "").strip()
        if not host:
            host = f"{_lan_ip()}:8000"
        return f"http://{host}/music/stream.wav?device_id={self._key(device_id)}"

    # ---------------------------------------------------------- public API

    def play(
        self, device_id: str | None, query: str, *, notify_device: bool = True
    ) -> Track:
        """Resolve the query and tell the board to start pulling the stream.

        notify_device=False leaves the session armed without contacting the robot,
        so the stream can be checked from the PC before the firmware exists.
        """
        track = resolve_track(query)
        key = self._key(device_id)
        rate = stream_sample_rate()

        self.stop(device_id, notify_device=False)
        with self._lock:
            self._sessions[key] = Session(device_id=key, track=track, sample_rate=rate)

        if notify_device:
            try:
                self._esp_control(
                    device_id, "play", {"url": self.stream_url_for(device_id)}
                )
            except MusicError as exc:
                # Keep the per-device session so stop/shutup still belong to
                # music on THIS robot even if firmware has not landed yet.
                logger.warning("Could not start music on %s: %s", key, exc)
        with self._lock:
            self._last_tracks[key] = track
        logger.info(
            "Music play | device=%s track=%s push=%s", key, track.spoken(), notify_device
        )
        return track

    def stop(self, device_id: str | None, *, notify_device: bool = True) -> bool:
        key = self._key(device_id)
        with self._lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            session.stopped.set()
            _terminate(session.process)
        if notify_device and session is not None:
            try:
                self._esp_control(device_id, "stop", {})
            except MusicError as exc:
                logger.warning("Could not stop music on %s: %s", key, exc)
        return session is not None

    def is_playing(self, device_id: str | None) -> bool:
        """True only if THIS device currently has an armed music session."""
        return self.current(device_id) is not None

    def last_track(self, device_id: str | None) -> Track | None:
        with self._lock:
            return self._last_tracks.get(self._key(device_id))

    def current(self, device_id: str | None) -> Session | None:
        with self._lock:
            return self._sessions.get(self._key(device_id))

    def status(self, device_id: str | None) -> dict:
        session = self.current(device_id)
        if session is None:
            return {
                "playing": False,
                "device_id": self._key(device_id),
                "sample_rate": stream_sample_rate(),
            }
        return {
            "playing": True,
            "device_id": self._key(device_id),
            "title": session.track.title,
            "artist": session.track.artist,
            "duration_seconds": session.track.duration_seconds,
            "elapsed_seconds": round(session.elapsed_seconds(), 1),
            "sample_rate": session.sample_rate,
            "stream_url": self.stream_url_for(device_id),
        }

    # ------------------------------------------------------------ streaming

    def iter_stream(self, device_id: str | None) -> Iterator[bytes]:
        """Yield a WAV header then PCM until the track ends or is stopped."""
        key = self._key(device_id)
        session = self.current(key)
        if session is None:
            raise MusicNoDeviceError(f"No music session for device {key!r}")

        yield wav_stream_header(session.sample_rate)

        try:
            process = open_pcm_process(session.track, sample_rate=session.sample_rate)
        except MusicError as exc:
            logger.warning("Music decode failed for %s: %s", key, exc)
            return
        session.process = process
        stdout = process.stdout

        try:
            while not session.stopped.is_set():
                chunk = stdout.read(PCM_CHUNK_BYTES) if stdout else b""
                if not chunk:
                    break
                session.bytes_sent += len(chunk)
                yield chunk
        except (BrokenPipeError, ConnectionResetError):
            logger.info("Music stream closed by device %s", key)
        finally:
            _terminate(process)
            with self._lock:
                if self._sessions.get(key) is session:
                    self._sessions.pop(key, None)
            logger.info(
                "Music stream finished | device=%s track=%s played=%.1fs",
                key,
                session.track.title[:60],
                session.elapsed_seconds(),
            )

    # -------------------------------------------------------- device control

    def _esp_control(self, device_id: str | None, action: str, payload: dict) -> None:
        """POST /music/play or /music/stop on the board."""
        from esp_playback import device_base_url

        base = device_base_url(device_id)
        if not base:
            raise MusicNoDeviceError(
                "No base URL for this robot (check devices.json or CAMERA_SOURCE)"
            )
        url = f"{base}/music/{action}"
        body = _json_bytes(payload)
        request = urllib.request.Request(
            url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=ESP_CONTROL_TIMEOUT_SECONDS) as resp:
                if resp.status != 200:
                    raise MusicUnavailableError(f"Robot returned HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise MusicNotConfiguredError(
                    "This robot's firmware has no /music endpoint yet "
                    "(see docs/music_stream.md)"
                ) from exc
            raise MusicUnavailableError(f"Robot HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise MusicNoDeviceError(f"Cannot reach the robot: {exc.reason}") from exc


def _json_bytes(payload: dict) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")


def _terminate(process: object | None) -> None:
    if process is None:
        return
    try:
        process.terminate()  # type: ignore[attr-defined]
        process.wait(timeout=2)  # type: ignore[attr-defined]
    except Exception:
        try:
            process.kill()  # type: ignore[attr-defined]
        except Exception:
            pass


def _lan_ip() -> str:
    """Best-effort LAN address the ESP can reach this server on."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


_MUSIC_SERVICE: MusicService | None = None


def get_music_service() -> MusicService:
    global _MUSIC_SERVICE
    if _MUSIC_SERVICE is None:
        _MUSIC_SERVICE = MusicService()
    return _MUSIC_SERVICE
