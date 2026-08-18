"""Tests for robot-speaker music voice commands and stream framing."""

from __future__ import annotations

import base64
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import music_voice
from music_service import MusicNoDeviceError, wav_stream_header
from music_source import (
    MusicNotConfiguredError,
    MusicNotFoundError,
    MusicUnavailableError,
    Track,
)
from music_voice import handle_music_voice, parse_music_command


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("play despacito", ("play", "despacito")),
        ("Play Believer", ("play", "Believer")),
        ("hey nino play shape of you by ed sheeran", ("play", "shape of you by ed sheeran")),
        ("please play the song blinding lights for me", ("play", "blinding lights")),
        ("play finding her on spotify", ("play", "finding her")),
        ("start playing levitating", ("play", "levitating")),
        ("play a song kalyani", ("play", "kalyani")),
        ("play a song", ("play", "")),
        ("play some music", ("play", "")),
        ("play music", ("play", "")),
        ("play me a song", ("play", "")),
        ("piyokalyani", ("play", "kalyani")),
        ("pause", ("stop", "")),
        ("stop the music", ("stop", "")),
        ("stop playing", ("stop", "")),
        ("turn off the music", ("stop", "")),
        ("shut up", ("stop", "")),
        ("shutup", ("stop", "")),
        ("shupupo", ("stop", "")),
        ("shutut", ("stop", "")),
        ("be quiet", ("stop", "")),
        ("quiet", ("stop", "")),
        ("that's enough", ("stop", "")),
        ("stop it", ("stop", "")),
        ("mute", ("stop", "")),
        ("what song is playing", ("now_playing", "")),
    ],
)
def test_parse_recognizes_music_commands(utterance, expected):
    assert parse_music_command(utterance) == expected


@pytest.mark.parametrize(
    "utterance",
    [
        "",
        "what is the weather today",
        "tell me a joke",
        "set an alarm for seven am",
        "five plus three",
        "turn the volume up",
        "start the alarm at seven am",
        "turn on the lights",
        "play a game",
        "play chess with me",
    ],
)
def test_parse_ignores_non_music_utterances(utterance):
    assert parse_music_command(utterance) is None


def test_wav_stream_header_is_valid_and_open_ended():
    header = wav_stream_header(32000)
    assert len(header) == 44
    assert header[:4] == b"RIFF"
    assert header[8:12] == b"WAVE"
    fmt_size, audio_fmt, channels, rate, byte_rate, block_align, bits = struct.unpack(
        "<IHHIIHH", header[16:36]
    )
    assert (fmt_size, audio_fmt, channels, bits) == (16, 1, 1, 16)
    assert rate == 32000
    assert byte_rate == 32000 * 2
    assert block_align == 2
    # Length is unknown up front, so both size fields are maxed out.
    assert struct.unpack("<I", header[4:8])[0] == 0xFFFFFFFF
    assert struct.unpack("<I", header[40:44])[0] == 0xFFFFFFFF


class _FakeSession:
    def __init__(self, track):
        self.track = track


class _FakeService:
    def __init__(self, *, playing=False, device_id=""):
        key = device_id or "default"
        self.sessions: dict[str, _FakeSession] = {}
        self.last: dict[str, Track] = {}
        self.calls: list[str] = []
        if playing:
            self.sessions[key] = _FakeSession(_TRACK)
            self.last[key] = _TRACK

    def play(self, device_id, query):
        key = device_id or "default"
        self.calls.append(f"play:{key}:{query}")
        self.sessions[key] = _FakeSession(_TRACK)
        self.last[key] = _TRACK
        return _TRACK

    def stop(self, device_id):
        key = device_id or "default"
        self.calls.append(f"stop:{key}")
        return self.sessions.pop(key, None) is not None

    def current(self, device_id):
        return self.sessions.get(device_id or "default")

    def is_playing(self, device_id):
        return self.current(device_id) is not None

    def last_track(self, device_id):
        return self.last.get(device_id or "default")


_TRACK = Track(
    title="Believer",
    artist="Imagine Dragons",
    duration_seconds=204,
    stream_url="https://example.test/audio",
    page_url="https://example.test/watch",
)


@pytest.fixture
def fake_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("music_service.get_music_service", lambda: service)
    return service


def test_play_reports_the_track(fake_service):
    result = handle_music_voice("play believer")
    assert result.handled
    assert result.reply_path == "music_play"
    assert result.reply == "Playing Believer by Imagine Dragons."
    assert fake_service.calls == ["play:default:believer"]


def test_stop_after_play(fake_service):
    handle_music_voice("play believer")
    result = handle_music_voice("stop the music")
    assert result.handled
    assert result.reply_path == "music_stop"
    assert result.reply == "Okay, stopping the music."


def test_stop_with_nothing_playing_says_so(fake_service):
    result = handle_music_voice("stop the music")
    assert result.handled
    assert result.reply == "Nothing is playing on this speaker."


def test_bare_stop_is_ignored_when_nothing_is_playing(fake_service):
    assert handle_music_voice("stop").handled is False
    assert handle_music_voice("shut up").handled is False
    assert handle_music_voice("shupupo").handled is False
    assert fake_service.calls == []


