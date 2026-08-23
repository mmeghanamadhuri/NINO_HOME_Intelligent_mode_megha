"""NiNO Coding Agent — parallel smart bug fixing via Ollama with developer approval."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intelligent_mode.config import IntelligentConfig, load_config
from intelligent_mode.emailer import email_configured, send_raw_email
from intelligent_mode.incidents import Incident

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SERVER_ROOT = Path(__file__).resolve().parent.parent
_PROPOSALS_PATH = _SERVER_ROOT / "data" / "coding_agent_proposals.json"
_BACKUP_DIR = _SERVER_ROOT / "data" / "coding_agent_backups"
_LOG_PATH = _SERVER_ROOT / "data" / "nino_server.log"
_LATENCY_PATH = _SERVER_ROOT / "data" / "latency_log.json"
_LOCK = threading.Lock()

# Single approved coding model for NiNO bug-fix proposals (no fallbacks).
DEFAULT_CODING_MODEL = "qwen2.5-coder:32b"
CODING_MODEL_RANK: list[tuple[str, str]] = [
    (
        DEFAULT_CODING_MODEL,
        "Qwen 2.5 Coder 32B — NiNO coding agent model (required)",
    ),
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CodeChange:
    file_path: str
    start_line: int = 0
    end_line: int = 0
    old_code: str = ""
    new_code: str = ""
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CodeChange:
        fields = cls.__dataclass_fields__
        return cls(**{k: raw[k] for k in fields if k in raw})


@dataclass
class FixProposal:
    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    incident_id: str = ""
    device_id: str = ""
    display_name: str = ""
    subsystem: str = ""
    status: str = "pending"  # pending | approved | rejected | applied | failed
    fix_type: str = "server"  # server | firmware | both
    model_used: str = ""
    bug_summary: str = ""
    root_cause: str = ""
    error: str = ""
    changes: list[CodeChange] = field(default_factory=list)
    manual_steps: list[str] = field(default_factory=list)
    firmware_filename: str = ""
    created_at: str = field(default_factory=_utc_now)
    approved_at: str | None = None
    applied_at: str | None = None
    apply_detail: str = ""
    email_sent: bool = False
    confidence: str = "medium"
    validation_passed: bool = False
    validation_detail: str = ""
    test_results: list[str] = field(default_factory=list)
    analysis_steps: list[str] = field(default_factory=list)
    related_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["changes"] = [c.to_dict() if isinstance(c, CodeChange) else c for c in self.changes]
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FixProposal:
        changes_raw = raw.get("changes") or []
        changes = [
            CodeChange.from_dict(c) if isinstance(c, dict) else c for c in changes_raw
        ]
        fields = cls.__dataclass_fields__
        kwargs = {k: raw[k] for k in fields if k in raw and k != "changes"}
        kwargs["changes"] = changes
        return cls(**kwargs)


def _load_proposals() -> list[FixProposal]:
    if not _PROPOSALS_PATH.is_file():
        return []
    try:
        raw = json.loads(_PROPOSALS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [FixProposal.from_dict(r) for r in raw if isinstance(r, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def _save_proposals(rows: list[FixProposal]) -> None:
    _PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = [r.to_dict() for r in rows]
    _PROPOSALS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def list_proposals(*, status: str | None = None, limit: int = 50) -> list[FixProposal]:
    rows = _load_proposals()
    if status:
        rows = [r for r in rows if r.status == status]
    return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]


def get_proposal(proposal_id: str) -> FixProposal | None:
    return next((r for r in _load_proposals() if r.proposal_id == proposal_id), None)


def _resolve_repo_path(rel_path: str) -> Path | None:
    rel = rel_path.strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    for base in (_REPO_ROOT, _SERVER_ROOT):
        candidate = (base / rel).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _read_snippet(rel_path: str, *, around_line: int | None = None, radius: int = 12) -> str:
    path = _resolve_repo_path(rel_path)
    if path is None:
        alt = _REPO_ROOT / rel_path
        if alt.is_file():
            path = alt
        else:
            return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if around_line is not None and 1 <= around_line <= len(lines):
        start = max(0, around_line - 1 - radius)
        end = min(len(lines), around_line + radius)
        chunk = lines[start:end]
        return "\n".join(f"{start + i + 1:4d}| {line}" for i, line in enumerate(chunk))
    return "\n".join(f"{i + 1:4d}| {line}" for i, line in enumerate(lines[:60]))


def _read_file_chunk(rel_path: str, *, max_lines: int = 120) -> str:
    path = _resolve_repo_path(rel_path)
    if path is None:
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(f"{i + 1:4d}| {line}" for i, line in enumerate(lines[:max_lines]))


def _grep_codebase(keywords: list[str], *, max_files: int = 6, max_hits: int = 3) -> dict[str, list[str]]:
    """Find related source files by keyword — local, no network."""
    if not keywords:
        return {}
    roots = [_SERVER_ROOT, _REPO_ROOT / "main"]
    hits: dict[str, list[str]] = {}
    patterns = [re.compile(re.escape(k), re.IGNORECASE) for k in keywords if k.strip()]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if len(hits) >= max_files:
                break
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix not in {".py", ".c", ".h", ".cpp", ".hpp"}:
                continue
            if any(part in {".venv", "node_modules", "build", "__pycache__"} for part in path.parts):
                continue
            try:
                rel = path.relative_to(_REPO_ROOT).as_posix()
            except ValueError:
                try:
                    rel = path.relative_to(_SERVER_ROOT).as_posix()
                    rel = f"server/{rel}"
                except ValueError:
                    continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            matched: list[str] = []
            for i, line in enumerate(lines, 1):
                if any(p.search(line) for p in patterns):
                    matched.append(f"{i}| {line.strip()[:120]}")
                    if len(matched) >= max_hits:
                        break
            if matched:
                hits[rel] = matched
    return hits


def _recent_log_lines(*, limit: int = 40) -> str:
    if not _LOG_PATH.is_file():
        return ""
    try:
        lines = _LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    relevant = [
        ln
        for ln in lines[-500:]
        if any(tok in ln.lower() for tok in ("error", "exception", "traceback", "failed", "bug"))
    ]
    return "\n".join(relevant[-limit:])


def _recent_latency_rows(device_id: str, *, limit: int = 8) -> str:
    if not _LATENCY_PATH.is_file():
        return ""
    try:
        raw = json.loads(_LATENCY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return ""
        rows = [r for r in raw if isinstance(r, dict)]
        if device_id not in {"", "server"}:
            rows = [
                r
                for r in rows
                if str(r.get("device_id") or "") == device_id
                or str(r.get("device_id") or "").startswith(device_id[:8])
            ]
        out = []
        for row in rows[-limit:]:
            out.append(
                f"event={row.get('event')} path={row.get('reply_path')} "
                f"error={str(row.get('error') or '')[:80]}"
            )
        return "\n".join(out)
    except (OSError, json.JSONDecodeError):
        return ""


def _extract_keywords(incident: Incident, code_bug: dict[str, Any]) -> list[str]:
    text = " ".join(
        [
            str(incident.error or ""),
            str(incident.subsystem or ""),
            str(code_bug.get("bug_summary") or ""),
            str(code_bug.get("likely_cause") or ""),
        ]
    )
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{3,}", text.lower())
    stop = {
        "error", "failed", "failure", "exception", "during", "after", "before",
        "from", "with", "that", "this", "have", "been", "were", "when", "while",
    }
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        if tok in stop or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= 8:
            break
    return out


def gather_rich_context(
    incident: Incident,
    code_bug: dict[str, Any],
    affected: list[str],
) -> dict[str, Any]:
    """Build deep context: snippets, grep hits, logs, latency."""
    snippets: dict[str, str] = {}
    for rel in affected[:5]:
        if rel.endswith("/"):
            continue
        snippets[rel] = _read_file_chunk(rel, max_lines=100)

    keywords = _extract_keywords(incident, code_bug)
    grep_hits = _grep_codebase(keywords)
    for rel in list(grep_hits.keys())[:3]:
        if rel not in snippets:
            snippets[rel] = _read_file_chunk(rel, max_lines=60)

    return {
        "snippets": snippets,
        "grep_hits": grep_hits,
        "log_tail": _recent_log_lines(),
        "latency_rows": _recent_latency_rows(incident.device_id),
        "keywords": keywords,
        "related_files": list(dict.fromkeys(list(snippets.keys()) + list(grep_hits.keys()))),
    }


def _llm_call(prompt: str, model: str, *, num_predict: int = 800, step: str = "") -> str:
    from llm_service import ollama_generate, resolve_ollama_api_url

    timeout = int(os.environ.get("CODING_AGENT_TIMEOUT_S", "120") or "120")
    api_url = resolve_ollama_api_url(model=model)
    if step:
        logger.debug("Coding agent LLM step %s (model=%s)", step, model)
    return ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        num_predict=num_predict,
        temperature=0.08,
        timeout_s=timeout,
    )


def _multi_step_analyze(
    incident: Incident,
    code_bug: dict[str, Any],
    context: dict[str, Any],
    model: str,
) -> tuple[dict[str, Any], list[str]]:
    """Three-step reasoning: diagnose → design → implement."""
    steps: list[str] = []
    snippets = context.get("snippets") or {}
    snippet_text = ""
    for path, body in snippets.items():
        if body:
            snippet_text += f"\n--- {path} ---\n{body[:2000]}\n"

    grep_text = ""
    for path, hits in (context.get("grep_hits") or {}).items():
        grep_text += f"\n{path}: " + " | ".join(hits[:2])

    # Step 1: Root cause
    step1_prompt = (
        "You are a senior NiNO robot debugger. Analyze this production bug.\n"
        "NiNO: ESP32-P4 firmware (main/*.c) + Python FastAPI server (server/*.py).\n"
        "Reply JSON only: {\"root_cause\": \"...\", \"subsystem_fault\": \"server|firmware|both\", "
        "\"key_files\": [\"path1\", \"path2\"], \"reasoning\": \"2-3 sentences\"}\n\n"
        f"Error: {incident.error}\n"
        f"Subsystem: {incident.subsystem}\n"
        f"Device: {incident.device_id}\n"
        f"Prior analysis: {code_bug.get('bug_summary') or ''}\n"
        f"Log errors:\n{context.get('log_tail') or 'none'}\n"
        f"Latency:\n{context.get('latency_rows') or 'none'}\n"
        f"Code grep:\n{grep_text[:1500]}\n"
        f"{snippet_text[:3000]}\n"
    )
    step1 = _parse_llm_json(_llm_call(step1_prompt, model, num_predict=400, step="diagnose")) or {}
    steps.append(f"Diagnose: {step1.get('reasoning') or step1.get('root_cause') or 'done'}")

    key_files = [str(f) for f in (step1.get("key_files") or []) if str(f).strip()]
    design_files = key_files or [str(f) for f in (code_bug.get("affected_files") or []) if str(f).strip()]

    # Step 2: Fix design
    step2_prompt = (
        "Design the minimal fix for this NiNO bug. Reply JSON only:\n"
        "{\"approach\": \"1-2 sentences\", \"fix_type\": \"server|firmware|both\", "
        "\"files_to_change\": [\"path\"], \"risk\": \"low|medium|high\"}\n\n"
        f"Root cause: {step1.get('root_cause') or code_bug.get('likely_cause') or incident.error}\n"
        f"Files: {', '.join(design_files)}\n"
        f"Error: {incident.error}\n"
    )
    step2 = _parse_llm_json(_llm_call(step2_prompt, model, num_predict=300, step="design")) or {}
    steps.append(f"Design: {step2.get('approach') or 'done'}")

    # Enrich snippets for files identified in step 1/2
    for rel in design_files[:4]:
        if rel.endswith("/"):
            continue
        if rel not in snippets:
            snippets[rel] = _read_file_chunk(rel, max_lines=120)
    snippet_text = ""
    for path, body in snippets.items():
        if body:
            snippet_text += f"\n--- {path} ---\n{body[:2500]}\n"

    # Step 3: Exact code changes
    step3_prompt = (
        "Implement the fix. Reply JSON only:\n"
        "{\n"
        '  "bug_summary": "one sentence",\n'
        '  "root_cause": "one sentence",\n'
        '  "fix_type": "server|firmware|both",\n'
        '  "confidence": "high|medium|low",\n'
        '  "changes": [{"file_path": "server/x.py", "start_line": 10, "end_line": 12, '
        '"old_code": "exact original", "new_code": "fixed", "explanation": "why"}],\n'
        '  "manual_steps": ["only if firmware rebuild needed"]\n'
        "}\n\n"
        "Rules: old_code MUST match file exactly. Max 3 changes. Smallest correct fix.\n\n"
        f"Approach: {step2.get('approach') or ''}\n"
        f"Root cause: {step1.get('root_cause') or ''}\n"
        f"Error: {incident.error}\n"
        f"{snippet_text[:6000]}\n"
    )
    step3 = _parse_llm_json(
        _llm_call(
            step3_prompt,
            model,
            num_predict=int(os.environ.get("CODING_AGENT_NUM_PREDICT", "2000") or "2000"),
            step="implement",
        )
    ) or {}
    steps.append(f"Implement: {len(step3.get('changes') or [])} change(s), confidence={step3.get('confidence') or '?'}")

    merged = dict(step3)
    merged.setdefault("root_cause", step1.get("root_cause") or code_bug.get("likely_cause"))
    merged.setdefault("fix_type", step2.get("fix_type") or step1.get("subsystem_fault") or "server")
    merged.setdefault("confidence", "medium")
    return merged, steps


def validate_proposal(proposal: FixProposal) -> tuple[bool, str]:
    """Verify old_code exists in files and changes are safe."""
    if not proposal.changes:
        return False, "No code changes proposed"

    issues: list[str] = []
    for change in proposal.changes:
        path = _resolve_repo_path(change.file_path)
        if path is None:
            issues.append(f"{change.file_path}: file not found")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(f"{change.file_path}: read failed ({exc})")
            continue
        if change.old_code and change.old_code not in content:
            issues.append(f"{change.file_path}: old_code not found in file")
        if not change.new_code.strip():
            issues.append(f"{change.file_path}: empty new_code")

    if issues:
        return False, "; ".join(issues)
    return True, f"Validated {len(proposal.changes)} change(s)"


def run_targeted_tests(proposal: FixProposal) -> list[str]:
    """Run unit tests for affected server modules (dry-run validation)."""
    if proposal.fix_type == "firmware" and not any(
        c.file_path.startswith("server/") for c in proposal.changes
    ):
        return ["Skipped: firmware-only change"]

    test_modules: list[str] = []
    for change in proposal.changes:
        if not change.file_path.startswith("server/") or not change.file_path.endswith(".py"):
            continue
        stem = Path(change.file_path).stem
        candidates = [
            f"test_{stem}.py",
            f"test_intelligent_{stem}.py",
        ]
        if stem == "voice_service":
            candidates.extend(["test_intelligent_code_bug.py", "test_llm_service.py"])
        if stem == "llm_service":
            candidates.append("test_llm_service.py")
        for name in candidates:
            if (_SERVER_ROOT / name).is_file() and name not in test_modules:
                test_modules.append(name)

    if not test_modules:
        return ["No targeted tests found for changed files"]

    results: list[str] = []
    for module in test_modules[:3]:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", module, "-q"],
                cwd=str(_SERVER_ROOT),
                capture_output=True,
                text=True,
                timeout=90,
            )
            status = "PASS" if proc.returncode == 0 else "FAIL"
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else ""
            results.append(f"{module}: {status} {tail[:120]}")
        except subprocess.TimeoutExpired:
            results.append(f"{module}: TIMEOUT")
        except Exception as exc:
            results.append(f"{module}: ERROR {exc}")
    return results


def _configured_coding_model() -> str:
    """Return the coding model name — always qwen2.5-coder:32b unless explicitly overridden."""
    explicit = os.environ.get("CODING_AGENT_MODEL", "").strip()
    if explicit and explicit != DEFAULT_CODING_MODEL:
        logger.warning(
            "CODING_AGENT_MODEL=%s ignored — NiNO coding agent uses %s only",
            explicit,
            DEFAULT_CODING_MODEL,
        )
    return DEFAULT_CODING_MODEL


def list_available_coding_models() -> list[dict[str, Any]]:
    """Return availability of the required coding model."""
    model = _configured_coding_model()
    out: list[dict[str, Any]] = []
    try:
        from llm_service import ollama_is_reachable, ollama_model_available, resolve_ollama_api_url

        if not ollama_is_reachable():
            return [
                {
                    "model": model,
                    "description": CODING_MODEL_RANK[0][1],
                    "available": False,
                    "recommended": True,
                    "required": True,
                    "reason": "Ollama not reachable — run: ollama pull qwen2.5-coder:32b",
                }
            ]

        api_url = resolve_ollama_api_url()
        available = ollama_model_available(model=model, api_url=api_url)
        out.append(
            {
                "model": model,
                "description": CODING_MODEL_RANK[0][1],
                "available": available,
                "recommended": True,
                "required": True,
                "reason": "" if available else "Not installed — run: ollama pull qwen2.5-coder:32b",
            }
        )
    except Exception as exc:
        out.append(
            {
                "model": model,
                "available": False,
                "required": True,
                "reason": str(exc),
            }
        )
    return out


def select_coding_model() -> tuple[str, str]:
    """Return the NiNO coding agent model (qwen2.5-coder:32b only)."""
    model = _configured_coding_model()
    try:
        from llm_service import ollama_is_reachable, ollama_model_available, resolve_ollama_api_url

        if not ollama_is_reachable():
            return model, f"{model} required but Ollama unreachable — install with: ollama pull {model}"

        api_url = resolve_ollama_api_url()
        if ollama_model_available(model=model, api_url=api_url):
            return model, f"NiNO coding agent model: {model}"
        return model, f"{model} required but not installed — run: ollama pull {model}"
    except Exception as exc:
        return model, f"{model} required — model check failed ({exc})"


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    # Strip markdown fences if present
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # Try to find first JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _build_fix_prompt(
    incident: Incident,
    code_bug: dict[str, Any],
    snippets: dict[str, str],
) -> str:
    snippet_text = ""
    for path, body in snippets.items():
        if body:
            snippet_text += f"\n--- {path} ---\n{body}\n"

    affected = code_bug.get("affected_files") or []
    return (
        "You are the NiNO robot coding agent. Analyze this bug and propose a minimal code fix.\n"
        "NiNO stack: ESP32-P4 firmware (C/ESP-IDF in main/), Python FastAPI server (server/).\n\n"
        "Respond with ONLY valid JSON (no markdown):\n"
        "{\n"
        '  "bug_summary": "one sentence",\n'
        '  "root_cause": "one sentence",\n'
        '  "fix_type": "server" | "firmware" | "both",\n'
        '  "changes": [\n'
        "    {\n"
        '      "file_path": "server/voice_service.py",\n'
        '      "start_line": 100,\n'
        '      "end_line": 105,\n'
        '      "old_code": "exact lines to replace",\n'
        '      "new_code": "replacement lines",\n'
        '      "explanation": "why this fixes the bug"\n'
        "    }\n"
        "  ],\n"
        '  "manual_steps": ["Rebuild firmware: idf.py build", "Copy bin to server/firmware/"],\n'
        '  "confidence": "high" | "medium" | "low"\n'
        "}\n\n"
        "Rules:\n"
        "- Propose the smallest correct fix (1-3 file changes max).\n"
        "- old_code must match the file exactly (include enough context to be unique).\n"
        "- For firmware bugs in main/, set fix_type=firmware or both and include manual_steps for rebuild+OTA.\n"
        "- For server Python bugs, set fix_type=server.\n"
        "- If unsure, set confidence=low and explain in manual_steps.\n\n"
        f"Incident ID: {incident.incident_id}\n"
        f"Device: {incident.device_id} ({incident.display_name})\n"
        f"Subsystem: {incident.subsystem}\n"
        f"Error: {incident.error}\n"
        f"Existing analysis: {code_bug.get('bug_summary') or code_bug.get('likely_cause') or ''}\n"
        f"Suggested fix hint: {code_bug.get('suggested_fix') or ''}\n"
        f"Affected files: {', '.join(str(f) for f in affected)}\n"
        f"{snippet_text}\n"
    )


def propose_fix(
    incident: Incident,
    *,
    code_bug: dict[str, Any] | None = None,
    force: bool = False,
) -> FixProposal | None:
    """Generate a structured fix proposal for a code-bug incident."""
    if not _env_bool("CODING_AGENT_ENABLED", False) and not force:
        return None

    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    code_bug = code_bug or (debug.get("code_bug") if isinstance(debug.get("code_bug"), dict) else {})
    if not code_bug.get("is_code_bug"):
        return None

    # Skip if we already have a pending proposal for this incident
    with _LOCK:
        existing = [
            p
            for p in _load_proposals()
            if p.incident_id == incident.incident_id and p.status in {"pending", "approved"}
        ]
        if existing and not force:
            return existing[0]

    affected = [str(f) for f in (code_bug.get("affected_files") or []) if str(f).strip()]
    if not affected:
        subsystem = str(incident.subsystem or "").lower()
        if subsystem in {"voice", "server"}:
            affected = ["server/voice_service.py", "server/llm_service.py"]
        elif subsystem == "camera":
            affected = ["server/camera.py", "main/"]

    model, model_reason = select_coding_model()
    logger.info("Coding agent using model %s (%s)", model, model_reason)

    use_smart = _env_bool("CODING_AGENT_SMART", True)
    analysis_steps: list[str] = []
    related_files: list[str] = []
    parsed: dict[str, Any] | None = None

    try:
        from llm_service import ollama_is_reachable

        if not ollama_is_reachable():
            logger.warning("Coding agent: Ollama not reachable")
            return _fallback_proposal(incident, code_bug, model, "Ollama not reachable")

        context = gather_rich_context(incident, code_bug, affected)
        related_files = list(context.get("related_files") or [])

        if use_smart:
            parsed, analysis_steps = _multi_step_analyze(incident, code_bug, context, model)
        else:
            snippets = context.get("snippets") or {}
            prompt = _build_fix_prompt(incident, code_bug, snippets)
            llm_response = _llm_call(
                prompt,
                model,
                num_predict=int(os.environ.get("CODING_AGENT_NUM_PREDICT", "1500") or "1500"),
                step="single",
            )
            parsed = _parse_llm_json(llm_response)
            analysis_steps = ["Single-step analysis"]
    except Exception as exc:
        logger.warning("Coding agent LLM call failed: %s", exc)
        return _fallback_proposal(incident, code_bug, model, str(exc))

    if not parsed:
        logger.warning("Coding agent: failed to parse LLM JSON response")
        return _fallback_proposal(incident, code_bug, model, "LLM returned invalid JSON")

    changes: list[CodeChange] = []
    for raw_change in parsed.get("changes") or []:
        if not isinstance(raw_change, dict):
            continue
        fp = str(raw_change.get("file_path") or "").strip()
        if not fp:
            continue
        changes.append(
            CodeChange(
                file_path=fp,
                start_line=int(raw_change.get("start_line") or 0),
                end_line=int(raw_change.get("end_line") or 0),
                old_code=str(raw_change.get("old_code") or ""),
                new_code=str(raw_change.get("new_code") or ""),
                explanation=str(raw_change.get("explanation") or ""),
            )
        )

    fix_type = str(parsed.get("fix_type") or "server").lower()
    if fix_type not in {"server", "firmware", "both"}:
        fix_type = "server" if code_bug.get("server_change_recommended") else "firmware"

    fw_name = ""
    if fix_type in {"firmware", "both"} or code_bug.get("firmware_update_recommended"):
        fw_name = str(code_bug.get("firmware_filename") or "")
        if not fw_name:
            fw_dir = _SERVER_ROOT / "firmware"
            if fw_dir.is_dir():
                bins = sorted(fw_dir.glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
                fw_name = bins[0].name if bins else ""

    proposal = FixProposal(
        incident_id=incident.incident_id,
        device_id=incident.device_id,
        display_name=incident.display_name,
        subsystem=incident.subsystem,
        fix_type=fix_type,
        model_used=model,
        bug_summary=str(parsed.get("bug_summary") or code_bug.get("bug_summary") or incident.error),
        root_cause=str(parsed.get("root_cause") or code_bug.get("likely_cause") or ""),
        error=incident.error,
        changes=changes,
        manual_steps=[str(s) for s in (parsed.get("manual_steps") or []) if str(s).strip()],
        firmware_filename=fw_name,
        confidence=str(parsed.get("confidence") or "medium"),
        analysis_steps=analysis_steps,
        related_files=related_files,
    )

    valid, valid_detail = validate_proposal(proposal)
    proposal.validation_passed = valid
    proposal.validation_detail = valid_detail

    if _env_bool("CODING_AGENT_RUN_TESTS", True) and valid and changes:
        proposal.test_results = run_targeted_tests(proposal)

    with _LOCK:
        rows = _load_proposals()
        rows.append(proposal)
        _save_proposals(rows)

    logger.info(
        "Coding agent proposal %s for incident %s (%d changes, fix_type=%s)",
        proposal.proposal_id,
        incident.incident_id,
        len(changes),
        fix_type,
    )
    return proposal


def _fallback_proposal(
    incident: Incident,
    code_bug: dict[str, Any],
    model: str,
    reason: str,
) -> FixProposal:
    """Create a manual-review proposal when LLM is unavailable."""
    proposal = FixProposal(
        incident_id=incident.incident_id,
        device_id=incident.device_id,
        display_name=incident.display_name,
        subsystem=incident.subsystem,
        fix_type="both" if code_bug.get("firmware_update_recommended") else "server",
        model_used=model,
        bug_summary=str(code_bug.get("bug_summary") or incident.error),
        root_cause=str(code_bug.get("likely_cause") or reason),
        error=incident.error,
        manual_steps=[
            str(code_bug.get("suggested_fix") or "Review logs and apply fix manually."),
            f"Coding agent note: {reason}",
        ],
        firmware_filename=str(code_bug.get("firmware_filename") or ""),
    )
    with _LOCK:
        rows = _load_proposals()
        rows.append(proposal)
        _save_proposals(rows)
    return proposal


def _server_base_url() -> str:
    host = os.environ.get("NINO_SERVER_LAN_HOST", "").strip()
    port = os.environ.get("NINO_SERVER_PORT", "8000").strip() or "8000"
    if not host:
        try:
            from network_util import server_lan_host

            host = server_lan_host()
        except Exception:
            host = "127.0.0.1"
    return f"http://{host}:{port}"


def _approval_token() -> str:
    return os.environ.get("CODING_AGENT_APPROVAL_TOKEN", "").strip()


def _approval_urls(proposal_id: str) -> tuple[str, str]:
    base = _server_base_url()
    token = _approval_token()
    qs = f"?token={token}" if token else ""
    approve = f"{base}/api/coding-agent/approve/{proposal_id}{qs}"
    reject = f"{base}/api/coding-agent/reject/{proposal_id}{qs}"
    return approve, reject


def build_proposal_email(proposal: FixProposal) -> tuple[str, str]:
    """Return (subject, plain_text_body) for developer approval email."""
    approve_url, reject_url = _approval_urls(proposal.proposal_id)
    bot = proposal.display_name or proposal.device_id or "NiNO"

    subject = f"[NiNO Coding Agent] Fix proposal for {bot} — {proposal.bug_summary[:60]}"

    lines = [
        "NiNO CODING AGENT — FIX PROPOSAL (approval required)",
        "=" * 55,
        "",
        f"Proposal ID:  {proposal.proposal_id}",
        f"Incident ID:  {proposal.incident_id}",
        f"Bot:          {bot} ({proposal.device_id})",
        f"Subsystem:    {proposal.subsystem}",
        f"Model used:   {proposal.model_used}",
        f"Fix type:     {proposal.fix_type}",
        f"Confidence:   {proposal.confidence}",
        f"Validation:   {'PASSED' if proposal.validation_passed else 'NEEDS REVIEW'} — {proposal.validation_detail}",
        "",
    ]

    if proposal.analysis_steps:
        lines.extend(["ANALYSIS STEPS", "-" * 40])
        for step in proposal.analysis_steps:
            lines.append(f"  • {step}")
        lines.append("")

    if proposal.test_results:
        lines.extend(["PRE-APPLY TESTS", "-" * 40])
        for result in proposal.test_results:
            lines.append(f"  • {result}")
        lines.append("")

    lines.extend([
        "THE BUG",
        "-" * 40,
        proposal.bug_summary,
        "",
        "ROOT CAUSE",
        "-" * 40,
        proposal.root_cause or "See error below.",
        "",
        "ERROR",
        "-" * 40,
        (proposal.error or "")[:500],
        "",
    ])

    if proposal.changes:
        lines.extend(["PROPOSED CODE CHANGES", "-" * 40])
        for i, change in enumerate(proposal.changes, 1):
            lines.extend(
                [
                    "",
                    f"Change {i}: {change.file_path}"
                    + (f" (lines {change.start_line}-{change.end_line})" if change.start_line else ""),
                    f"Why: {change.explanation}",
                    "",
                    "--- BEFORE ---",
                    change.old_code or "(see file)",
                    "",
                    "--- AFTER ---",
                    change.new_code or "(manual edit required)",
                ]
            )
    else:
        lines.extend(
            [
                "PROPOSED CODE CHANGES",
                "-" * 40,
                "No automatic code changes — manual review required.",
            ]
        )

    if proposal.manual_steps:
        lines.extend(["", "MANUAL STEPS", "-" * 40])
        for step in proposal.manual_steps:
            lines.append(f"  • {step}")

    lines.extend(["", "WHAT HAPPENS WHEN YOU APPROVE", "-" * 40])
    if proposal.fix_type in {"server", "both"} and proposal.changes:
        lines.append("  • Server code files will be patched automatically")
        lines.append("  • NiNO server will be scheduled for restart")
    elif proposal.fix_type == "server":
        lines.append("  • Manual server changes may be needed (no auto-patch)")
    if proposal.fix_type in {"firmware", "both"}:
        if proposal.firmware_filename:
            lines.append(f"  • OTA firmware deploy queued: {proposal.firmware_filename}")
            lines.append(f"    → POST {_server_base_url()}/api/ota/deploy/{proposal.device_id}")
        else:
            lines.append("  • Rebuild firmware (idf.py build) and upload .bin before OTA")

    lines.extend(
        [
            "",
            "APPROVE OR REJECT",
            "-" * 40,
            f"  APPROVE: {approve_url}",
            f"  REJECT:  {reject_url}",
            "",
            f"Or use Ops dashboard: {_server_base_url()}/ops",
            "",
            "This email was sent by NiNO Intelligent Mode Coding Agent.",
            "Fixes are NOT applied until you approve.",
        ]
    )
    return subject, "\n".join(lines)


def send_proposal_email(proposal: FixProposal, *, config: IntelligentConfig | None = None) -> tuple[bool, str]:
    """Email the developer with the fix proposal and approval links."""
    config = config or load_config()
    if not email_configured(config):
        return False, "Email not configured"
    if proposal.email_sent:
        return False, "Already emailed"

    subject, body = build_proposal_email(proposal)
    ok, detail = send_raw_email(subject, body, config=config)
    if ok:
        with _LOCK:
            rows = _load_proposals()
            for row in rows:
                if row.proposal_id == proposal.proposal_id:
                    row.email_sent = True
            _save_proposals(rows)
    return ok, detail


def _apply_code_change(change: CodeChange) -> tuple[bool, str]:
    path = _resolve_repo_path(change.file_path)
    if path is None:
        return False, f"File not found or not allowed: {change.file_path}"

    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"Cannot read {change.file_path}: {exc}"

    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_name = f"{proposal_backup_name(path)}.{_utc_now().replace(':', '-')}.bak"
    backup_path = _BACKUP_DIR / backup_name
    try:
        backup_path.write_text(original, encoding="utf-8")
    except OSError as exc:
        return False, f"Backup failed: {exc}"

    if change.old_code and change.old_code in original:
        updated = original.replace(change.old_code, change.new_code, 1)
    elif change.start_line > 0 and change.end_line >= change.start_line:
        lines = original.splitlines(keepends=True)
        start_idx = change.start_line - 1
        end_idx = change.end_line
        new_lines = change.new_code.splitlines(keepends=True)
        if not new_lines or not new_lines[-1].endswith("\n"):
            new_lines = [ln if ln.endswith("\n") else ln + "\n" for ln in change.new_code.splitlines()]
            if new_lines and not change.new_code.endswith("\n"):
                new_lines[-1] = new_lines[-1].rstrip("\n")
        updated = "".join(lines[:start_idx] + new_lines + lines[end_idx:])
    else:
        return False, f"Cannot locate change in {change.file_path} — old_code not found"

    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return False, f"Write failed: {exc}"

    return True, f"Patched {change.file_path} (backup: {backup_name})"


def proposal_backup_name(path: Path) -> str:
    try:
        rel = path.relative_to(_REPO_ROOT)
    except ValueError:
        rel = path.name
    return str(rel).replace("/", "_")


def _schedule_server_restart(proposal_id: str) -> str:
    restart_script = os.environ.get("CODING_AGENT_RESTART_SCRIPT", "").strip()
    if not restart_script:
        default_script = _SERVER_ROOT / "scripts" / "restart_nino_server.sh"
        if default_script.is_file():
            restart_script = str(default_script)
    flag_path = _SERVER_ROOT / "data" / "coding_agent_restart_requested.json"
    payload = {
        "proposal_id": proposal_id,
        "requested_at": _utc_now(),
        "script": restart_script,
    }
    flag_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if restart_script and Path(restart_script).is_file():
        try:
            subprocess.Popen(
                ["bash", restart_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return f"Restart script launched: {restart_script}"
        except OSError as exc:
            return f"Restart flag written; script failed ({exc}). Run: bash {restart_script}"
    return f"Restart requested — run: bash server/scripts/restart_nino_server.sh"


def _deploy_firmware_ota(proposal: FixProposal) -> tuple[bool, str]:
    if not proposal.firmware_filename:
        return False, "No firmware .bin specified — rebuild firmware first"
    if proposal.device_id in {"", "server"}:
        return False, "No target bot for OTA"

    try:
        from intelligent_mode.context import get_context
        from ota_service import request_firmware_deploy

        ctx = get_context()
        record = ctx.registry.get(proposal.device_id)
        if record is None:
            return False, f"Bot {proposal.device_id} not in registry"
        base = record.effective_base_url() if hasattr(record, "effective_base_url") else ""
        if not base:
            return False, "Bot has no base URL"

        result = request_firmware_deploy(
            device_id=proposal.device_id,
            filename=proposal.firmware_filename,
            base_url=base,
            requested_by=f"coding-agent:{proposal.proposal_id}",
            require_approval=False,
        )
        status = str(result.get("status") or "")
        return status in {"deployed", "approved"}, f"OTA {status}: {proposal.firmware_filename}"
    except Exception as exc:
        return False, f"OTA failed: {exc}"


def approve_proposal(proposal_id: str, *, token: str | None = None) -> dict[str, Any]:
    """Approve and apply a fix proposal."""
    expected = _approval_token()
    if expected and token != expected:
        return {"ok": False, "error": "Invalid approval token"}

    with _LOCK:
        rows = _load_proposals()
        proposal = next((r for r in rows if r.proposal_id == proposal_id), None)
        if proposal is None:
            return {"ok": False, "error": f"Unknown proposal: {proposal_id}"}
        if proposal.status not in {"pending"}:
            return {"ok": False, "error": f"Proposal status is {proposal.status}, not pending"}

        proposal.status = "approved"
        proposal.approved_at = _utc_now()

    details: list[str] = []
    patch_ok = True

    if proposal.fix_type in {"server", "both"} and proposal.changes:
        for change in proposal.changes:
            ok, detail = _apply_code_change(change)
            details.append(detail)
            if not ok:
                patch_ok = False

        if patch_ok:
            restart_detail = _schedule_server_restart(proposal_id)
            details.append(restart_detail)

    ota_ok = True
    if proposal.fix_type in {"firmware", "both"}:
        ota_ok, ota_detail = _deploy_firmware_ota(proposal)
        details.append(ota_detail)

    final_status = "applied" if patch_ok and ota_ok else "failed"
    apply_detail = "; ".join(details)

    with _LOCK:
        rows = _load_proposals()
        for row in rows:
            if row.proposal_id == proposal_id:
                row.status = final_status
                row.applied_at = _utc_now()
                row.apply_detail = apply_detail
        _save_proposals(rows)

    logger.info("Coding agent proposal %s %s: %s", proposal_id, final_status, apply_detail[:200])
    return {
        "ok": patch_ok and ota_ok,
        "proposal_id": proposal_id,
        "status": final_status,
        "detail": apply_detail,
    }


def reject_proposal(proposal_id: str, *, token: str | None = None, reason: str = "") -> dict[str, Any]:
    expected = _approval_token()
    if expected and token != expected:
        return {"ok": False, "error": "Invalid approval token"}

    with _LOCK:
        rows = _load_proposals()
        found = False
        for row in rows:
            if row.proposal_id == proposal_id:
                if row.status != "pending":
                    return {"ok": False, "error": f"Proposal status is {row.status}"}
                row.status = "rejected"
                row.apply_detail = reason or "Rejected by developer"
                found = True
        if not found:
            return {"ok": False, "error": f"Unknown proposal: {proposal_id}"}
        _save_proposals(rows)
    return {"ok": True, "proposal_id": proposal_id, "status": "rejected"}


def process_code_bug_incident(incident: Incident, *, config: IntelligentConfig | None = None) -> FixProposal | None:
    """Full pipeline: propose fix + email developer. Called from orchestrator."""
    return process_code_bug_incident_smart(incident, config=config)


def process_code_bug_incident_smart(
    incident: Incident,
    *,
    config: IntelligentConfig | None = None,
    force: bool = False,
) -> FixProposal | None:
    """Smart pipeline: multi-step analysis, validate, test, email."""
    config = config or load_config()
    if not _env_bool("CODING_AGENT_ENABLED", False) and not force:
        return None

    proposal = propose_fix(incident, force=force)
    if proposal is None:
        return None

    # Only email validated proposals (or all if validation skipped)
    should_email = _env_bool("CODING_AGENT_EMAIL", True)
    if should_email and proposal.validation_passed:
        ok, detail = send_proposal_email(proposal, config=config)
        if not ok:
            logger.warning("Coding agent email failed for %s: %s", proposal.proposal_id, detail)
    elif should_email and not proposal.validation_passed:
        logger.info(
            "Coding agent %s failed validation — email skipped: %s",
            proposal.proposal_id,
            proposal.validation_detail,
        )

    return proposal


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, str(int(default))).strip().lower()
    return raw not in {"0", "false", "no", "off"}
