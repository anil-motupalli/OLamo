"""ClaudeEngine — runs agents via the Claude Agent SDK (claude-agent-sdk)."""

from __future__ import annotations

import os
from typing import Awaitable, Callable

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .base import AgentEngine
from ..models import AppSettings, ModelConfig


def _make_env(settings: AppSettings) -> dict[str, str]:
    """Build the subprocess env dict: bypass nested-session guard + optional base URL."""
    env: dict[str, str] = {"CLAUDECODE": ""}
    if settings.api_base_url:
        env["ANTHROPIC_BASE_URL"] = settings.api_base_url
    return env


class ClaudeEngine:
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
        env = _make_env(self._settings)
        # Apply per-agent model config overrides — api_key + base_url always respected
        if model_config.api_key:
            env["ANTHROPIC_API_KEY"] = model_config.api_key
        if model_config.base_url:
            env["ANTHROPIC_BASE_URL"] = model_config.base_url

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            tools=tools,
            model=model,
            permission_mode="acceptEdits",
            env=env,
            mcp_servers=mcp_servers,
        )
        result = ""
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        await on_event({"type": "agent_message", "role": role, "text": block.text[:300]})
            elif isinstance(msg, ResultMessage):
                result = msg.result
        return result
