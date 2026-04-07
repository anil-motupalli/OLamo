"""Tests for CopilotEngine session reuse, persistence, and resume behaviour."""

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.copilot import CopilotEngine
from app.models import AppSettings, ModelConfig


def _make_event_driven_session(content="copilot result"):
    """Build a mock session matching the event-driven SDK API."""
    session = MagicMock()
    session.disconnect = AsyncMock()
    session.send = AsyncMock()
    session.session_id = "sess-123"

    idle_et = MagicMock()
    idle_et.value = "session.idle"
    idle_evt = MagicMock()
    idle_evt.type = idle_et
    idle_evt.data = MagicMock()

    msg_et = MagicMock()
    msg_et.value = "assistant.message"
    msg_evt = MagicMock()
    msg_evt.type = msg_et
    msg_evt.data = MagicMock()
    msg_evt.data.content = content

    def fake_on(handler):
        handler(msg_evt)
        handler(idle_evt)
        return lambda: None

    session.on = MagicMock(side_effect=fake_on)
    return session


def _make_engine():
    """Create a CopilotEngine with a mock client."""
    mock_client = MagicMock()
    mock_client.start = AsyncMock()
    mock_client.stop = AsyncMock()
    mock_client.resume_session = AsyncMock(side_effect=Exception("no resume"))

    engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
    engine._client = mock_client
    return engine, mock_client


class TestSessionReuse:
    """Session is reused when run_id is set — no second create_session call."""

    @pytest.mark.asyncio
    async def test_same_run_id_role_reuses_session(self):
        engine, mock_client = _make_engine()
        session = _make_event_driven_session("result")
        mock_client.create_session = AsyncMock(return_value=session)

        mc = ModelConfig()
        # First call — should create
        r1 = await engine.run(
            role="developer", prompt="do X", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
        )
        assert r1 == "result"
        assert mock_client.create_session.call_count == 1

        # Second call — same (run_id, role) — should reuse, no new session
        r2 = await engine.run(
            role="developer", prompt="now do Y", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
        )
        assert r2 == "result"
        assert mock_client.create_session.call_count == 1  # no additional call

        await engine.stop()

    @pytest.mark.asyncio
    async def test_different_role_creates_new_session(self):
        engine, mock_client = _make_engine()
        mock_client.create_session = AsyncMock(return_value=_make_event_driven_session("ok"))

        mc = ModelConfig()
        await engine.run(
            role="developer", prompt="do X", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
        )
        await engine.run(
            role="qa-engineer", prompt="test it", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
        )
        assert mock_client.create_session.call_count == 2
        await engine.stop()

    @pytest.mark.asyncio
    async def test_different_run_id_creates_new_session(self):
        engine, mock_client = _make_engine()
        mock_client.create_session = AsyncMock(return_value=_make_event_driven_session("ok"))

        mc = ModelConfig()
        await engine.run(
            role="developer", prompt="do X", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
        )
        await engine.run(
            role="developer", prompt="do Y", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-2",
        )
        assert mock_client.create_session.call_count == 2
        await engine.stop()


class TestSessionNaming:
    """Agent is named {run_id}_{role} when run_id is set."""

    @pytest.mark.asyncio
    async def test_client_name_set_when_run_id_present(self):
        engine, mock_client = _make_engine()
        mock_client.create_session = AsyncMock(return_value=_make_event_driven_session("ok"))

        await engine.run(
            role="build-agent", prompt="build", system_prompt="sys",
            tools=[], model="gpt-5", model_config=ModelConfig(),
            mcp_servers={}, on_event=AsyncMock(), run_id="20260407_3",
        )

        call_kwargs = mock_client.create_session.call_args.kwargs
        assert call_kwargs["client_name"] == "20260407_3_build-agent"
        await engine.stop()

    @pytest.mark.asyncio
    async def test_client_name_absent_when_run_id_is_none(self):
        engine, mock_client = _make_engine()
        mock_client.create_session = AsyncMock(return_value=_make_event_driven_session("ok"))

        await engine.run(
            role="build-agent", prompt="build", system_prompt="sys",
            tools=[], model="gpt-5", model_config=ModelConfig(),
            mcp_servers={}, on_event=AsyncMock(), run_id=None,
        )

        call_kwargs = mock_client.create_session.call_args.kwargs
        assert "client_name" not in call_kwargs
        await engine.stop()


class TestCloseRun:
    """close_run disconnects only sessions for a specific run_id."""

    @pytest.mark.asyncio
    async def test_close_run_disconnects_only_target_run(self):
        engine, mock_client = _make_engine()
        sess_a = _make_event_driven_session("a")
        sess_b = _make_event_driven_session("b")
        mock_client.create_session = AsyncMock(side_effect=[sess_a, sess_b])

        mc = ModelConfig()
        await engine.run(
            role="developer", prompt="X", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-A",
        )
        await engine.run(
            role="developer", prompt="Y", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-B",
        )

        await engine.close_run("run-A")

        # run-A session was disconnected, run-B still cached
        assert ("run-A", "developer") not in engine._session_cache
        assert ("run-B", "developer") in engine._session_cache
        sess_a.disconnect.assert_awaited_once()
        sess_b.disconnect.assert_not_awaited()

        await engine.stop()


