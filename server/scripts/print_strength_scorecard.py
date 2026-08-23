#!/usr/bin/env python3
"""Run strength validation tests and print a versioned scorecard report.

Usage:
  python3 scripts/print_strength_scorecard.py           # run tests + print report
  python3 scripts/print_strength_scorecard.py --json  # also save server/data/strength_scorecard.json
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="NiNO strength scorecard")
    parser.add_argument("--json", action="store_true", help="Save JSON to server/data/strength_scorecard.json")
    parser.add_argument("--skip-tests", action="store_true", help="Print scorecard without running tests")
    args = parser.parse_args()

    passed = total = None
    if not args.skip_tests:
        test_modules = [
            "test_intelligent_strength_validation",
            "test_intelligent_coding_agent",
            "test_intelligent_coding_agent_worker",
            "test_intelligent_agent_remediation",
            "test_intelligent_code_bug",
            "test_intelligent_mode_agents",
        ]
        cmd = [sys.executable, "-m", "unittest", *test_modules, "-v"]
        result = subprocess.run(cmd, cwd=str(SERVER_DIR))
        # Count from unittest output is approximate; re-run quiet for count
        count_cmd = [sys.executable, "-m", "unittest"] + test_modules
        count_result = subprocess.run(
            count_cmd, cwd=str(SERVER_DIR), capture_output=True, text=True
        )
        # Parse "Ran N tests" from stderr/stdout
        import re

        for line in (count_result.stderr + count_result.stdout).splitlines():
            m = re.search(r"Ran (\d+) test", line)
            if m:
                total = int(m.group(1))
                break
        passed = total if result.returncode == 0 else (total or 0) - 1

    sys.path.insert(0, str(SERVER_DIR))
    from intelligent_mode.strength_scorecard import print_scorecard, save_scorecard_json

    card = print_scorecard(test_passed=passed, test_total=total)

    if args.json:
        out = save_scorecard_json(
            SERVER_DIR / "data" / "strength_scorecard.json",
            test_passed=passed,
            test_total=total,
        )
        print(f"\nSaved: {out}")

    return 0 if (passed is None or passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
