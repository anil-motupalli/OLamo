"""AgentEngine Protocol — the common interface all engine implementations must satisfy."""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from ..models import AppSettings, ModelConfig


class AgentEngine(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
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
        run_id: str | None = None,
    ) -> str: ...
