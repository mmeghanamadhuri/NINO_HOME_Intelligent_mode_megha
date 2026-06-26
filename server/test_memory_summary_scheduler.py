"""Tests for Phase C daily summary scheduler helpers."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from memory_service import (
    parse_summary_scheduler_time,
    seconds_until_local_time,
    yesterday_local_date,
)


class SummarySchedulerTests(unittest.TestCase):
    def test_parse_scheduler_time(self) -> None:
        self.assertEqual(parse_summary_scheduler_time("00:05"), (0, 5))
        self.assertEqual(parse_summary_scheduler_time("23:59"), (23, 59))

    def test_parse_scheduler_time_invalid(self) -> None:
        with self.assertRaises(ValueError):
            parse_summary_scheduler_time("25:00")

    def test_seconds_until_later_today(self) -> None:
        now = datetime(2026, 6, 26, 22, 0, 0)
        wait = seconds_until_local_time(23, 30, now=now)
        self.assertEqual(wait, 90 * 60)

    def test_seconds_until_tomorrow(self) -> None:
        now = datetime(2026, 6, 26, 23, 30, 0)
        wait = seconds_until_local_time(0, 5, now=now)
        self.assertEqual(wait, 35 * 60)

    def test_yesterday_local_date(self) -> None:
        now = datetime(2026, 6, 26, 1, 0, 0)
        self.assertEqual(yesterday_local_date(now=now).isoformat(), "2026-06-25")


if __name__ == "__main__":
    unittest.main()
