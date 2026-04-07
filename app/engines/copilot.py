"""CopilotEngine — runs agents via the GitHub Copilot SDK."""

from __future__ import annotations

import asyncio
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

# Default timeout: 30 minutes — enough for complex developer/build tasks.
# Override per-agent via model_config.extra_params["timeout_seconds"].
_DEFAULT_TIMEOUT_SECONDS = 1800


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
        **_kwargs,
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

        # Event-driven approach matching OLaCo's TaskCompletionSource pattern.
        # SDK dispatches events from a background JSON-RPC thread, so we use
        # call_soon_threadsafe / run_coroutine_threadsafe to bridge to asyncio.
        loop = asyncio.get_running_loop()
        idle_event = asyncio.Event()
        error_exc: Exception | None = None
        message_parts: list[str] = []

        def _handler(event) -> None:
            nonlocal error_exc
            etype = event.type
            if hasattr(etype, "value"):
                etype = etype.value

            if etype == "assistant.message":
                content = str(getattr(getattr(event, "data", None), "content", "") or "")
                if content:
                    message_parts.append(content)
                    asyncio.run_coroutine_threadsafe(
                        on_event({"type": "agent_message", "role": role, "text": content}),
                        loop,
                    )
            elif etype == "tool.execution_start":
                tool_name = str(getattr(getattr(event, "data", None), "tool_name", "") or "")
                asyncio.run_coroutine_threadsafe(
                    on_event({"type": "agent_tool_call", "role": role, "tool_name": tool_name, "args_preview": ""}),
                    loop,
                )
            elif etype == "session.idle":
                loop.call_soon_threadsafe(idle_event.set)
            elif etype == "session.error":
                msg = str(getattr(getattr(event, "data", None), "message", "Unknown session error") or "")
                error_exc = RuntimeError(f"CopilotEngine session error for '{role}': {msg}")
                loop.call_soon_threadsafe(idle_event.set)

        timeout = float((model_config.extra_params or {}).get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
        unsubscribe = session.on(_handler)
        try:
            await session.send(prompt)
            try:
                await asyncio.wait_for(idle_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"CopilotEngine: agent '{role}' timed out after {timeout:.0f}s. "
                    f"Increase timeout_seconds in model_config.extra_params if needed."
                )
            if error_exc:
                raise error_exc
        finally:
            unsubscribe()
            await session.disconnect()

        return "\n".join(message_parts)
