"""Pipeline helpers: _make_env, _parse_stage_announcement,
_reviewer_prompt, _extract_comment_ids."""

from __future__ import annotations

import re

from ..models import AppSettings
from .approval_gate import ApprovalGate  # noqa: F401  (backward compat)

_STAGE_RE = re.compile(
    r"(Stage [1-4]|Design cycle \d+/\d+|Implementation cycle \d+/\d+|PR cycle \d+/\d+|CI check cycle \d+/\d+)",
    re.IGNORECASE,
)


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
    if role == "code-reviewer":
        return f"Review the implementation.{diff_ctx}"
    if role == "qa-engineer":
        return f"REVIEW CODE:\nOriginal plan:\n{plan}{diff_ctx}"
    return f"REVIEW IMPLEMENTATION:\nOriginal plan:\n{plan}{diff_ctx}"  # lead-developer


def _extract_comment_ids(text: str) -> list[str]:
    """Best-effort extraction of comment IDs from repo-manager poll output."""
    return re.findall(r"\bID[:\s]+(\S+)", text, re.IGNORECASE)
