"""Incident store prune and archive tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from intelligent_mode.incidents import Incident, IncidentStore


class IncidentStorePruneTests(unittest.TestCase):
    def test_prune_removes_benign_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "incidents.json"
            store = IncidentStore(path=path)
            store.open_incident(
                Incident(
                    device_id="b0a6048addd4",
                    display_name="Robot",
                    subsystem="voice",
                    severity="warning",
                    tier=1,
                    error=(
                        "HTTPConnectionPool(host='127.0.0.1', port=11434): Max retries exceeded "
                        "with url: /api/generate"
                    ),
                    signature="sig-benign",
                    status="resolved",
                )
            )
            store.open_incident(
                Incident(
                    device_id="b0a6048addd4",
                    display_name="Robot",
                    subsystem="camera",
                    severity="critical",
                    tier=0,
                    error="HTTP Error 503: Service Unavailable",
                    signature="sig-camera",
                    status="resolved",
                )
            )
            stats = store.prune_resolved(keep_recent=50)
            self.assertEqual(stats["removed"], 1)
            remaining = store.list_incidents(limit=50)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].subsystem, "camera")
            archive = path.with_name("intelligent_incidents_archive.json")
            self.assertTrue(archive.is_file())


if __name__ == "__main__":
    unittest.main()
