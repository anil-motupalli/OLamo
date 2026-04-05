"""MockEngine — deterministic stub engine for headless / dry-run testing.

When ``AppSettings.headless = True`` the orchestrated pipeline swaps every
real engine for this one.  Each agent role returns a canned response that is
realistic enough to let the pipeline advance through all stages without making
any network calls.

Canned-response logic:
  - ``qa-engineer``   → "APPROVED – design looks good."
  - ``code-reviewer`` → "APPROVED – implementation looks good."
  - ``build-agent``   → "BUILD SUCCESS – all tests passed."
  - ``repo-manager``  → "https://github.com/mock/repo/pull/1  NO ACTIONABLE COMMENTS"
  - all others        → a short descriptive stub for the role
"""

from __future__ import annotations

from typing import Awaitable, Callable

from ..models import AppSettings, ModelConfig

_CANNED: dict[str, str] = {
    "lead-developer": (
        "## Design Plan (headless stub)\n\n"
        "1. Add feature X\n2. Update tests\n3. Update docs\n\nAPPROVED"
    ),
    "developer": (
        "## Implementation (headless stub)\n\n"
        "Created `feature_x.py` with required logic.\n"
        "Updated `test_feature_x.py` with passing tests."
    ),
    "code-reviewer": (
        "## Code Review (headless stub)\n\n"
        "Code quality is good.  No issues found.\n\nAPPROVED"
    ),
    "qa-engineer": (
        "## QA Review (headless stub)\n\n"
        "Design is complete and testable.\n\nAPPROVED"
    ),
    "build-agent": (
        "BUILD SUCCESS – all tests passed. (headless stub)"
    ),
    "repo-manager": (
        "https://github.com/mock/repo/pull/1\n\nNO ACTIONABLE COMMENTS (headless stub)"
    ),
}

_DEFAULT = "OK (headless stub)"


class MockEngine:
    """Deterministic stub — always returns canned text, never calls any API."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def run(
        self,
        role: str,
        prompt: str,
        system_prompt: str,
        tools: list[str],
        model: str,
        model_config: ModelConfig,
        mcp_servers: dict[str, dict],
        on_event: Callable[[dict], Awaitable[None]],
    ) -> str:
        text = _CANNED.get(role, _DEFAULT)
        await on_event({"type": "agent_message", "role": role, "text": text[:300]})
        return text
