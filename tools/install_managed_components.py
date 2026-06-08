#!/usr/bin/env python3
"""Install managed_components on Windows when the project path exceeds MAX_PATH."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is required. Use the ESP-IDF Python environment.", file=sys.stderr)
    sys.exit(1)


CACHE_ROOT = Path(os.environ.get("IDF_COMPONENT_CACHE", Path.home() / "AppData/Local/Espressif/ComponentManager/Cache"))
SERVICE_CACHE = CACHE_ROOT / "service_d92d8f1e"
HASH_FILE = ".component_hash"


def win_long(path: Path) -> str:
    absolute = str(path.resolve())
    if absolute.startswith("\\\\?\\"):
        return absolute
    return "\\\\?\\" + absolute


def build_name(name: str) -> str:
    return "__".join(name.split("/"))


def cache_dir_name(name: str, version: str, component_hash: str) -> str:
    return f"{build_name(name)}_{version}_{component_hash[:8]}"


def patch_esp_hosted_deferred_init(managed_dir: Path) -> None:
    """Skip constructor-time esp_hosted_init(); main calls it from app_main."""
    init_c = (
        managed_dir
        / "espressif__esp_hosted"
        / "host"
        / "port"
        / "esp"
        / "freertos"
        / "src"
        / "port_esp_hosted_host_init.c"
    )
    if not init_c.exists():
        return

    text = init_c.read_text(encoding="utf-8")
    needle = "\tESP_ERROR_CHECK(esp_hosted_init());"
    if needle not in text:
        if "init deferred to app_main" in text:
            return
        print(f"Warning: could not patch {init_c} (unexpected content)", file=sys.stderr)
        return

    text = text.replace(
        needle,
        "\t/* patched: esp_hosted_init() runs from app_main */\n",
        1,
    )
    text = text.replace(
        'ESP_LOGI(TAG, "ESP Hosted : Host chip_ip[%d]", CONFIG_IDF_FIRMWARE_CHIP_ID);',
        'ESP_LOGI(TAG, "ESP Hosted : Host chip_ip[%d] (init deferred to app_main)",\n'
        "\t\t CONFIG_IDF_FIRMWARE_CHIP_ID);",
        1,
    )
    init_c.write_text(text, encoding="utf-8")
    print("Patched esp_hosted for deferred init (app_main)")


def copy_component(src: Path, dst: Path, component_hash: str) -> None:
    long_src = win_long(src)
    long_dst = win_long(dst)

    if os.path.exists(long_dst):
        shutil.rmtree(long_dst)

    shutil.copytree(long_src, long_dst)

    hash_path = win_long(dst / HASH_FILE)
    with open(hash_path, "w", encoding="utf-8") as handle:
        handle.write(f"{component_hash}\n")


def main() -> int:
    project_dir = Path(__file__).resolve().parents[1]
    lock_path = project_dir / "dependencies.lock"
    managed_dir = project_dir / "managed_components"

    if not lock_path.exists():
        print(f"Missing {lock_path}", file=sys.stderr)
        return 1

    if not SERVICE_CACHE.exists():
        print(f"Missing component cache at {SERVICE_CACHE}", file=sys.stderr)
        print("Run idf.py build once (or idf.py reconfigure) to populate the cache.", file=sys.stderr)
        return 1

    with lock_path.open(encoding="utf-8") as handle:
        lock = yaml.safe_load(handle)

    managed_dir.mkdir(parents=True, exist_ok=True)

    installed = 0
    skipped = 0
    missing = []

    for name, info in lock.get("dependencies", {}).items():
        if info.get("source", {}).get("type") != "service":
            continue

        component_hash = info.get("component_hash")
        version = str(info.get("version", ""))
        if not component_hash or not version:
            continue

        cache_name = cache_dir_name(name, version, component_hash)
        src = SERVICE_CACHE / cache_name
        dst = managed_dir / build_name(name)

        if not src.exists():
            missing.append(name)
            continue

        print(f"Installing {name} ({version})")
        copy_component(src, dst, component_hash)
        installed += 1

    print(f"Installed {installed} component(s).")
    patch_esp_hosted_deferred_init(managed_dir)
    if missing:
        print("Missing from cache (run idf.py reconfigure to fetch):", ", ".join(missing), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
