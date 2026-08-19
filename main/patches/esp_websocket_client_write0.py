#!/usr/bin/env python3
"""Keep the stream socket up on write-0 / poll timeout.

esp_websocket_client 1.7.0 aborts the connection when send_bin gets a 0-byte
write (TCP not writable for the send timeout). That raced VAD EOS, closed the
voice WS mid-conversation, and turned UVC off. Treat a 0-byte write as a
short write so firmware can pause TX and still receive EOS/WAV.
"""
from __future__ import annotations

import pathlib
import sys

MARKER = "NINO_WRITE0_NOABORT"
OLD = """        if (wlen < 0 || (wlen == 0 && need_write != 0)) {
            ret = wlen;
            esp_websocket_free_buf(client, true);
"""
NEW = """        /* NINO_WRITE0_NOABORT: poll timeout / short write must not tear down
         * the socket — pause TX and keep RX for EOS/WAV. */
        if (wlen == 0 && need_write != 0) {
            ret = 0;
            esp_websocket_free_buf(client, true);
            goto unlock_and_return;
        }
        if (wlen < 0) {
            ret = wlen;
            esp_websocket_free_buf(client, true);
"""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: esp_websocket_client_write0.py <esp_websocket_client.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return 0
    if OLD not in text:
        print(f"write-0 patch: pattern not found in {path}", file=sys.stderr)
        return 1
    path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"write-0 patch applied: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
