from __future__ import annotations

from pathlib import Path

import aux_recordings


def test_save_aux_wav_writes_under_device_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUX_RECORDINGS_DIR", str(tmp_path))
    wav = b"RIFF" + b"\x00" * 40
    path = aux_recordings.save_aux_wav(
        wav, device_id="30eda0e34fc4", turn=3, energy=122, session="wake", source="aux"
    )
    assert path.is_file()
    assert path.read_bytes() == wav
    assert path.parent.name == "30eda0e34fc4"
    assert path.name.endswith("_t3_e122_wake_aux.wav")
    listed = aux_recordings.list_aux_recordings()
    assert listed[0]["device_id"] == "30eda0e34fc4"
    assert listed[0]["bytes"] == len(wav)


def test_safe_device_id_strips_path_parts() -> None:
    assert aux_recordings.safe_device_id("../etc/passwd") == "etc-passwd"
    assert aux_recordings.safe_device_id("") == "unknown"
