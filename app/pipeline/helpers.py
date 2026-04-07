"""Pipeline helpers: _make_env, _parse_stage_announcement,
_reviewer_prompt, _extract_comment_ids, parse_review_json,
parse_finding_responses."""

from __future__ import annotations

import json
import re
from typing import Any

from ..models import AppSettings
from .approval_gate import ApprovalGate  # noqa: F401  (backward compat)

_STAGE_RE = re.compile(
    r"(Stage [1-4]|Design cycle \d+/\d+|Implementation cycle \d+/\d+|PR cycle \d+/\d+|CI check cycle \d+/\d+)",
    re.IGNORECASE,
)

# Separator used by lead-developer and developer to split plan/summary from finding responses
FINDING_RESPONSES_SEP = "---FINDING_RESPONSES---"


def _parse_stage_announcement(text: str) -> str | None:
    m = _STAGE_RE.search(text)
    return m.group(0) if m else None


def _make_env(settings: AppSettings) -> dict[str, str]:
    """Build the subprocess env dict: bypass nested-session guard + optional base URL."""
    env: dict[str, str] = {"CLAUDECODE": ""}
    if settings.api_base_url:
        env["ANTHROPIC_BASE_URL"] = settings.api_base_url
    return env


def _reviewer_prompt(role: str, plan: str, diff_ctx: str) -> str:
    """Build the review prompt for a given reviewer role."""
    if role == "qa-engineer":
        return f"REVIEW CODE:\nOriginal plan:\n{plan}{diff_ctx}"
    return f"REVIEW IMPLEMENTATION:\nOriginal plan:\n{plan}{diff_ctx}"  # lead-developer


def _extract_comment_ids(text: str) -> list[str]:
    """Best-effort extraction of comment IDs from repo-manager poll output."""
    return re.findall(r"\bID[:\s]+(\S+)", text, re.IGNORECASE)


def parse_review_json(text: str) -> dict[str, Any]:
    """Parse structured JSON review output from an agent.

    Agents output a JSON block (possibly inside markdown fences) of the form:
    {
      "decision": "Approved" | "NeedsImprovement",
      "findings": [
        {"id": "f1", "type": "...", "severity": "...", "file": "...",
         "line": 0, "description": "...", "suggestion": "..."}
      ]
    }

    Falls back to text heuristics (APPROVED / NEEDS IMPROVEMENT keywords)
    if JSON cannot be extracted, so old-style responses still work.

    Returns a dict with keys: decision (str), findings (list[dict]).
    """
    raw = text.strip()

    # 1. Try to extract a JSON block (with or without markdown fences)
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if not json_match:
        # Try bare JSON object
        json_match = re.search(r"(\{[^{}]*\"decision\"[^{}]*\})", raw, re.DOTALL)
    if not json_match:
        # Greedy: find first { to last }
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            json_match = re.match(r"(.+)", raw[start:end + 1], re.DOTALL)
            try:
                data = json.loads(raw[start:end + 1])
                return _normalise_review(data)
            except (json.JSONDecodeError, ValueError):
                pass

    if json_match:
        try:
            data = json.loads(json_match.group(1))
            return _normalise_review(data)
        except (json.JSONDecodeError, ValueError):
            pass

    # 2. Fallback: heuristic text parsing
    upper = raw.upper()
    if "APPROVED" in upper and "NEEDS IMPROVEMENT" not in upper:
        return {"decision": "Approved", "findings": []}
    return {
        "decision": "NeedsImprovement",
        "findings": [{"id": "f1", "type": "General", "severity": "MustHave",
                      "file": None, "line": 0, "description": raw[:500],
                      "suggestion": "See full agent output."}],
    }


def _normalise_review(data: dict) -> dict[str, Any]:
    """Normalise field names and assign sequential IDs to findings."""
    decision = data.get("decision", "Approved")
    # Accept both "NeedsImprovement" and "NEEDS_IMPROVEMENT" and "Needs Improvement"
    if "needs" in decision.lower() or "improvement" in decision.lower():
        decision = "NeedsImprovement"
    findings = data.get("findings", [])
    # Assign IDs if missing
    for i, f in enumerate(findings):
        if not f.get("id"):
            f["id"] = f"f{i + 1}"
        # Normalise severity
        sev = str(f.get("severity", "MustHave"))
        if sev.lower() in ("critical", "blocker"):
            f["severity"] = "Critical"
        elif sev.lower() in ("musthave", "must have", "must-have", "high"):
            f["severity"] = "MustHave"
        elif sev.lower() in ("goodtohave", "good to have", "medium"):
            f["severity"] = "GoodToHave"
        else:
            f["severity"] = sev
    return {"decision": decision, "findings": findings}


