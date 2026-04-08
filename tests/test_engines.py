"""Tests for engine implementations: ClaudeEngine, CopilotEngine, OpenAIEngine, MockEngine."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.claude import ClaudeEngine
from app.engines.copilot import CopilotEngine
from app.engines.openai_compat import OpenAIEngine
from app.models import AgentEngineConfig, AppSettings, ModelConfig
from app.pipeline.orchestrated import run_pipeline_orchestrated


class TestClaudeEngine:
    @pytest.mark.asyncio
    async def test_start_is_noop(self):
        engine = ClaudeEngine(AppSettings())
        await engine.start()  # must not raise

    @pytest.mark.asyncio
    async def test_stop_is_noop(self):
        engine = ClaudeEngine(AppSettings())
        await engine.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_run_returns_result_message_text(self):
        from claude_agent_sdk import ResultMessage
        mock_result = MagicMock(spec=ResultMessage)
        mock_result.result = "implementation done"

        async def fake_query(**kwargs):
            yield mock_result

        with patch("app.engines.claude.query", fake_query):
            engine = ClaudeEngine(AppSettings())
            result = await engine.run(
                role="developer", prompt="implement X",
                system_prompt="You are a developer", tools=["Read", "Write"],
                model="sonnet", model_config=ModelConfig(),
                mcp_servers={}, on_event=AsyncMock(),
            )
        assert result == "implementation done"

    @pytest.mark.asyncio
    async def test_run_emits_agent_message_for_text_blocks(self):
        from claude_agent_sdk import AssistantMessage, TextBlock, ResultMessage
        mock_msg = MagicMock(spec=AssistantMessage)
        mock_block = MagicMock(spec=TextBlock)
        mock_block.text = "Working on it..."
        mock_msg.content = [mock_block]
        mock_result = MagicMock(spec=ResultMessage)
        mock_result.result = "done"

        events = []
        async def on_event(evt): events.append(evt)

        async def fake_query(**kwargs):
            yield mock_msg
            yield mock_result

        with patch("app.engines.claude.query", fake_query):
            engine = ClaudeEngine(AppSettings())
            await engine.run(
                role="developer", prompt="x", system_prompt="sys",
                tools=[], model="sonnet", model_config=ModelConfig(),
                mcp_servers={}, on_event=on_event,
            )
        agent_msgs = [e for e in events if e["type"] == "agent_message"]
        assert len(agent_msgs) == 1
        assert agent_msgs[0]["role"] == "developer"
        assert "Working on it" in agent_msgs[0]["text"]

    @pytest.mark.asyncio
    async def test_run_per_agent_base_url_overrides_global(self):
        captured = {}

        async def fake_query(**kwargs):
            captured.update(kwargs.get("options").env or {})
            from claude_agent_sdk import ResultMessage
            mock = MagicMock(spec=ResultMessage)
            mock.result = ""
            yield mock

        with patch("app.engines.claude.query", fake_query):
            engine = ClaudeEngine(AppSettings(api_base_url="https://global.example.com"))
            await engine.run(
                role="developer", prompt="x", system_prompt="sys", tools=[],
                model="gpt-4",
                model_config=ModelConfig(mode="advanced", model="gpt-4",
                                          base_url="https://per-agent.example.com",
                                          api_key="sk-test"),
                mcp_servers={}, on_event=AsyncMock(),
            )
        assert captured.get("ANTHROPIC_BASE_URL") == "https://per-agent.example.com"
        assert captured.get("ANTHROPIC_API_KEY") == "sk-test"

    @pytest.mark.asyncio
    async def test_run_passes_mcp_servers_to_options(self):
        captured_options = {}

        async def fake_query(**kwargs):
            captured_options.update({"mcp_servers": kwargs.get("options").mcp_servers})
            from claude_agent_sdk import ResultMessage
            mock = MagicMock(spec=ResultMessage)
            mock.result = ""
            yield mock

        mcp = {"my-server": {"type": "local", "command": "node", "args": ["srv.js"]}}
        with patch("app.engines.claude.query", fake_query):
            engine = ClaudeEngine(AppSettings())
            await engine.run(
                role="developer", prompt="x", system_prompt="sys", tools=[],
                model="sonnet", model_config=ModelConfig(),
                mcp_servers=mcp, on_event=AsyncMock(),
            )
        assert captured_options["mcp_servers"] == mcp


class TestCopilotEngine:
    def _make_mock_session(self, result_content="copilot result"):
        """Build a mock session matching the event-driven SDK API.

        The engine calls session.send() and subscribes via session.on().
        We simulate an 'assistant.message' followed by 'session.idle'.
        """
        session = MagicMock()
        session.disconnect = AsyncMock()
        session.send = AsyncMock()

        idle_event_type = MagicMock()
        idle_event_type.value = "session.idle"
        idle_event = MagicMock()
        idle_event.type = idle_event_type
        idle_event.data = MagicMock()

        msg_event_type = MagicMock()
        msg_event_type.value = "assistant.message"
        msg_event = MagicMock()
        msg_event.type = msg_event_type
        msg_event.data = MagicMock()
        msg_event.data.content = result_content

        def fake_on(handler):
            # Immediately dispatch events synchronously to simulate SDK behavior
            handler(msg_event)
            handler(idle_event)
            return lambda: None  # unsubscribe noop

        session.on = MagicMock(side_effect=fake_on)
        return session

    @pytest.mark.asyncio
    async def test_start_raises_if_sdk_missing(self):
        with patch("app.engines.copilot.CopilotClient", None):
            engine = CopilotEngine(AppSettings())
            with pytest.raises(SystemExit):
                await engine.start()

    @pytest.mark.asyncio
    async def test_run_returns_assistant_message_content(self):
        session = self._make_mock_session("feature implemented")
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        with patch("app.engines.copilot.CopilotClient", return_value=mock_client), \
             patch("app.engines.copilot.SubprocessConfig"):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            result = await engine.run(
                role="qa-engineer", prompt="test it", system_prompt="You are QA",
                tools=[], model="gpt-5.4", model_config=ModelConfig(),
                mcp_servers={}, on_event=AsyncMock(),
            )
            await engine.stop()

        assert result == "feature implemented"

    @pytest.mark.asyncio
    async def test_run_emits_agent_message_event(self):
        session = self._make_mock_session("result text")
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        events = []
        async def on_event(e): events.append(e)

        with patch("app.engines.copilot.CopilotClient", return_value=mock_client), \
             patch("app.engines.copilot.SubprocessConfig"):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            await engine.run(
                role="qa-engineer", prompt="test", system_prompt="sys",
                tools=[], model="gpt-5.4", model_config=ModelConfig(),
                mcp_servers={}, on_event=on_event,
            )

        agent_msgs = [e for e in events if e["type"] == "agent_message"]
        assert len(agent_msgs) == 1
        assert agent_msgs[0]["role"] == "qa-engineer"

    @pytest.mark.asyncio
    async def test_run_passes_mcp_servers_to_create_session(self):
        session = self._make_mock_session()
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        mcp = {"my-server": {"type": "local", "command": "python", "args": ["./s.py"], "tools": ["*"]}}

        with patch("app.engines.copilot.CopilotClient", return_value=mock_client), \
             patch("app.engines.copilot.SubprocessConfig"):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            await engine.run(
                role="build-agent", prompt="build", system_prompt="sys",
                tools=[], model="gpt-5-mini", model_config=ModelConfig(),
                mcp_servers=mcp, on_event=AsyncMock(),
            )

        call_cfg = mock_client.create_session.call_args.kwargs
        assert call_cfg["mcp_servers"] == mcp

    @pytest.mark.asyncio
    async def test_run_passes_provider_config_in_advanced_mode(self):
        session = self._make_mock_session()
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        mc = ModelConfig(mode="advanced", model="my-model", provider_type="openai",
                         base_url="https://custom.api.com", api_key="sk-custom")

        with patch("app.engines.copilot.CopilotClient", return_value=mock_client), \
             patch("app.engines.copilot.SubprocessConfig"):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            await engine.run(
                role="code-reviewer", prompt="review", system_prompt="sys",
                tools=[], model="my-model", model_config=mc,
                mcp_servers={}, on_event=AsyncMock(),
            )

        call_cfg = mock_client.create_session.call_args.kwargs
        assert call_cfg["provider"]["type"] == "openai"
        assert call_cfg["provider"]["base_url"] == "https://custom.api.com"
        assert call_cfg["provider"]["api_key"] == "sk-custom"


class TestOpenAIEngine:
    """Tests for the OpenAI-compatible engine (app/engines/openai_compat.py)."""

    def _make_settings(self, api_key="test-key", base_url="https://api.z.ai/api/paas/v4", model="glm-5v-turbo"):
        cfg = AgentEngineConfig(
            engine="openai",
            model_config=ModelConfig(mode="chat", model=model, base_url=base_url, api_key=api_key),
        )
        s = AppSettings()
        s.agent_configs = {r: cfg for r in ["lead-developer", "developer", "code-reviewer", "qa-engineer", "build-agent", "repo-manager"]}
        return s

    @pytest.mark.asyncio
    async def test_start_stop(self):
        engine = OpenAIEngine(self._make_settings())
        await engine.start()
        await engine.stop()

    @pytest.mark.asyncio
    async def test_run_no_tool_calls(self, monkeypatch):
        """Engine returns model text when no tool calls are made."""
        events = []

        class FakeMsg:
            content = "Hello from GLM"
            tool_calls = None

        class FakeChoice:
            message = FakeMsg()

        class FakeResponse:
            choices = [FakeChoice()]

        class FakeCompletions:
            async def create(self, **kwargs):
                return FakeResponse()

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        engine = OpenAIEngine(self._make_settings())

        monkeypatch.setattr("app.engines.openai_compat.AsyncOpenAI", lambda **kw: FakeClient())

        result = await engine.run(
            role="developer",
            prompt="Write hello world",
            system_prompt="You are a developer",
            tools=[],
            model="glm-5v-turbo",
            model_config=ModelConfig(mode="chat", model="glm-5v-turbo", base_url="https://api.z.ai/api/paas/v4", api_key="key"),
            mcp_servers={},
            on_event=lambda e: asyncio.sleep(0),
        )
        assert result == "Hello from GLM"

    @pytest.mark.asyncio
    async def test_run_with_tool_call_then_answer(self, monkeypatch):
        """Engine executes one tool call then returns final text."""
        import json as _json

        call_count = 0

        class FakeToolCall:
            id = "tc1"
            class function:
                name = "Read"
                arguments = _json.dumps({"file_path": "/tmp/nonexistent_test_file.txt"})

        class FakeMsgWithTool:
            content = None
            tool_calls = [FakeToolCall()]

        class FakeMsgFinal:
            content = "Done reading"
            tool_calls = None

        class FakeCompletions:
            async def create(self, **kwargs):
                nonlocal call_count
                call_count += 1

                class _R:
                    pass
                r = _R()
                r.choices = [type("C", (), {"message": FakeMsgWithTool() if call_count == 1 else FakeMsgFinal()})()]
                return r

        class FakeClient:
            class chat:
                completions = FakeCompletions()

        engine = OpenAIEngine(self._make_settings())
        monkeypatch.setattr("app.engines.openai_compat.AsyncOpenAI", lambda **kw: FakeClient())

        result = await engine.run(
            role="developer",
            prompt="Read a file",
            system_prompt="You are helpful",
            tools=["Read"],
            model="glm-5v-turbo",
            model_config=ModelConfig(mode="chat", model="glm-5v-turbo", base_url="https://api.z.ai/api/paas/v4", api_key="key"),
            mcp_servers={},
            on_event=lambda e: asyncio.sleep(0),
        )
        assert result == "Done reading"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_client_uses_api_key_from_model_config(self, monkeypatch):
        """_client() picks up api_key from ModelConfig."""
        captured = {}

        def fake_openai(**kwargs):
            captured.update(kwargs)
            class FakeCompletions:
                async def create(self, **kw):
                    raise RuntimeError("stop")
            class FakeChat:
                completions = FakeCompletions()
            class FakeClient:
                chat = FakeChat()
            return FakeClient()

        monkeypatch.setattr("app.engines.openai_compat.AsyncOpenAI", fake_openai)
        engine = OpenAIEngine(self._make_settings())
        mc = ModelConfig(mode="chat", model="glm-5v-turbo", base_url="https://api.z.ai/api/paas/v4", api_key="my-secret-key")
        try:
            await engine.run("developer", "hi", "sys", [], "glm-5v-turbo", mc, {}, lambda e: asyncio.sleep(0))
        except RuntimeError:
            pass
        assert captured.get("api_key") == "my-secret-key"
        assert captured.get("base_url") == "https://api.z.ai/api/paas/v4"

    def test_openai_engine_exported_from_app(self):
        """OpenAIEngine is importable from the top-level app package."""
        from app import OpenAIEngine  # noqa: F401
        assert OpenAIEngine is not None

    def test_settings_json_all_agents_use_openai_engine(self):
        """_settings_from_dict correctly parses an openai/glm-5v-turbo config for all 6 roles."""
        from app.models import _settings_from_dict
        agent_configs = {
            role: {
                "engine": "openai",
                "model_config": {
                    "mode": "chat",
                    "model": "glm-5v-turbo",
                    "base_url": "https://api.z.ai/api/coding/paas/v4",
                    "api_key": "test-key",
                },
            }
            for role in ["lead-developer", "developer", "code-reviewer", "qa-engineer", "build-agent", "repo-manager"]
        }
        settings = _settings_from_dict({"agent_configs": agent_configs})
        for role in agent_configs:
            cfg = settings.agent_configs[role]
            assert cfg.engine == "openai", f"{role} should use openai engine"
            assert cfg.model_config.model == "glm-5v-turbo"
            assert "coding/paas" in cfg.model_config.base_url


class TestHeadlessMode:
    """End-to-end headless pipeline run using MockEngine."""

    def _headless_settings(self, mode: str = "orchestrated") -> AppSettings:
        return AppSettings(headless=True, orchestration_mode=mode,
                           max_design_cycles=1, max_build_cycles=1,
                           max_impl_cycles=1, max_pr_cycles=1)

    def test_mock_engine_exported(self):
        from app import MockEngine
        assert MockEngine is not None

    def test_mock_engine_returns_canned_response(self):
        from app.engines.mock import MockEngine, _CANNED

        engine = MockEngine(AppSettings())
        events = []

        async def run():
            await engine.start()
            result = await engine.run(
                role="qa-engineer",
                prompt="Review this",
                system_prompt="sys",
                tools=[],
                model="mock",
                model_config=ModelConfig(),
                mcp_servers={},
                on_event=lambda e: asyncio.sleep(0),
            )
            await engine.stop()
            return result

        result = asyncio.run(run())
        assert "APPROVED" in result.upper()

    def test_mock_engine_build_agent_returns_success(self):
        from app.engines.mock import MockEngine

        engine = MockEngine(AppSettings())

        async def run():
            return await engine.run(
                role="build-agent", prompt="build", system_prompt="sys",
                tools=[], model="mock", model_config=ModelConfig(),
                mcp_servers={}, on_event=lambda e: asyncio.sleep(0),
            )

        result = asyncio.run(run())
        assert "SUCCESS" in result.upper()

    @pytest.mark.asyncio
    async def test_full_headless_pipeline_orchestrated(self):
        """Full orchestrated pipeline run in headless mode — all stages complete."""
        settings = self._headless_settings("orchestrated")
        events: list[dict] = []

        async def on_event(e: dict) -> None:
            events.append(e)

        result = await run_pipeline_orchestrated(
            task="Add a hello world endpoint",
            settings=settings,
            on_event=on_event,
        )

        assert "Pipeline complete" in result
        assert "PR" in result

        stage_events = [e["stage"] for e in events if e.get("type") == "stage_changed"]
        assert any("Stage 1" in s for s in stage_events), f"Stage 1 missing from {stage_events}"
        assert any("Stage 2" in s for s in stage_events), f"Stage 2 missing from {stage_events}"
        assert any("Stage 3" in s for s in stage_events), f"Stage 3 missing from {stage_events}"

        agent_roles = {e["role"] for e in events if e.get("type") == "agent_started"}
        assert "lead-developer" in agent_roles
        assert "developer" in agent_roles
        assert "repo-manager" in agent_roles

    @pytest.mark.asyncio
    async def test_full_headless_pipeline_skips_approval_gate(self):
        """In headless mode the approval gate callback is never invoked."""
        import asyncio as _asyncio
        settings = self._headless_settings("orchestrated")
        gate_called = False

        async def on_approval(plan: str) -> dict:
            nonlocal gate_called
            gate_called = True
            return {"approved": True, "feedback": ""}

        await run_pipeline_orchestrated(
            task="dummy task",
            settings=settings,
            on_event=lambda e: _asyncio.sleep(0),
            on_approval_required=on_approval,
        )
        assert not gate_called, "Approval gate should not be called in headless mode"

    def test_settings_headless_field_default_false(self):
        assert AppSettings().headless is False

    def test_settings_headless_field_serialises(self):
        from dataclasses import asdict
        d = asdict(AppSettings(headless=True))
        assert d["headless"] is True

    def test_settings_z_ai_coding_endpoint(self):
        """OpenAIEngine._client() correctly uses z.ai coding/paas endpoint from model_config."""
        captured = {}

        def fake_openai(**kw):
            captured.update(kw)
            class _C:
                class chat:
                    class completions:
                        async def create(self, **k):
                            raise RuntimeError("stop")
            return _C()

        import unittest.mock as mock
        with mock.patch("app.engines.openai_compat.AsyncOpenAI", fake_openai):
            engine = OpenAIEngine(AppSettings())
            mc = ModelConfig(
                mode="chat",
                model="glm-5v-turbo",
                base_url="https://api.z.ai/api/coding/paas/v4",
                api_key="test-key",
            )
            client = engine._client(mc)

        assert captured.get("base_url") == "https://api.z.ai/api/coding/paas/v4"
        assert captured.get("api_key") == "test-key"


class TestEngineRegistry:
    """Tests for ENGINE_REGISTRY and unified model resolution."""

    def test_registry_contains_all_engine_names(self):
        from app.engines import ENGINE_REGISTRY
        expected = {"claude", "copilot", "codex", "openai", "mock"}
        assert set(ENGINE_REGISTRY.keys()) == expected

    def test_registry_maps_to_correct_classes(self):
        from app.engines import ENGINE_REGISTRY, ClaudeEngine, CopilotEngine, CodexEngine, OpenAIEngine, MockEngine
        assert ENGINE_REGISTRY["claude"] is ClaudeEngine
        assert ENGINE_REGISTRY["copilot"] is CopilotEngine
        assert ENGINE_REGISTRY["codex"] is CodexEngine
        assert ENGINE_REGISTRY["openai"] is OpenAIEngine
        assert ENGINE_REGISTRY["mock"] is MockEngine

    def test_resolve_default_model_claude_uses_settings_attribute(self):
        from app.models import _resolve_default_model, AppSettings
        s = AppSettings(opus_model="my-opus")
        model = _resolve_default_model("lead-developer", "claude", s)
        assert model == "my-opus"

    def test_resolve_default_model_copilot_uses_literal(self):
        from app.models import _resolve_default_model, AppSettings
        model = _resolve_default_model("code-reviewer", "copilot", AppSettings())
        assert model == "codex"

    def test_resolve_default_model_openai_uses_literal(self):
        from app.models import _resolve_default_model, AppSettings
        model = _resolve_default_model("qa-engineer", "openai", AppSettings())
        assert model == "gpt-5.4"

    def test_resolve_default_model_unknown_engine_returns_empty(self):
        from app.models import _resolve_default_model, AppSettings
        model = _resolve_default_model("lead-developer", "nonexistent", AppSettings())
        assert model == ""

    def test_resolve_default_model_unknown_role_returns_empty(self):
        from app.models import _resolve_default_model, AppSettings
        model = _resolve_default_model("nonexistent-role", "claude", AppSettings())
        assert model == ""

    def test_get_default_engine_config_uses_unified_map(self):
        from app.models import get_default_engine_config, AppSettings
        # Default for lead-developer is "claude" engine
        cfg = get_default_engine_config("lead-developer", AppSettings())
        assert cfg.engine == "claude"
        assert cfg.model_config.model == AppSettings().opus_model

    def test_get_default_engine_config_for_copilot_default_role(self):
        from app.models import get_default_engine_config, AppSettings
        # Default for code-reviewer is "copilot"
        cfg = get_default_engine_config("code-reviewer", AppSettings())
        assert cfg.engine == "copilot"
        assert cfg.model_config.model == "codex"

    def test_legacy_claude_tier_compat(self):
        """_CLAUDE_TIER still works as a legacy alias."""
        from app.constants import _CLAUDE_TIER
        assert _CLAUDE_TIER["lead-developer"] == "opus_model"
        assert _CLAUDE_TIER["developer"] == "sonnet_model"
        assert _CLAUDE_TIER["build-agent"] == "haiku_model"

    def test_legacy_copilot_defaults_compat(self):
        """_COPILOT_DEFAULTS still works as a legacy alias."""
        from app.constants import _COPILOT_DEFAULTS
        assert _COPILOT_DEFAULTS["code-reviewer"] == "codex"
        assert _COPILOT_DEFAULTS["qa-engineer"] == "gpt-5.4"

    def test_engine_default_models_has_all_roles(self):
        from app.constants import _ENGINE_DEFAULT_MODELS
        roles = {"lead-developer", "developer", "code-reviewer", "qa-engineer", "build-agent", "repo-manager"}
        assert set(_ENGINE_DEFAULT_MODELS.keys()) == roles

    def test_engine_default_models_has_all_engines_per_role(self):
        from app.constants import _ENGINE_DEFAULT_MODELS
        engines = {"claude", "copilot", "codex", "openai"}
        for role, model_map in _ENGINE_DEFAULT_MODELS.items():
            assert set(model_map.keys()) == engines, f"{role} missing engines: {engines - set(model_map.keys())}"