def test_bare_stop_stops_while_music_plays(monkeypatch):
    service = _FakeService(playing=True)
    monkeypatch.setattr("music_service.get_music_service", lambda: service)
    result = handle_music_voice("stop")
    assert result.handled and result.reply_path == "music_stop"
    assert result.reply == "Okay, stopping the music."


@pytest.mark.parametrize(
    "utterance",
    [
        "shut up",
        "shutup",
        "shupupo",
        "shupupo stop",
        "shutut",
        "quiet",
        "be quiet",
        "that's enough",
        "stop it",
        "nino stop",
        "please stop",
    ],
)
def test_garbled_stop_claims_the_turn_while_music_plays(monkeypatch, utterance):
    service = _FakeService(playing=True)
    monkeypatch.setattr("music_service.get_music_service", lambda: service)
    result = handle_music_voice(utterance)
    assert result.handled
    assert result.reply_path == "music_stop"
    assert result.reply == "Okay, stopping the music."
    assert service.calls == ["stop:default"]


def test_now_playing_names_the_track(monkeypatch):
    service = _FakeService(playing=True)
    monkeypatch.setattr("music_service.get_music_service", lambda: service)
    result = handle_music_voice("what song is playing")
    assert result.reply == "This is Believer by Imagine Dragons."


@pytest.mark.parametrize(
    ("error", "expected_path"),
    [
        (MusicNotFoundError("nope"), "music_not_found"),
        (MusicNotConfiguredError("no ffmpeg"), "music_unavailable"),
        (MusicUnavailableError("network"), "music_unavailable"),
        (MusicNoDeviceError("unreachable"), "music_no_device"),
    ],
)
def test_failures_become_spoken_replies(monkeypatch, error, expected_path):
    service = _FakeService()

    def _raise(_device_id, _query):
        raise error

    service.play = _raise
    monkeypatch.setattr("music_service.get_music_service", lambda: service)

    result = handle_music_voice("play believer")
    assert result.handled
    assert result.reply_path == expected_path
    assert result.reply


def test_track_spoken_avoids_repeating_artist_in_title():
    assert _TRACK.spoken() == "Believer by Imagine Dragons"
    dupe = Track("Adele - Hello", "Adele", 0, "u", "p")
    assert dupe.spoken() == "Adele - Hello"
    # Channel names arrive unspaced ("ImagineDragons") but mean the same thing.
    unspaced = Track("Imagine Dragons - Believer", "ImagineDragons", 0, "u", "p")
    assert unspaced.spoken() == "Imagine Dragons - Believer"


def test_saavn_url_decrypts_back_to_the_cdn_url():
    from Crypto.Cipher import DES

    from music_source import decrypt_saavn_url

    plain = "https://aac.saavncdn.com/248/46944eb7b4b31f5b0abf5eb2e1be2d2a_96.mp4"
    pad = 8 - (len(plain) % 8)
    padded = (plain + chr(pad) * pad).encode()
    encrypted = base64.b64encode(
        DES.new(b"38346591", DES.MODE_ECB).encrypt(padded)
    ).decode()

    assert decrypt_saavn_url(encrypted) == plain


def test_saavn_artist_prefers_the_primary_artist_map():
    from music_source import _saavn_artist

    more_info = {
        "artistMap": {"primary_artists": [{"name": "Imagine Dragons"}]},
        "primary_artists": "Someone Else",
    }
    assert _saavn_artist(more_info) == "Imagine Dragons"
    assert _saavn_artist({"primary_artists": "Ed Sheeran, Justin"}) == "Ed Sheeran"
    assert _saavn_artist({}) == ""


def test_saavn_artist_unescapes_html_entities():
    from music_source import _saavn_artist

    more_info = {"artistMap": {"primary_artists": [{"name": "Vishal &amp; Shekhar"}]}}
    assert _saavn_artist(more_info) == "Vishal & Shekhar"


def test_mentions_music_helper():
    assert music_voice.mentions_music("stop the music")
    assert not music_voice.mentions_music("stop")


def test_play_a_song_uses_last_track_for_that_device(fake_service):
    handle_music_voice("play believer", device_id="nino-home-147")
    result = handle_music_voice("play a song", device_id="nino-home-147")
    assert result.handled
    assert result.reply_path == "music_play"
    assert result.reply == "Playing Believer by Imagine Dragons."
    assert fake_service.calls[-1] == "play:nino-home-147:Believer by Imagine Dragons"


def test_stop_is_per_device(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("music_service.get_music_service", lambda: service)
    handle_music_voice("play believer", device_id="nino-home-147")
    handle_music_voice("play believer", device_id="ninofarfromhome")

    idle = handle_music_voice("shut up", device_id="someone-else")
    assert idle.handled is False

    home = handle_music_voice("shut up", device_id="nino-home-147")
    assert home.handled
    assert home.reply == "Okay, stopping the music."
    assert service.is_playing("nino-home-147") is False
    assert service.is_playing("ninofarfromhome") is True


def test_music_replies_do_not_continue_the_conversation():
    from voice_service import CONTINUE_LISTEN_REPLY_PATHS, should_continue_listen_after_reply

    for path in (
        "music_play",
        "music_stop",
        "music_now_playing",
        "music_not_found",
        "music_unavailable",
        "music_no_device",
    ):
        assert path not in CONTINUE_LISTEN_REPLY_PATHS
        assert should_continue_listen_after_reply(path, "play kalyani") is False
