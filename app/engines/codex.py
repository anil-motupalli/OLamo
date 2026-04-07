"""CodexEngine — Codex app-server SDK engine.

Uses the official ``codex_app_server`` SDK which communicates with the
``codex`` CLI binary via JSON-RPC v2 over stdio.

Install requirements:
  pip install codex-app-server-sdk   # or: cd codex/sdk/python && pip install -e .
  # plus the codex CLI binary (codex-cli-bin wheel or build from source)
  # See: https://github.com/openai/codex/tree/main/sdk/python

The Codex CLI manages its own sandboxed tool execution (file I/O, shell
commands, web search) just like the Claude CLI does for claude-agent-sdk.
The ``tools`` list passed by OLamo serves as documentation / intent only;
actual capability is governed by the ``approval_policy`` set on the thread.
"""

from __future__ import annotations

from typing import Awaitable, Callable

try:
    from codex_app_server import AsyncCodex
except ImportError:
    AsyncCodex = None  # type: ignore

from .base import AgentEngine
from ..models import AppSettings, ModelConfig


class CodexEngine:
    """
    Engine backed by the Codex app-server SDK (``codex_app_server``).

    One ``AsyncCodex`` client is kept alive for the duration of a pipeline run
    (``start()`` → many ``run()`` calls → ``stop()``).  Each agent invocation
    creates a fresh ephemeral thread so context doesn't bleed between agents.

    Model selection follows the same pattern as ``CopilotEngine``: pass the
    model name explicitly via ``thread_start(model=...)``.

    ``model_config.extra_params`` is forwarded as the Codex thread ``config``
    dict (e.g. ``{"model_reasoning_effort": "high"}``).
    """

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client: "AsyncCodex | None" = None
        self._ctx = None

    async def start(self) -> None:
        if AsyncCodex is None:
            raise SystemExit(
                "codex-app-server-sdk not installed and the Codex engine is required.\n"
                "Install with:\n"
                "  pip install codex-app-server-sdk\n"
                "Or build from the source repo:\n"
                "  cd codex/sdk/python && pip install -e .\n"
                "See: https://github.com/openai/codex/tree/main/sdk/python"
            )
        self._ctx = AsyncCodex()
        self._client = await self._ctx.__aenter__()

    async def stop(self) -> None:
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._ctx = None
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
        run_id: str | None = None,
    ) -> str:
        if self._client is None:
            raise RuntimeError("CodexEngine not started — call start() first")

        thread_kwargs: dict = {
            "model": model,
            "developer_instructions": system_prompt,
            # Never prompt for human approval — mirrors permission_mode="acceptEdits"
            # in ClaudeEngine and the auto-session behaviour of CopilotEngine.
            "approval_policy": "never_require",
            # Ephemeral threads don't persist after the session ends.
            "ephemeral": True,
        }
        # Forward extra_params as Codex thread config (e.g. reasoning effort).
        if model_config.extra_params:
            thread_kwargs["config"] = model_config.extra_params

        thread = await self._client.thread_start(**thread_kwargs)
        result = await thread.run(prompt)

        text = result.final_response or ""
        await on_event({"type": "agent_message", "role": role, "text": text[:300]})
        return text