_BUILD_SUCCESS_STATUSES = {"BUILD SUCCESS"}
_BUILD_FAILURE_STATUSES = {"BUILD FAILURE", "TEST FAILURE"}


def parse_build_output(text: str) -> dict:
    """Parse build-agent structured JSON output.

    Returns a dict with at least:
      ``{"status": "BUILD SUCCESS"|"BUILD FAILURE"|"TEST FAILURE",
         "output": str, "build_errors": list, "test_failures": list}``

    Falls back to text heuristics so old-style plain-text responses still work.
    """
    raw = text.strip()
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            data = json.loads(raw[start:end + 1])
            if "status" in data:
                data.setdefault("output", raw)
                data.setdefault("build_errors", [])
                data.setdefault("test_failures", [])
                # back-compat: fold old "errors" string into build_errors list
                if "errors" in data and isinstance(data["errors"], str) and data["errors"]:
                    if not data["build_errors"]:
                        data["build_errors"] = [{"file": None, "line": 0, "message": data["errors"]}]
                return data
    except (json.JSONDecodeError, ValueError):
        pass
    upper = raw.upper()
    if "SUCCESS" in upper:
        return {"status": "BUILD SUCCESS", "output": raw, "build_errors": [], "test_failures": []}
    return {"status": "BUILD FAILURE", "output": raw,
            "build_errors": [{"file": None, "line": 0, "message": raw}], "test_failures": []}


def _build_failed(parsed: dict) -> bool:
    """Return True when a parse_build_output result represents a failure."""
    return parsed.get("status", "") in _BUILD_FAILURE_STATUSES


def _build_failure_summary(parsed: dict) -> str:
    """Return a developer-friendly summary of all build/test errors."""
    import json as _json
    parts: list[str] = [f"Status: {parsed.get('status', 'BUILD FAILURE')}"]
    if parsed.get("build_errors"):
        parts.append("Build errors:\n" + _json.dumps(parsed["build_errors"], indent=2))
    if parsed.get("test_failures"):
        parts.append("Test failures:\n" + _json.dumps(parsed["test_failures"], indent=2))
    if not parsed.get("build_errors") and not parsed.get("test_failures"):
        parts.append(parsed.get("output", "")[:2000])
    return "\n\n".join(parts)


def parse_repo_output(text: str) -> dict:
    """Parse repo-manager structured JSON output.

    Returns a dict containing at least a ``mode`` key where known, plus
    mode-specific fields (``pr_url``, ``diff``, ``status``, ``comments``, etc.).

    Falls back to text heuristics so old-style plain-text responses still work.
    """
    raw = text.strip()
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            data = json.loads(raw[start:end + 1])
            if "mode" in data or "pr_url" in data or ("status" in data and "diff" in data):
                data.setdefault("raw", raw)
                return data
    except (json.JSONDecodeError, ValueError):
        pass
    upper = raw.upper()
    result: dict = {"raw": raw}
    if "CHECKS PASSING" in upper:
        result.update({"mode": "poll_ci", "status": "CHECKS PASSING", "details": ""})
    elif "CHECKS FAILING" in upper:
        result.update({"mode": "poll_ci", "status": "CHECKS FAILING", "details": raw})
    elif "NO ACTIONABLE COMMENTS" in upper:
        result.update({"mode": "poll_comments", "status": "NO ACTIONABLE COMMENTS", "count": 0, "comments": []})
    elif "ACTIONABLE COMMENTS" in upper:
        result.update({"mode": "poll_comments", "status": raw, "count": 0, "comments": []})
    else:
        result.update({"diff": raw})
    return result


def parse_finding_responses(text: str) -> tuple[str, list[dict]]:
    """Split agent output on FINDING_RESPONSES_SEP and parse the JSON responses.

    Returns (main_text, responses) where responses is a list of:
    {"id": "f1", "action": "ADDRESSED|FIXED|PUSHBACK", "explanation": "..."}
    """
    if FINDING_RESPONSES_SEP not in text:
        return text, []
    parts = text.split(FINDING_RESPONSES_SEP, 1)
    main_text = parts[0].strip()
    try:
        responses = json.loads(parts[1].strip())
        if isinstance(responses, list):
            return main_text, responses
    except (json.JSONDecodeError, ValueError):
        pass
    return main_text, []
