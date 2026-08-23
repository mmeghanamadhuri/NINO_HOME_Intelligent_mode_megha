#!/usr/bin/env bash
# Run intelligent mode unit tests (no live robots required).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest test_intelligent_mode.py test_intelligent_mode_smoke.py test_intelligent_mode_agents.py test_intelligent_e2e.py test_intelligent_reporter.py test_intelligent_dashboard.py test_intelligent_debugger.py test_intelligent_strength_validation.py test_intelligent_coding_agent.py test_intelligent_coding_agent_worker.py -v "$@"
echo ""
echo "Strength scorecard (dated, versioned):"
python3 scripts/print_strength_scorecard.py --skip-tests --json
