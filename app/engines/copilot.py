"""CopilotEngine — runs agents via the GitHub Copilot SDK."""

from __future__ import annotations

import os
from typing import Awaitable, Callable

try:
    from copilot import CopilotClient, SubprocessConfig
    from copilot.session import PermissionHandler
except ImportError:
    CopilotClient = None       # type: ignore
    SubprocessConfig = None    # type: ignore
    PermissionHandler = None   # type: ignore

from .base import AgentEngine
from ..models import AppSettings, ModelConfig


class CopilotEngine:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client = None

    async def start(self) -> None:
        if CopilotClient is None:
            raise SystemExit(
                "github-copilot-sdk not installed.\n"
                "Install:  pip install github-copilot-sdk"
            )
        # Pass explicit token only when configured; otherwise let the SDK
        # auto-discover credentials from `gh auth login` (same as OLaCo's
        # `new CopilotClient()` with no args in C#).
        explicit_token = (
            self._settings.copilot_github_token
            or os.environ.get("COPILOT_GITHUB_TOKEN")
        )
        self._client = CopilotClient(SubprocessConfig(github_token=explicit_token or None))
        await self._client.start()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.stop()
            self._client = None

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
        # Build keyword args for create_session — all keyword-only, no positional dict.
        kwargs: dict = {
            "on_permission_request": PermissionHandler.approve_all,
            "model": model or None,
            "system_message": {"mode": "append", "content": system_prompt},
        }

        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers

        # BYOK provider config (e.g. z.ai, Azure, custom OpenAI)
        if model_config.base_url:
            kwargs["provider"] = {
                "type": model_config.provider_type or "openai",
                "base_url": model_config.base_url,
                "api_key": model_config.api_key,
            }

        try:
            session = await self._client.create_session(**kwargs)
        except Exception as e:
            raise RuntimeError(f"CopilotEngine: failed to create session for '{role}': {e}") from e

        try:
            # send_and_wait blocks until session.idle; returns final assistant message event
            event = await session.send_and_wait(prompt, timeout=600.0)
            result = str(getattr(getattr(event, "data", None), "content", "")) if event else ""
        finally:
            await session.disconnect()

        await on_event({"type": "agent_message", "role": role, "text": result[:300]})
        return result
