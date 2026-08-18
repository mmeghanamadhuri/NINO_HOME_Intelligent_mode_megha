"""Resolve a spoken song request to a decoded PCM stream for the robot speaker.

JioSaavn is the primary catalogue: its search needs no auth and hands back plain
CDN URLs that are not tied to the requesting IP, which matters because this site's
egress address rotates. yt-dlp is kept as an opt-in fallback (MUSIC_ENABLE_YTDLP=1)
but it needs a PO Token and a stable IP to work.

ffmpeg decodes whatever URL we end up with into the raw mono 16-bit PCM the
ESP32-P4 codec consumes. Nothing is written to disk.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_STREAM_HZ = 32000
SEARCH_TIMEOUT_SECONDS = 15.0

SAAVN_API = "https://www.jiosaavn.com/api.php"
# Long-standing key for JioSaavn's DES-ECB wrapped media URLs.
_SAAVN_DES_KEY = b"38346591"
_SAAVN_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
_SAAVN_QUALITIES = ("_320.mp4", "_160.mp4", "_96.mp4")

# Filler that search results add to titles but nobody wants read aloud.
_TITLE_NOISE_RE = re.compile(
    r"\s*[\(\[][^\)\]]*(?:official|lyric|audio|video|hd|4k|remaster|visualizer|"
    r"music\s*video|full\s*song|with\s*lyrics)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)


class MusicError(RuntimeError):
    """Base for music failures that become spoken replies."""


class MusicNotConfiguredError(MusicError):
    """ffmpeg (or an enabled backend) is missing from the server."""


class MusicNotFoundError(MusicError):
    """The search returned nothing playable."""


class MusicUnavailableError(MusicError):
    """Network or decoder failure."""


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    duration_seconds: int
    stream_url: str
    page_url: str
    source: str = "saavn"

    def spoken(self) -> str:
        if self.artist and _squash(self.artist) not in _squash(self.title):
            return f"{self.title} by {self.artist}"
        return self.title


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def ffmpeg_path() -> str | None:
    return shutil.which(os.environ.get("FFMPEG_BINARY", "ffmpeg"))


def stream_sample_rate() -> int:
    raw = os.environ.get("MUSIC_STREAM_HZ", "").strip()
    if not raw:
        return DEFAULT_STREAM_HZ
    try:
        rate = int(raw)
    except ValueError:
        logger.warning("Invalid MUSIC_STREAM_HZ=%r; using %d", raw, DEFAULT_STREAM_HZ)
        return DEFAULT_STREAM_HZ
    # ES8311 accepts 8k-48k; outside that the codec open fails on the board.
    return max(8000, min(48000, rate))


def _clean_title(raw: str) -> str:
    cleaned = _TITLE_NOISE_RE.sub("", html.unescape(str(raw or ""))).strip(" -–—|")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# ------------------------------------------------------------------ JioSaavn


def decrypt_saavn_url(encrypted: str) -> str:
    """Undo the DES-ECB wrapper JioSaavn puts around its CDN media URLs."""
    try:
        from Crypto.Cipher import DES
    except ImportError as exc:
        raise MusicNotConfiguredError(
            "pycryptodome is not installed (needed for JioSaavn URLs)"
        ) from exc

    try:
        raw = DES.new(_SAAVN_DES_KEY, DES.MODE_ECB).decrypt(
            base64.b64decode(str(encrypted or "").strip())
        )
    except (ValueError, TypeError) as exc:
        raise MusicUnavailableError(f"Could not decrypt media URL: {exc}") from exc

    text = raw.decode("utf-8", errors="replace")
    if text:
        # PKCS#5 padding: the final byte repeats as the pad character.
        text = text.rstrip(text[-1])
    return text.strip()


def _saavn_get(params: dict) -> dict:
    url = f"{SAAVN_API}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": _SAAVN_UA, "Accept": "*/*"}
    )
    try:
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.URLError as exc:
        raise MusicUnavailableError(f"JioSaavn unreachable: {exc.reason}") from exc
    except (ValueError, urllib.error.HTTPError) as exc:
        raise MusicUnavailableError(f"JioSaavn search failed: {exc}") from exc


def _saavn_artist(more_info: dict) -> str:
    artist_map = more_info.get("artistMap") or {}
    primary = artist_map.get("primary_artists") or []
    if primary and isinstance(primary, list):
        name = str((primary[0] or {}).get("name") or "").strip()
        if name:
            return html.unescape(name)
    for key in ("primary_artists", "singers", "music"):
        value = str(more_info.get(key) or "").strip()
        if value:
            return html.unescape(value.split(",")[0].strip())
    return ""


def _best_quality_url(base_url: str) -> str:
    """Prefer 320 kbps; fall back if the CDN does not carry that rendition."""
    for quality in _SAAVN_QUALITIES:
        candidate = re.sub(r"_(?:12|48|96|160|320)\.mp4$", quality, base_url)
        request = urllib.request.Request(
            candidate, method="HEAD", headers={"User-Agent": _SAAVN_UA}
        )
        try:
            with urllib.request.urlopen(request, timeout=6) as resp:
                if resp.status == 200:
                    return candidate
        except (urllib.error.URLError, OSError):
            continue
    return base_url


def _resolve_via_saavn(query: str) -> Track:
    payload = _saavn_get(
        {
            "__call": "search.getResults",
            "_format": "json",
            "_marker": "0",
            "api_version": "4",
            "ctx": "web6dot0",
            "q": query,
            "p": 1,
            "n": 5,
        }
    )
    results = payload.get("results") or []
    if not results:
        raise MusicNotFoundError(f"JioSaavn has nothing for {query!r}")

    for song in results:
        more_info = song.get("more_info") or {}
        encrypted = more_info.get("encrypted_media_url")
        if not encrypted:
            continue
        stream_url = _best_quality_url(decrypt_saavn_url(encrypted))
        if not stream_url:
            continue
        try:
            duration = int(more_info.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        return Track(
            title=_clean_title(song.get("title") or query) or query,
            artist=_saavn_artist(more_info),
            duration_seconds=duration,
            stream_url=stream_url,
            page_url=str(song.get("perma_url") or ""),
            source="saavn",
        )

    raise MusicNotFoundError(f"No playable JioSaavn result for {query!r}")


# -------------------------------------------------------------------- yt-dlp


def _ytdlp_enabled() -> bool:
    return os.environ.get("MUSIC_ENABLE_YTDLP", "").strip().lower() in {"1", "true", "yes"}


def _resolve_via_ytdlp(query: str) -> Track:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise MusicNotConfiguredError("yt-dlp is not installed") from exc

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "socket_timeout": SEARCH_TIMEOUT_SECONDS,
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    except Exception as exc:
        raise MusicUnavailableError(f"yt-dlp search failed: {exc}") from exc

    entries = (info or {}).get("entries") or []
    if not entries:
        raise MusicNotFoundError(f"YouTube has nothing for {query!r}")
    entry = entries[0]
    stream_url = str(entry.get("url") or "")
    if not stream_url:
        raise MusicNotFoundError(f"No audio stream for {query!r}")
    return Track(
        title=_clean_title(entry.get("title") or query) or query,
        artist=re.sub(r"\s*-\s*Topic$", "", str(entry.get("uploader") or "")).strip(),
        duration_seconds=int(entry.get("duration") or 0),
        stream_url=stream_url,
        page_url=str(entry.get("webpage_url") or ""),
        source="youtube",
    )


# ------------------------------------------------------------------- public


def resolve_track(query: str) -> Track:
    """Search for a song and return a URL ffmpeg can decode."""
    cleaned = str(query or "").strip()
    if not cleaned:
        raise MusicNotFoundError("Empty search query")

    try:
        track = _resolve_via_saavn(cleaned)
        logger.info("Resolved %r via JioSaavn: %s", cleaned, track.spoken())
        return track
    except MusicNotFoundError:
        if not _ytdlp_enabled():
            raise
        logger.info("JioSaavn missed %r; trying yt-dlp", cleaned)
    except (MusicUnavailableError, MusicNotConfiguredError) as exc:
        if not _ytdlp_enabled():
            raise
        logger.warning("JioSaavn failed (%s); trying yt-dlp", exc)

    track = _resolve_via_ytdlp(cleaned)
    logger.info("Resolved %r via yt-dlp: %s", cleaned, track.spoken())
    return track


def open_pcm_process(track: Track, *, sample_rate: int) -> subprocess.Popen[bytes]:
    """Start ffmpeg decoding the track to mono 16-bit PCM on stdout."""
    binary = ffmpeg_path()
    if not binary:
        raise MusicNotConfiguredError("ffmpeg is not installed on the server")

    command = [
        binary,
        "-nostdin",
        "-loglevel", "error",
        # Long tracks over a CDN drop connections; let ffmpeg re-establish them.
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-user_agent", _SAAVN_UA,
        "-i", track.stream_url,
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "s16le",
        "-",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        raise MusicUnavailableError(f"Could not start ffmpeg: {exc}") from exc

    # Drain stderr so a chatty decoder cannot fill the pipe and stall playback.
    threading.Thread(
        target=_log_ffmpeg_stderr, args=(process, track.title), daemon=True
    ).start()
    logger.info(
        "ffmpeg decoding %r (%s) at %d Hz mono", track.title, track.source, sample_rate
    )
    return process


def _log_ffmpeg_stderr(process: subprocess.Popen[bytes], title: str) -> None:
    stderr = process.stderr
    if stderr is None:
        return
    try:
        for line in stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                logger.warning("ffmpeg [%s]: %s", title[:40], text[:200])
    except (ValueError, OSError):
        pass
