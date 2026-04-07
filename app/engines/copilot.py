"""CopilotEngine — runs agents via the GitHub Copilot SDK.

Sessions are reused across calls for the same (run_id, role) pair and optionally
persisted to the ``agent_sessions`` table so they can be resumed after a server
restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from typing import Awaitable, Callable

try:
    from copilot import CopilotClient, SubprocessConfig
    from copilot.session import PermissionHandler
except ImportError:
    CopilotClient = None       # type: ignore
    SubprocessConfig = None    # type: ignore
    PermissionHandler = None   # type: ignore

from .base import AgentEngine
from ..models import AppSettings, ModelConfig, resolve_secret
logger = logging.getLogger(__name__)

# Default timeout: 30 minutes — enough for complex developer/build tasks.
# Override per-agent via model_config.extra_params["timeout_seconds"].
_DEFAULT_TIMEOUT_SECONDS = 1800


class CopilotEngine:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client = None
        # In-process session cache: (run_id, role) → SDK session object.
        self._session_cache: dict[tuple[str, str], object] = {}
        # Optional DB persistence hooks — set by the pipeline / runner.
        self._db_conn = None  # aiosqlite.Connection
        self._session_persist_fn: Callable | None = None

    def set_db_conn(self, conn) -> None:  # pragma: no cover — wired by pipeline
        """Provide an aiosqlite connection for session persistence."""
        self._db_conn = conn

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
        """Disconnect all cached sessions, then shut down the SDK client."""
        for key, session in list(self._session_cache.items()):
            try:
                await session.disconnect()
            except Exception:
                pass
            if self._db_conn is not None:
                try:
                    from ..db.sessions import mark_closed
                    await mark_closed(self._db_conn, key[0], key[1])
                except Exception:
                    pass
        self._session_cache.clear()
        if self._client is not None:
            await self._client.stop()
            self._client = None

    async def close_run(self, run_id: str) -> None:
        """Disconnect and remove all cached sessions for a specific run_id."""
        for key in list(self._session_cache):
            if key[0] == run_id:
                session = self._session_cache.pop(key, None)
                if session is not None:
                    try:
                        await session.disconnect()
                    except Exception:
                        pass
                if self._db_conn is not None:
                    try:
                        from ..db.sessions import mark_closed
                        await mark_closed(self._db_conn, key[0], key[1])
                    except Exception:
                        pass

    async def _get_or_create_session(
        self,
        run_id: str | None,
        role: str,
        model: str,
        model_config: ModelConfig,
        mcp_servers: dict[str, dict],
        system_prompt: str,
    ) -> tuple[object, bool]:
        """Return a cached session for (run_id, role), creating one if needed.

        Returns (session, is_new).  When *run_id* is ``None`` a fresh session
        is always created (backward-compatible path).
        """
        if run_id is None:
            return await self._create_session(role, model, model_config, mcp_servers, system_prompt), True

        cache_key = (run_id, role)

        # 1. In-process cache hit
        cached = self._session_cache.get(cache_key)
        if cached is not None:
            return cached, False

        # 2. Try to resume from DB
        if self._db_conn is not None:
            session = await self._try_resume_from_db(run_id, role, model, model_config, mcp_servers, system_prompt)
            if session is not None:
                self._session_cache[cache_key] = session
                return session, False

        # 3. Fresh session
        client_name = f"{run_id}_{role}"
        session = await self._create_session(
            role, model, model_config, mcp_servers, system_prompt, client_name=client_name,
        )
        self._session_cache[cache_key] = session

        # Persist to DB
        await self._persist_session(run_id, role, session, model_config)

        return session, True

    async def _try_resume_from_db(
        self,
        run_id: str,
        role: str,
        model: str,
        model_config: ModelConfig,
        mcp_servers: dict[str, dict],
        system_prompt: str,
    ) -> object | None:
        """Attempt to resume a previously persisted session.  Returns None on failure."""
        try:
            from ..db.sessions import lookup_session, mark_expired, upsert_session
            row = await lookup_session(self._db_conn, run_id, role)
            if row is None or row["status"] != "active":
                return None
            session_id = row["session_id"]
            if not session_id:
                return None
            resumed = await self._client.resume_session(session_id)
            # Persist the (possibly new) session ID
            await upsert_session(
                self._db_conn, run_id, role,
                agent_name=f"{run_id}_{role}",
                session_id=getattr(resumed, "session_id", session_id),
                settings_snapshot=asdict(model_config),
            )
            logger.info("Resumed Copilot session %s for (%s, %s)", session_id, run_id, role)
            return resumed
        except Exception:
            logger.warning("Failed to resume Copilot session for (%s, %s), creating fresh", run_id, role, exc_info=True)
            try:
                from ..db.sessions import mark_expired
                await mark_expired(self._db_conn, run_id, role)
            except Exception:
                pass
            return None

    async def _persist_session(self, run_id: str, role: str, session: object, model_config: ModelConfig) -> None:
        """Write session info to the agent_sessions table."""
        if self._db_conn is None:
            return
        try:
            from ..db.sessions import upsert_session
            session_id = getattr(session, "session_id", None) or ""
            await upsert_session(
                self._db_conn, run_id, role,
                agent_name=f"{run_id}_{role}",
                session_id=session_id,
                settings_snapshot=asdict(model_config),
            )
        except Exception:
            logger.warning("Failed to persist Copilot session for (%s, %s)", run_id, role, exc_info=True)

    async def _create_session(
        self,
        role: str,
        model: str,
        model_config: ModelConfig,
        mcp_servers: dict[str, dict],
        system_prompt: str,
        client_name: str | None = None,
    ) -> object:
        """Create a new Copilot SDK session with the given parameters."""
        kwargs: dict = {
            "on_permission_request": PermissionHandler.approve_all,
            "model": model or None,
            "system_message": {"mode": "append", "content": system_prompt},
        }
        if client_name:
            kwargs["client_name"] = client_name
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers

        # BYOK provider config (e.g. z.ai, Azure, custom OpenAI)
        if model_config.base_url:
            kwargs["provider"] = {
                "type": model_config.provider_type or "openai",
                "base_url": model_config.base_url,
                "api_key": resolve_secret(model_config.api_key),
            }

        try:
            return await self._client.create_session(**kwargs)
        except Exception as e:
            raise RuntimeError(f"CopilotEngine: failed to create session for '{role}': {e}") from e

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
        **_kwargs,
    ) -> str:
        session, is_new = await self._get_or_create_session(
            run_id, role, model, model_config, mcp_servers, system_prompt,
        )

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
            # Only disconnect if session is not cached (run_id is None → legacy path)
            if run_id is None:
                try:
                    await session.disconnect()
                except Exception:
                    pass

        return "\n".join(message_parts)