class TestNoDisconnectWhenCached:
    """Session is NOT disconnected after run() when run_id is set."""

    @pytest.mark.asyncio
    async def test_run_with_run_id_does_not_disconnect(self):
        engine, mock_client = _make_engine()
        session = _make_event_driven_session("ok")
        mock_client.create_session = AsyncMock(return_value=session)

        await engine.run(
            role="developer", prompt="X", system_prompt="sys",
            tools=[], model="gpt-5", model_config=ModelConfig(),
            mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
        )

        session.disconnect.assert_not_awaited()
        assert ("run-1", "developer") in engine._session_cache
        await engine.stop()

    @pytest.mark.asyncio
    async def test_run_without_run_id_does_disconnect(self):
        engine, mock_client = _make_engine()
        session = _make_event_driven_session("ok")
        mock_client.create_session = AsyncMock(return_value=session)

        await engine.run(
            role="developer", prompt="X", system_prompt="sys",
            tools=[], model="gpt-5", model_config=ModelConfig(),
            mcp_servers={}, on_event=AsyncMock(), run_id=None,
        )

        session.disconnect.assert_awaited_once()
        await engine.stop()


class TestResumeFromDB:
    """On creation, engine attempts to resume from DB before creating fresh."""

    @pytest.mark.asyncio
    async def test_resume_succeeds(self):
        engine, mock_client = _make_engine()
        resumed_session = _make_event_driven_session("resumed result")
        mock_client.resume_session = AsyncMock(return_value=resumed_session)

        # Set a truthy db_conn to enter the DB path
        engine._db_conn = MagicMock()

        with patch("app.db.sessions.lookup_session", new_callable=AsyncMock) as mock_lookup, \
             patch("app.db.sessions.upsert_session", new_callable=AsyncMock):
            mock_lookup.return_value = {
                "session_id": "sess-old-999",
                "status": "active",
                "settings_snapshot": asdict(ModelConfig()),
            }

            result = await engine.run(
                role="developer", prompt="continue", system_prompt="sys",
                tools=[], model="gpt-5", model_config=ModelConfig(),
                mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
            )

        assert result == "resumed result"
        mock_client.resume_session.assert_awaited_once_with("sess-old-999")
        mock_client.create_session.assert_not_called()
        await engine.stop()

    @pytest.mark.asyncio
    async def test_resume_fails_creates_fresh(self):
        engine, mock_client = _make_engine()
        fresh_session = _make_event_driven_session("fresh result")
        mock_client.resume_session = AsyncMock(side_effect=RuntimeError("session expired"))
        mock_client.create_session = AsyncMock(return_value=fresh_session)

        engine._db_conn = MagicMock()

        with patch("app.db.sessions.lookup_session", new_callable=AsyncMock) as mock_lookup, \
             patch("app.db.sessions.mark_expired", new_callable=AsyncMock), \
             patch("app.db.sessions.upsert_session", new_callable=AsyncMock):
            mock_lookup.return_value = {
                "session_id": "sess-old-999",
                "status": "active",
                "settings_snapshot": asdict(ModelConfig()),
            }

            result = await engine.run(
                role="developer", prompt="continue", system_prompt="sys",
                tools=[], model="gpt-5", model_config=ModelConfig(),
                mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
            )

        assert result == "fresh result"
        mock_client.resume_session.assert_awaited_once()
        mock_client.create_session.assert_awaited_once()
        await engine.stop()


class TestBackwardCompat:
    """Engine works identically when run_id is None (legacy path)."""

    @pytest.mark.asyncio
    async def test_no_run_id_creates_disconnects_per_call(self):
        engine, mock_client = _make_engine()
        session_a = _make_event_driven_session("first")
        session_b = _make_event_driven_session("second")
        mock_client.create_session = AsyncMock(side_effect=[session_a, session_b])

        mc = ModelConfig()
        r1 = await engine.run(
            role="developer", prompt="A", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id=None,
        )
        r2 = await engine.run(
            role="developer", prompt="B", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id=None,
        )

        assert r1 == "first"
        assert r2 == "second"
        assert mock_client.create_session.call_count == 2
        session_a.disconnect.assert_awaited_once()
        session_b.disconnect.assert_awaited_once()
        assert len(engine._session_cache) == 0
        await engine.stop()


class TestStopCleanup:
    """stop() disconnects all cached sessions."""

    @pytest.mark.asyncio
    async def test_stop_disconnects_all_cached_sessions(self):
        engine, mock_client = _make_engine()
        sess1 = _make_event_driven_session("a")
        sess2 = _make_event_driven_session("b")
        mock_client.create_session = AsyncMock(side_effect=[sess1, sess2])

        mc = ModelConfig()
        await engine.run(
            role="developer", prompt="X", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
        )
        await engine.run(
            role="qa-engineer", prompt="Y", system_prompt="sys",
            tools=[], model="gpt-5", model_config=mc,
            mcp_servers={}, on_event=AsyncMock(), run_id="run-1",
        )

        assert len(engine._session_cache) == 2

        await engine.stop()

        assert len(engine._session_cache) == 0
        sess1.disconnect.assert_awaited_once()
        sess2.disconnect.assert_awaited_once()
