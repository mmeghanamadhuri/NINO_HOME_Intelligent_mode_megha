"""NiNO Intelligent Mode + Coding Agent — versioned strength scorecard metadata and report."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SERVER_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ScenarioRow:
    number: int
    name: str
    status: str  # PASS | PARTIAL | NOT_TESTED
    evidence_level: str  # unit-mocked | unit | integration | live-hardware
    evidence: str
    live_gap: str = ""


# Scenario metadata — single source of truth for scorecard + tests.
SCENARIO_ROWS: tuple[ScenarioRow, ...] = (
    ScenarioRow(
        1,
        "False alarm discrimination (empathetic LLM reply)",
        "PASS",
        "unit",
        "soak_valid_reply auto-resolves; not classified as code bug",
    ),
    ScenarioRow(
        2,
        "No wasted cycles on unfixable bug",
        "PASS",
        "unit",
        "Logic bug escalates immediately with 0 fix attempts; file named",
    ),
    ScenarioRow(
        3,
        "Compound failure ordering (Ollama + bot offline)",
        "PASS",
        "unit-mocked",
        "LLM recovery chain isolated from bot discovery",
        live_gap="Full live dual-failure (kill Ollama + unplug bot) not automated",
    ),
    ScenarioRow(
        4,
        "Never interrupt live user",
        "PASS",
        "unit-mocked",
        "Fix skipped when voice_active=True; soak defers during live session",
        live_gap="Mid-conversation camera drop on hardware not automated",
    ),
    ScenarioRow(
        5,
        "Self-contained repair (WAV too large)",
        "PASS",
        "unit",
        "wav_auto_split pattern; no developer email; not a code bug",
    ),
    ScenarioRow(
        6,
        "Accurate diagnosis when can't fix",
        "PASS",
        "unit",
        "Email names files; developer fix required; never Auto-fixed or Resolved",
    ),
)

CODING_AGENT_ROWS: tuple[dict[str, str], ...] = (
    {
        "capability": "Pre-validate old_code before emailing developer",
        "status": "PASS",
        "evidence_level": "unit",
    },
    {
        "capability": "Gather logs + source context for LLM",
        "status": "PASS",
        "evidence_level": "unit",
    },
    {
        "capability": "Parallel background worker (non-blocking)",
        "status": "PASS",
        "evidence_level": "unit-mocked",
    },
    {
        "capability": "3-step LLM reasoning pipeline",
        "status": "PASS",
        "evidence_level": "unit-mocked",
    },
    {
        "capability": "Real-world fix accuracy on live incidents",
        "status": "NOT_TESTED",
        "evidence_level": "live",
        "live_gap": "Requires Ollama coding model + live code-bug incidents — next validation phase",
    },
)

TEST_SUITE_FOOTNOTE = (
    "Automated checks across smoke, e2e, agent remediation, code-bug analysis, "
    "coding-agent pipeline, and the 6 scenario classes in test_intelligent_strength_validation.py"
)

FIX_QUALITY_ONE_LINER = (
    "The reasoning pipeline is validated end-to-end; we have not yet scored real-world "
    "fix accuracy because that requires live incidents with a dedicated coding model "
    "installed — that is the next validation phase."
)


def _git_commit_short() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_SERVER_ROOT.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def build_scorecard(*, test_passed: int | None = None, test_total: int | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "generated_date": now.strftime("%Y-%m-%d"),
        "commit": _git_commit_short(),
        "test_suite_footnote": TEST_SUITE_FOOTNOTE,
        "test_counts": {"passed": test_passed, "total": test_total},
        "intelligent_mode": {
            "summary": "Strong and well-tested (automated evidence)",
            "scenarios": [row.__dict__ for row in SCENARIO_ROWS],
        },
        "coding_agent": {
            "summary": "Architecture proven; output quality unbenchmarked on live bugs",
            "fix_quality_one_liner": FIX_QUALITY_ONE_LINER,
            "capabilities": list(CODING_AGENT_ROWS),
        },
        "honest_boundary": (
            "Intelligent Mode (detection, recovery, escalation) and Coding Agent "
            "(LLM fix proposals) are separate systems. Do not merge their pass rates "
            "when asked whether the LLM writes good fixes."
        ),
    }


def format_scorecard_text(card: dict[str, Any]) -> str:
    lines = [
        "NiNO STRENGTH SCORECARD",
        f"as of {card['generated_date']} · commit {card['commit']}",
        "=" * 60,
        "",
        "INTELLIGENT MODE — detection, recovery, escalation",
        f"  {card['intelligent_mode']['summary']}",
        "",
    ]
    if card["test_counts"]["total"]:
        lines.append(
            f"  Automated checks: {card['test_counts']['passed']}/{card['test_counts']['total']} passed"
        )
        lines.append(f"  ({card['test_suite_footnote']})")
        lines.append("")

    lines.append(f"  {'#':<3} {'Status':<8} {'Level':<14} Scenario")
    lines.append("  " + "-" * 56)
    for row in card["intelligent_mode"]["scenarios"]:
        level = row["evidence_level"]
        gap = f" · gap: {row['live_gap']}" if row.get("live_gap") else ""
        lines.append(
            f"  {row['number']:<3} {row['status']:<8} {level:<14} {row['name']}{gap}"
        )

    lines.extend(
        [
            "",
            "CODING AGENT — LLM fix proposals (separate from Intelligent Mode above)",
            f"  {card['coding_agent']['summary']}",
            "",
            f"  {'Status':<12} {'Level':<14} Capability",
            "  " + "-" * 56,
        ]
    )
    for row in card["coding_agent"]["capabilities"]:
        gap = f" · {row.get('live_gap', '')}" if row.get("live_gap") else ""
        lines.append(
            f"  {row['status']:<12} {row.get('evidence_level', ''):<14} {row['capability']}{gap}"
        )

    lines.extend(
        [
            "",
            "IF ASKED: Does the AI actually fix bugs?",
            f"  {card['coding_agent']['fix_quality_one_liner']}",
            "",
            "BOUNDARY",
            f"  {card['honest_boundary']}",
        ]
    )
    return "\n".join(lines)


def print_scorecard(*, test_passed: int | None = None, test_total: int | None = None) -> dict[str, Any]:
    card = build_scorecard(test_passed=test_passed, test_total=test_total)
    print(format_scorecard_text(card))
    return card


def save_scorecard_json(path: Path, *, test_passed: int | None = None, test_total: int | None = None) -> Path:
    card = build_scorecard(test_passed=test_passed, test_total=test_total)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    print_scorecard()
