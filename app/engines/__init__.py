"""Engines package — exports all engine classes and the AgentEngine protocol."""

from .base import AgentEngine
from .claude import ClaudeEngine
from .copilot import CopilotEngine
from .codex import CodexEngine
from .openai_compat import OpenAIEngine
from .mock import MockEngine

__all__ = ["AgentEngine", "ClaudeEngine", "CopilotEngine", "CodexEngine", "OpenAIEngine", "MockEngine"]
