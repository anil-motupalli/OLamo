"""Engines package — exports all engine classes, the AgentEngine protocol, and ENGINE_REGISTRY."""

from .base import AgentEngine
from .claude import ClaudeEngine
from .copilot import CopilotEngine
from .codex import CodexEngine
from .openai_compat import OpenAIEngine
from .mock import MockEngine

ENGINE_REGISTRY: dict[str, type] = {
    "claude": ClaudeEngine,
    "copilot": CopilotEngine,
    "codex": CodexEngine,
    "openai": OpenAIEngine,
    "mock": MockEngine,
}

__all__ = [
    "AgentEngine", "ClaudeEngine", "CopilotEngine", "CodexEngine",
    "OpenAIEngine", "MockEngine", "ENGINE_REGISTRY",
]
