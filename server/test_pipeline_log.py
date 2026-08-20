"""Pipeline log includes stage latency and a running total."""

from __future__ import annotations

import logging
import os
import time
import unittest
from unittest.mock import patch

from pipeline_log import (
    PipelineFormatter,
    UvicornPollFilter,
    begin_pipeline,
    format_pipeline_line,
    pipeline_log,
    uvicorn_log_config,
)


class PipelineLogTests(unittest.TestCase):
    def test_stage_and_total_are_on_the_same_line(self) -> None:
        begin_pipeline(device_id="30eda0e34fc4", turn=3, session="wake", t0=time.perf_counter())
        with self.assertLogs("nino.pipeline", level="INFO") as captured:
            pipeline_log("ASR", "DONE", text="what time is it", stage_s=0.412)

        line = captured.records[-1].getMessage()
        self.assertIn("device=30eda0e34fc4", line)
        self.assertIn("turn=3", line)
        self.assertIn("ASR DONE", line)
        self.assertIn("stage=0.412s", line)
        self.assertIn("total=", line)
        self.assertIn("what time is it", line)

    def test_status_poll_access_logs_are_filtered(self) -> None:
        filt = UvicornPollFilter()
        noisy = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            '127.0.0.1:1 - "GET /api/status?device_id=default HTTP/1.1" 200',
            (),
            None,
        )
        useful = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            '192.168.1.20:2 - "GET /ws/voice HTTP/1.1" 101',
            (),
            None,
        )
        self.assertFalse(filt.filter(noisy))
        self.assertTrue(filt.filter(useful))

    def test_explicit_device_overrides_thread_context(self) -> None:
        begin_pipeline(device_id="a", turn=1, t0=time.perf_counter())
        with self.assertLogs("nino.pipeline", level="INFO") as captured:
            pipeline_log("DEVICE", "CONNECT", device_id="b", turn=9)

        line = captured.records[-1].getMessage()
        self.assertIn("device=b", line)
        self.assertIn("turn=9", line)

    def test_uvicorn_log_config_prints_app_info_logs(self) -> None:
        config = uvicorn_log_config()
        self.assertFalse(config["disable_existing_loggers"])
        self.assertEqual(config["loggers"][""]["level"], "INFO")
        self.assertEqual(config["loggers"]["nino.pipeline"]["level"], "INFO")
        self.assertEqual(config["loggers"]["nino.pipeline"]["handlers"], ["nino"])
        self.assertEqual(config["formatters"]["nino"]["()"], "pipeline_log.PipelineFormatter")

    def test_plain_columns_stay_grepable(self) -> None:
        line = format_pipeline_line(
            device="30eda0e34fc4",
            turn=3,
            kind="ASR",
            event="DONE",
            detail="text='what time is it'",
            stage="0.412s",
            total="1.280s",
            use_colors=False,
        )
        self.assertTrue(line.startswith("NINO | "))
        self.assertIn("30eda0e34fc4", line)
        self.assertIn("t3", line)
        self.assertIn("ASR", line)
        self.assertIn("DONE", line)
        self.assertIn("0.412s", line)
        self.assertIn("1.280s", line)
        self.assertNotIn("\033[", line)

    def test_colors_mark_kind_event_and_http_status(self) -> None:
        line = format_pipeline_line(
            device="30eda0e34fc4",
            turn=1,
            kind="ASR",
            event="DONE",
            detail="status=200 text='hi'",
            stage="0.412s",
            total="1.280s",
            stage_s=0.412,
            total_s=1.280,
            status=200,
            use_colors=True,
        )
        self.assertIn("\033[95m", line)  # ASR bright magenta
        self.assertIn("\033[92m", line)  # DONE / HTTP 200 green
        self.assertIn("\033[91m", line)  # total >= 1s red
        self.assertTrue(line.endswith("\033[0m") or "\033[0m" in line)

    def test_formatter_uses_nino_extras(self) -> None:
        record = logging.LogRecord(
            "nino.pipeline",
            logging.INFO,
            "",
            0,
            "NINO | device=30eda0e34fc4 turn=3 | ASR DONE | text='hi' | stage=0.100s total=0.200s",
            (),
            None,
        )
        record.nino_kind = "ASR"
        record.nino_event = "DONE"
        record.nino_device = "30eda0e34fc4"
        record.nino_turn = 3
        record.nino_detail = "text='hi'"
        record.nino_stage = "0.100s"
        record.nino_total = "0.200s"
        formatted = PipelineFormatter(use_colors=False).format(record)
        self.assertIn("30eda0e34fc4", formatted)
        self.assertIn("ASR", formatted)
        self.assertIn("DONE", formatted)
        self.assertNotIn("\033[", formatted)

    def test_nino_no_color_env_disables_ansi(self) -> None:
        from pipeline_log import colors_enabled

        with patch.dict(os.environ, {"NO_COLOR": "1", "FORCE_COLOR": "0"}):
            os.environ.pop("NINO_NO_COLOR", None)
            self.assertTrue(colors_enabled(None))
        with patch.dict(os.environ, {"NINO_NO_COLOR": "1"}):
            self.assertFalse(colors_enabled(None))
            self.assertFalse(colors_enabled(True))


if __name__ == "__main__":
    unittest.main()
