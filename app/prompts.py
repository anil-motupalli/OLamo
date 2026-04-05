"""PromptLoader — loads agent character files and task prompt templates from
the ``agents/`` directory tree, substituting ``{{token}}`` placeholders.

Directory layout (mirrors OLaCo)::

    agents/
        <role>.md                      ← character / system prompt
        prompts/
            <role>/
                <task>.md              ← per-task prompt template

Token substitution uses ``{{key}}`` syntax (double braces, matching OLaCo's
PromptLoader convention).
"""

from __future__ import annotations

import re
from pathlib import Path

# Repo root → agents/ lives next to app/
_AGENTS_DIR = Path(__file__).parent.parent / "agents"

_TOKEN_RE = re.compile(r"\{\{(\w+)\}\}")


def _substitute(text: str, tokens: dict[str, str]) -> str:
    return _TOKEN_RE.sub(lambda m: tokens.get(m.group(1), m.group(0)), text)


def load_character(role: str) -> str:
    """Load the character/system-prompt markdown for *role*.

    Example::

        load_character("lead-developer")
    """
    path = _AGENTS_DIR / f"{role}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent character file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def load_prompt(role: str, task: str, tokens: dict[str, str] | None = None) -> str:
    """Load and render a task prompt template.

    Example::

        load_prompt("lead-developer", "plan", {"task": "Add reverse_string"})
    """
    path = _AGENTS_DIR / "prompts" / role / f"{task}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    return _substitute(text, tokens or {})


def agents_dir() -> Path:
    """Return the absolute path to the agents/ directory."""
    return _AGENTS_DIR
