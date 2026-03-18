# Per-Agent Engine & Model Selection Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-agent engine (Claude Agent SDK / GitHub Copilot SDK) and model selection with simple/advanced model config, configurable from the web UI.

**Architecture:** Two engine classes (`ClaudeEngine`, `CopilotEngine`) implement a shared `AgentEngine` Protocol. The `call()` helper in `run_pipeline_orchestrated` resolves per-agent config from `AppSettings.agent_configs` (falling back to smart defaults) and delegates to the appropriate engine. UI adds an Agents section to Settings with per-agent engine toggle and model config.

**Tech Stack:** Python 3.11+, `claude_agent_sdk` (existing), `github-copilot-sdk` (new), `pytest` + `pytest-asyncio` (existing), Tailwind CSS + Alpine.js (existing).

**Spec:** `docs/superpowers/specs/2026-03-16-sdk-selection-design.md`

---

## File Map

| File | Change |
|---|---|
| `main.py` | Add `ModelConfig`, `AgentEngineConfig` dataclasses; add fields to `AppSettings`; add `get_default_engine_config`, `_settings_from_dict`, `_agent_engine_config_from_dict`; add `AgentEngine` Protocol, `ClaudeEngine`, `CopilotEngine`; refactor `run_pipeline_orchestrated.call()`; update `/api/settings` and `/api/team` handlers |
| `test_main.py` | Add `TestModelConfig`, `TestAgentEngineConfig`, `TestGetDefaultEngineConfig`, `TestSettingsFromDict`, `TestClaudeEngine`, `TestCopilotEngine`, `TestOrchestrationEngineRouting`; extend `TestAppSettings`, `TestApiSettings`, `TestApiTeam` |
| `requirements.txt` | Add `github-copilot-sdk` |
| `static/index.html` | Add Agents section + Copilot Connection to Settings tab; add engine badge to Team tab agent cards |

---

## Chunk 1: Data Model, Smart Defaults & Validation

### Task 1: Add `ModelConfig` and `AgentEngineConfig` dataclasses

**Files:**
- Modify: `main.py` — after `AppSettings` dataclass (~line 86)
- Modify: `test_main.py` — add two test classes

- [ ] **Step 1: Write failing tests**

Extend the existing `from main import (...)` block in `test_main.py` with `ModelConfig, AgentEngineConfig`. Do **not** add a new `from main import` line.

Add after `TestAppSettings`:
```python
class TestModelConfig:
    def test_defaults(self):
        m = ModelConfig()
        assert m.mode == "simple"
        assert m.model == ""
        assert m.provider_type == "openai"
        assert m.base_url == ""
        assert m.api_key == ""
        assert m.extra_params == {}

    def test_advanced_mode(self):
        m = ModelConfig(mode="advanced", model="gpt-4", base_url="https://api.example.com", api_key="sk-test")
        assert m.mode == "advanced"
        assert m.model == "gpt-4"
        assert m.base_url == "https://api.example.com"

    def test_extra_params_default_independent(self):
        m1 = ModelConfig()
        m2 = ModelConfig()
        m1.extra_params["key"] = "val"
        assert m2.extra_params == {}


class TestAgentEngineConfig:
    def test_defaults(self):
        c = AgentEngineConfig()
        assert c.engine == "claude"
        assert isinstance(c.model_config, ModelConfig)
        assert c.mcp_servers == {}

    def test_copilot_engine(self):
        c = AgentEngineConfig(engine="copilot")
        assert c.engine == "copilot"

    def test_mcp_servers_default_independent(self):
        c1 = AgentEngineConfig()
        c2 = AgentEngineConfig()
        c1.mcp_servers["test"] = {}
        assert c2.mcp_servers == {}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/anil/Source/OLamo && source venv/bin/activate && pytest test_main.py::TestModelConfig test_main.py::TestAgentEngineConfig -v 2>&1 | tail -5
```
Expected: `ImportError` — `ModelConfig` not defined yet

- [ ] **Step 3: Add `ModelConfig` and `AgentEngineConfig` to `main.py`**

In `main.py`, insert after the `AppSettings` dataclass (after line 86), before the `# System prompts` section:

```python
@dataclass
class ModelConfig:
    mode: str = "simple"           # "simple" | "advanced"
    model: str = ""                # model name; "" = use smart default
    provider_type: str = "openai"  # "openai" | "azure" | "anthropic"
    base_url: str = ""
    api_key: str = ""
    extra_params: dict = field(default_factory=dict)


@dataclass
class AgentEngineConfig:
    engine: str = "claude"         # "claude" | "copilot"
    model_config: ModelConfig = field(default_factory=ModelConfig)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest test_main.py::TestModelConfig test_main.py::TestAgentEngineConfig -v 2>&1 | tail -5
```
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: add ModelConfig and AgentEngineConfig dataclasses"
```

---

### Task 2: Update `AppSettings` with new fields and `__post_init__` validation

**Files:**
- Modify: `main.py` — `AppSettings` dataclass
- Modify: `test_main.py` — extend `TestAppSettings`

- [ ] **Step 1: Write failing tests**

Add to `TestAppSettings`:
```python
    def test_agent_configs_defaults_empty(self):
        assert AppSettings().agent_configs == {}

    def test_copilot_github_token_defaults_empty(self):
        assert AppSettings().copilot_github_token == ""

    def test_post_init_raises_on_advanced_with_no_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            AppSettings(agent_configs={
                "developer": AgentEngineConfig(
                    model_config=ModelConfig(mode="advanced", model="gpt-4", base_url="")
                )
            })

    def test_post_init_ok_with_advanced_and_base_url(self):
        s = AppSettings(agent_configs={
            "developer": AgentEngineConfig(
                model_config=ModelConfig(mode="advanced", model="gpt-4",
                                         base_url="https://api.example.com")
            )
        })
        assert s.agent_configs["developer"].engine == "claude"

    def test_asdict_includes_agent_configs(self):
        s = AppSettings(agent_configs={"developer": AgentEngineConfig(engine="copilot")})
        d = asdict(s)
        assert d["agent_configs"]["developer"]["engine"] == "copilot"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest test_main.py::TestAppSettings -v 2>&1 | tail -10
```
Expected: `AttributeError` — `agent_configs` not on `AppSettings`

- [ ] **Step 3: Update `AppSettings` in `main.py`**

Add two fields and `__post_init__` to the existing `AppSettings` dataclass:
```python
    agent_configs: dict[str, AgentEngineConfig] = field(default_factory=dict)
    copilot_github_token: str = ""

    def __post_init__(self) -> None:
        for role, cfg in self.agent_configs.items():
            if cfg.model_config.mode == "advanced" and not cfg.model_config.base_url:
                raise ValueError(
                    f"Agent '{role}': advanced model config requires base_url"
                )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest test_main.py::TestAppSettings -v 2>&1 | tail -10
```
Expected: all `TestAppSettings` tests pass

- [ ] **Step 5: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: add agent_configs and copilot_github_token to AppSettings"
```

---

### Task 3: Add `get_default_engine_config()` and `_settings_from_dict()` helpers

**Files:**
- Modify: `main.py` — add helpers after `AppSettings`
- Modify: `test_main.py` — add `TestGetDefaultEngineConfig`, `TestSettingsFromDict`

- [ ] **Step 1: Write failing tests**

Extend the existing `from main import (...)` block in `test_main.py` with `get_default_engine_config, _settings_from_dict`. Do **not** add a new `from main import` line.

Add test classes:
```python
class TestGetDefaultEngineConfig:
    def test_lead_developer_defaults_to_claude(self):
        assert get_default_engine_config("lead-developer", AppSettings()).engine == "claude"

    def test_developer_defaults_to_claude(self):
        assert get_default_engine_config("developer", AppSettings()).engine == "claude"

    def test_code_reviewer_defaults_to_copilot(self):
        assert get_default_engine_config("code-reviewer", AppSettings()).engine == "copilot"

    def test_qa_engineer_defaults_to_copilot(self):
        assert get_default_engine_config("qa-engineer", AppSettings()).engine == "copilot"

    def test_build_agent_defaults_to_copilot(self):
        assert get_default_engine_config("build-agent", AppSettings()).engine == "copilot"

    def test_repo_manager_defaults_to_copilot(self):
        assert get_default_engine_config("repo-manager", AppSettings()).engine == "copilot"

    def test_lead_developer_claude_model_resolves_from_settings(self):
        cfg = get_default_engine_config("lead-developer", AppSettings(opus_model="my-opus"))
        assert cfg.model_config.model == "my-opus"

    def test_developer_claude_model_resolves_from_settings(self):
        cfg = get_default_engine_config("developer", AppSettings(sonnet_model="my-sonnet"))
        assert cfg.model_config.model == "my-sonnet"

    def test_code_reviewer_copilot_model_is_codex(self):
        assert get_default_engine_config("code-reviewer", AppSettings()).model_config.model == "codex"

    def test_qa_engineer_copilot_model(self):
        assert get_default_engine_config("qa-engineer", AppSettings()).model_config.model == "gpt-5.4"

    def test_build_agent_copilot_model(self):
        assert get_default_engine_config("build-agent", AppSettings()).model_config.model == "gpt-5-mini"

    def test_repo_manager_copilot_model(self):
        assert get_default_engine_config("repo-manager", AppSettings()).model_config.model == "gpt-5-mini"

    def test_all_six_roles_covered(self):
        roles = ["lead-developer", "developer", "code-reviewer", "qa-engineer", "build-agent", "repo-manager"]
        for role in roles:
            cfg = get_default_engine_config(role, AppSettings())
            assert cfg.engine in ("claude", "copilot")
            assert cfg.model_config.model != ""


class TestSettingsFromDict:
    def test_plain_settings_round_trips(self):
        s = AppSettings(pm_model="opus")
        restored = _settings_from_dict(asdict(s))
        assert restored.pm_model == "opus"
        assert isinstance(restored, AppSettings)

    def test_agent_configs_deserialized_as_dataclasses(self):
        d = asdict(AppSettings(agent_configs={
            "developer": AgentEngineConfig(
                engine="copilot",
                model_config=ModelConfig(mode="simple", model="gpt-5")
            )
        }))
        s = _settings_from_dict(d)
        assert isinstance(s.agent_configs["developer"], AgentEngineConfig)
        assert isinstance(s.agent_configs["developer"].model_config, ModelConfig)
        assert s.agent_configs["developer"].engine == "copilot"
        assert s.agent_configs["developer"].model_config.model == "gpt-5"

    def test_missing_agent_configs_defaults_to_empty(self):
        d = asdict(AppSettings())
        d.pop("agent_configs")
        s = _settings_from_dict(d)
        assert s.agent_configs == {}

    def test_does_not_mutate_input(self):
        d = asdict(AppSettings())
        original_keys = set(d.keys())
        _settings_from_dict(d)
        assert set(d.keys()) == original_keys
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest test_main.py::TestGetDefaultEngineConfig test_main.py::TestSettingsFromDict -v 2>&1 | tail -5
```
Expected: `ImportError`

- [ ] **Step 3: Add helpers to `main.py`**

After `AppSettings` (before `# System prompts` comment), add:

```python
# ---------------------------------------------------------------------------
# Smart defaults and settings helpers
# ---------------------------------------------------------------------------

_COPILOT_DEFAULTS: dict[str, str] = {
    "lead-developer": "claude-opus-4-6",
    "developer":      "claude-sonnet-4-6",
    "code-reviewer":  "codex",
    "qa-engineer":    "gpt-5.4",
    "build-agent":    "gpt-5-mini",
    "repo-manager":   "gpt-5-mini",
}

_DEFAULT_ENGINES: dict[str, str] = {
    "lead-developer": "claude",
    "developer":      "claude",
    "code-reviewer":  "copilot",
    "qa-engineer":    "copilot",
    "build-agent":    "copilot",
    "repo-manager":   "copilot",
}

_CLAUDE_TIER: dict[str, str] = {
    "lead-developer": "opus_model",
    "developer":      "sonnet_model",
    "code-reviewer":  "opus_model",
    "qa-engineer":    "opus_model",
    "build-agent":    "haiku_model",
    "repo-manager":   "haiku_model",
}


def get_default_engine_config(role: str, settings: AppSettings) -> AgentEngineConfig:
    """Return the smart-default AgentEngineConfig for a given role."""
    engine = _DEFAULT_ENGINES.get(role, "claude")
    if engine == "copilot":
        model = _COPILOT_DEFAULTS.get(role, "")
    else:
        tier_field = _CLAUDE_TIER.get(role, "sonnet_model")
        model = getattr(settings, tier_field)
    return AgentEngineConfig(engine=engine, model_config=ModelConfig(model=model))


def _agent_engine_config_from_dict(d: dict) -> AgentEngineConfig:
    mc = d.get("model_config") or {}
    return AgentEngineConfig(
        engine=d.get("engine", "claude"),
        model_config=ModelConfig(**mc) if mc else ModelConfig(),
        mcp_servers=d.get("mcp_servers") or {},
    )


def _settings_from_dict(d: dict) -> AppSettings:
    """Reconstruct AppSettings from a plain dict (e.g. from JSON API body)."""
    d = dict(d)  # shallow copy — do not mutate caller's dict
    agent_configs_raw = d.pop("agent_configs", None) or {}
    filtered = {k: v for k, v in d.items() if k in AppSettings.__dataclass_fields__}
    agent_configs = {
        role: _agent_engine_config_from_dict(cfg) if isinstance(cfg, dict) else cfg
        for role, cfg in agent_configs_raw.items()
    }
    return AppSettings(**filtered, agent_configs=agent_configs)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest test_main.py::TestGetDefaultEngineConfig test_main.py::TestSettingsFromDict -v 2>&1 | tail -10
```
Expected: all pass

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
pytest test_main.py -v 2>&1 | tail -5
```
Expected: all previously-passing tests still pass

- [ ] **Step 6: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: add get_default_engine_config and _settings_from_dict helpers"
```

---

## Chunk 2: Engine Abstraction

### Task 4: Define `AgentEngine` Protocol and implement `ClaudeEngine`

**Files:**
- Modify: `main.py` — add Protocol and `ClaudeEngine` class after the smart defaults section
- Modify: `test_main.py` — add `TestClaudeEngine`

- [ ] **Step 1: Write failing tests**

Add to `test_main.py` imports:
```python
from unittest.mock import AsyncMock, MagicMock, patch
from main import ClaudeEngine
```

Add test class:
```python
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

        with patch("main.query", fake_query):
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

        with patch("main.query", fake_query):
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

        with patch("main.query", fake_query):
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
        with patch("main.query", fake_query):
            engine = ClaudeEngine(AppSettings())
            await engine.run(
                role="developer", prompt="x", system_prompt="sys", tools=[],
                model="sonnet", model_config=ModelConfig(),
                mcp_servers=mcp, on_event=AsyncMock(),
            )
        assert captured_options["mcp_servers"] == mcp
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest test_main.py::TestClaudeEngine -v 2>&1 | tail -5
```
Expected: `ImportError` — `ClaudeEngine` not defined

- [ ] **Step 3: Add `AgentEngine` Protocol and `ClaudeEngine` to `main.py`**

Add `import os` at the top of `main.py` (with other stdlib imports).

After the smart defaults section, add:

```python
# ---------------------------------------------------------------------------
# Engine abstraction
# ---------------------------------------------------------------------------

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
    ) -> str: ...


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
        if model_config.mode == "advanced":
            if model_config.base_url:
                env["ANTHROPIC_BASE_URL"] = model_config.base_url
            if model_config.api_key:
                env["ANTHROPIC_API_KEY"] = model_config.api_key

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
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest test_main.py::TestClaudeEngine -v 2>&1 | tail -10
```
Expected: `5 passed`

- [ ] **Step 5: Run full suite**

```bash
pytest test_main.py -v 2>&1 | tail -5
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: add AgentEngine Protocol and ClaudeEngine"
```

---

### Task 5: Implement `CopilotEngine`

**Files:**
- Modify: `main.py` — add `CopilotEngine` class after `ClaudeEngine`
- Modify: `test_main.py` — add `TestCopilotEngine`
- Modify: `requirements.txt` — add `github-copilot-sdk`

- [ ] **Step 1: Add `github-copilot-sdk` to `requirements.txt`**

Add to `requirements.txt`:
```
github-copilot-sdk
```

- [ ] **Step 2: Write failing tests for `CopilotEngine`**

Add to `test_main.py` imports:
```python
from main import CopilotEngine
```

Add test class:
```python
class TestCopilotEngine:
    def _make_mock_session(self, result_content="copilot result"):
        """Build a mock session that fires assistant.message then session.idle."""
        session = MagicMock()
        handlers = []
        session.on = lambda h: handlers.append(h)
        session.disconnect = AsyncMock()

        async def fake_send(prompt):
            msg_evt = MagicMock()
            msg_evt.type.value = "assistant.message"
            msg_evt.data.content = result_content
            for h in handlers:
                h(msg_evt)
            idle_evt = MagicMock()
            idle_evt.type.value = "session.idle"
            for h in handlers:
                h(idle_evt)

        session.send = fake_send
        return session

    @pytest.mark.asyncio
    async def test_start_raises_if_sdk_missing(self):
        with patch("main.CopilotClient", None):
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

        with patch("main.CopilotClient", return_value=mock_client):
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

        with patch("main.CopilotClient", return_value=mock_client):
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

        with patch("main.CopilotClient", return_value=mock_client):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            await engine.run(
                role="build-agent", prompt="build", system_prompt="sys",
                tools=[], model="gpt-5-mini", model_config=ModelConfig(),
                mcp_servers=mcp, on_event=AsyncMock(),
            )

        call_cfg = mock_client.create_session.call_args[0][0]
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

        with patch("main.CopilotClient", return_value=mock_client):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            await engine.run(
                role="code-reviewer", prompt="review", system_prompt="sys",
                tools=[], model="my-model", model_config=mc,
                mcp_servers={}, on_event=AsyncMock(),
            )

        call_cfg = mock_client.create_session.call_args[0][0]
        assert call_cfg["provider"]["type"] == "openai"
        assert call_cfg["provider"]["base_url"] == "https://custom.api.com"
        assert call_cfg["provider"]["api_key"] == "sk-custom"
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
pytest test_main.py::TestCopilotEngine -v 2>&1 | tail -5
```
Expected: `ImportError` — `CopilotEngine` not defined

- [ ] **Step 4: Add Copilot SDK import to `main.py`**

At the top of `main.py`, after the `aiosqlite` try/except block, add:

```python
try:
    from copilot import CopilotClient, SubprocessConfig
except ImportError:
    CopilotClient = None   # type: ignore — only required when Copilot engine is used
    SubprocessConfig = None  # type: ignore
```

- [ ] **Step 5: Add `CopilotEngine` class to `main.py`**

After `ClaudeEngine`, add:

```python
class CopilotEngine:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client = None

    async def start(self) -> None:
        if CopilotClient is None:
            raise SystemExit(
                "github-copilot-sdk not installed and/or Copilot CLI not found.\n"
                "Install SDK:  pip install github-copilot-sdk\n"
                "Install CLI:  https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli"
            )
        token = (
            self._settings.copilot_github_token
            or os.environ.get("COPILOT_GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
        )
        if not token:
            raise RuntimeError(
                "Copilot engine requires a GitHub token. "
                "Set copilot_github_token in settings or one of the env vars: "
                "COPILOT_GITHUB_TOKEN, GH_TOKEN, GITHUB_TOKEN"
            )
        self._client = CopilotClient(SubprocessConfig(github_token=token))
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
        session_cfg: dict = {
            "model": model,
            "system_message": {"role": "system", "content": system_prompt},
        }
        if mcp_servers:
            session_cfg["mcp_servers"] = mcp_servers
        if model_config.mode == "advanced" and model_config.base_url:
            session_cfg["provider"] = {
                "type": model_config.provider_type,
                "base_url": model_config.base_url,
                "api_key": model_config.api_key,
                **model_config.extra_params,
            }

        try:
            session = await self._client.create_session(session_cfg)
        except Exception as e:
            raise RuntimeError(f"CopilotEngine: failed to create session for '{role}': {e}") from e

        result = ""
        done = asyncio.Event()

        def _on_event(event) -> None:
            nonlocal result
            etype = event.type.value if hasattr(event.type, "value") else str(event.type)
            if etype == "assistant.message":
                result = str(getattr(event.data, "content", ""))
            elif etype == "session.idle":
                done.set()

        session.on(_on_event)
        try:
            await session.send(prompt)
            await done.wait()
        finally:
            await session.disconnect()

        await on_event({"type": "agent_message", "role": role, "text": result[:300]})
        return result
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
pytest test_main.py::TestCopilotEngine -v 2>&1 | tail -10
```
Expected: `5 passed`

- [ ] **Step 7: Run full suite**

```bash
pytest test_main.py -v 2>&1 | tail -5
```
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add main.py test_main.py requirements.txt
git commit -m "feat: add CopilotEngine wrapping github-copilot-sdk"
```

---

## Chunk 3: Pipeline Integration & API Changes

### Task 6: Refactor `run_pipeline_orchestrated` to use engines

**Files:**
- Modify: `main.py` — replace `call()` closure, add engine lifecycle
- Modify: `test_main.py` — add `TestOrchestrationEngineRouting`

The existing `call()` closure inside `run_pipeline_orchestrated` directly calls `ClaudeAgentOptions` and `query`. We replace it with engine-aware routing. The pipeline stage logic itself is unchanged.

- [ ] **Step 1: Write a routing smoke test**

Extend the existing `from main import (...)` block in `test_main.py` with `run_pipeline_orchestrated`. Do **not** add a new `from main import` line.

Add test class:
```python
class TestOrchestrationEngineRouting:
    @pytest.mark.asyncio
    async def test_claude_engine_agents_invoke_query(self):
        """Agents configured for claude engine go through ClaudeEngine (query())."""
        from claude_agent_sdk import ResultMessage
        query_calls = []

        async def fake_query(**kwargs):
            query_calls.append(kwargs)
            mock = MagicMock(spec=ResultMessage)
            mock.result = "APPROVED"
            yield mock

        settings = AppSettings(
            orchestration_mode="orchestrated",
            max_design_cycles=1,
            max_impl_cycles=1,
            max_build_cycles=1,
            max_pr_cycles=1,
            agent_configs={role: AgentEngineConfig(engine="claude")
                           for role in ["lead-developer", "developer", "code-reviewer",
                                        "qa-engineer", "build-agent", "repo-manager"]},
        )

        with patch("main.query", fake_query):
            events = []
            on_event = AsyncMock(side_effect=lambda e: events.append(e))
            await run_pipeline_orchestrated(
                task="add hello world",
                settings=settings,
                on_event=on_event,
            )

        # With max_design_cycles=1, max_impl_cycles=1, max_build_cycles=1 the pipeline
        # runs at minimum: lead-dev plan, qa review, developer impl, build-agent, code-reviewer,
        # qa-engineer, lead-dev review, repo-manager = 8+ calls
        assert len(query_calls) >= 8

    @pytest.mark.asyncio
    async def test_copilot_engine_agents_invoke_copilot_client(self):
        """Agents configured for copilot engine go through CopilotEngine."""
        session = MagicMock()
        handlers = []
        session.on = lambda h: handlers.append(h)
        session.disconnect = AsyncMock()

        async def fake_send(prompt):
            msg_evt = MagicMock()
            msg_evt.type.value = "assistant.message"
            msg_evt.data.content = "APPROVED"
            for h in handlers: h(msg_evt)
            idle_evt = MagicMock()
            idle_evt.type.value = "session.idle"
            for h in handlers: h(idle_evt)

        session.send = fake_send

        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        settings = AppSettings(
            orchestration_mode="orchestrated",
            copilot_github_token="gh-tok",
            max_design_cycles=1,
            max_impl_cycles=1,
            max_build_cycles=1,
            max_pr_cycles=1,
            agent_configs={role: AgentEngineConfig(engine="copilot",
                                                    model_config=ModelConfig(model="gpt-5"))
                           for role in ["lead-developer", "developer", "code-reviewer",
                                        "qa-engineer", "build-agent", "repo-manager"]},
        )

        with patch("main.CopilotClient", return_value=mock_client):
            events = []
            on_event = AsyncMock(side_effect=lambda e: events.append(e))
            await run_pipeline_orchestrated(
                task="add hello world",
                settings=settings,
                on_event=on_event,
            )

        # start() called once, stop() called once, create_session() called ≥8 times
        assert mock_client.start.call_count == 1
        assert mock_client.stop.call_count == 1
        assert mock_client.create_session.call_count >= 8
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest test_main.py::TestOrchestrationEngineRouting -v 2>&1 | tail -5
```
Expected: FAIL — old `call()` doesn't use engine abstraction

- [ ] **Step 3: Refactor `run_pipeline_orchestrated` in `main.py`**

At the start of `run_pipeline_orchestrated` (after the function signature), replace the `env = _make_env(settings)` line and the existing `call()` closure with:

```python
    # Build engine instances
    uses_copilot = any(
        (settings.agent_configs.get(r) or get_default_engine_config(r, settings)).engine == "copilot"
        for r in AGENT_CONFIGS
    )
    claude_engine: AgentEngine = ClaudeEngine(settings)
    copilot_engine: AgentEngine | None = CopilotEngine(settings) if uses_copilot else None

    await claude_engine.start()
    if copilot_engine:
        await copilot_engine.start()

    def _resolve(role: str) -> tuple[AgentEngine, str, ModelConfig, dict]:
        cfg = settings.agent_configs.get(role) or get_default_engine_config(role, settings)
        eng = copilot_engine if cfg.engine == "copilot" and copilot_engine else claude_engine
        model = cfg.model_config.model or (
            _COPILOT_DEFAULTS.get(role, "") if cfg.engine == "copilot"
            else getattr(settings, _CLAUDE_TIER.get(role, "sonnet_model"))
        )
        return eng, model, cfg.model_config, cfg.mcp_servers

    async def call(role: str, prompt: str) -> str:
        await on_event({"type": "agent_started", "role": role})
        system_prompt, tools, _ = AGENT_CONFIGS[role]
        eng, model, model_config, mcp_servers = _resolve(role)
        try:
            return await eng.run(
                role=role,
                prompt=prompt,
                system_prompt=system_prompt,
                tools=tools,
                model=model,
                model_config=model_config,
                mcp_servers=mcp_servers,
                on_event=on_event,
            )
        except Exception as e:
            raise RuntimeError(f"Agent '{role}' failed: {e}") from e
```

Then wrap the entire pipeline body (from `plan = task` to `return ...`) in a `try/finally`:

```python
    try:
        plan = task
        # ... all existing pipeline stage code unchanged ...
        return f"Pipeline complete. PR: {pr_result[:200]}"
    finally:
        await claude_engine.stop()
        if copilot_engine:
            await copilot_engine.stop()
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest test_main.py::TestOrchestrationEngineRouting -v 2>&1 | tail -5
```
Expected: `2 passed`

- [ ] **Step 5: Run full suite**

```bash
pytest test_main.py -v 2>&1 | tail -10
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: refactor run_pipeline_orchestrated to route through engine abstraction"
```

---

### Task 7: Update API endpoints

**Files:**
- Modify: `main.py` — `PUT /api/settings`, `GET /api/team`
- Modify: `test_main.py` — extend `TestApiSettings`, `TestApiTeam`

- [ ] **Step 1: Write failing tests**

Add to `TestApiSettings`:
```python
    def test_get_settings_includes_agent_configs(self, client):
        assert "agent_configs" in client.get("/api/settings").json()["config"]

    def test_get_settings_includes_copilot_github_token(self, client):
        assert "copilot_github_token" in client.get("/api/settings").json()["config"]

    def test_put_settings_accepts_agent_configs(self, client):
        payload = {"agent_configs": {"developer": {
            "engine": "copilot",
            "model_config": {"mode": "simple", "model": "gpt-5",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {}},
            "mcp_servers": {}
        }}}
        resp = client.put("/api/settings", json=payload)
        assert resp.status_code == 200
        assert resp.json()["config"]["agent_configs"]["developer"]["engine"] == "copilot"

    def test_put_settings_advanced_missing_base_url_returns_422(self, client):
        payload = {"agent_configs": {"developer": {
            "engine": "claude",
            "model_config": {"mode": "advanced", "model": "gpt-4",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {}},
            "mcp_servers": {}
        }}}
        resp = client.put("/api/settings", json=payload)
        assert resp.status_code == 422
```

Add to `TestApiTeam`:
```python
    def test_each_agent_has_engine_field(self, client):
        for agent in client.get("/api/team").json()["agents"]:
            assert agent["engine"] in ("claude", "copilot"), f"{agent['role']} bad engine"

    def test_each_agent_has_config_mode_field(self, client):
        for agent in client.get("/api/team").json()["agents"]:
            assert agent["config_mode"] in ("simple", "advanced"), f"{agent['role']} bad config_mode"

    def test_default_engines_match_smart_defaults(self, client):
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        assert agents["lead-developer"]["engine"] == "claude"
        assert agents["developer"]["engine"] == "claude"
        assert agents["code-reviewer"]["engine"] == "copilot"
        assert agents["qa-engineer"]["engine"] == "copilot"
        assert agents["build-agent"]["engine"] == "copilot"
        assert agents["repo-manager"]["engine"] == "copilot"

    def test_copilot_default_agents_report_copilot_model(self, client):
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        # Smart default models for copilot-engine agents
        assert agents["code-reviewer"]["model"] == "codex"
        assert agents["qa-engineer"]["model"] == "gpt-5.4"
        assert agents["build-agent"]["model"] == "gpt-5-mini"
        assert agents["repo-manager"]["model"] == "gpt-5-mini"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest test_main.py::TestApiSettings test_main.py::TestApiTeam -v 2>&1 | tail -10
```
Expected: failures on the new tests

- [ ] **Step 3: Update `PUT /api/settings` handler in `main.py`**

Replace the body of `update_settings`:
```python
    @app.put("/api/settings")
    async def update_settings(request: Request) -> dict:
        body = await request.json()
        try:
            current = asdict(store.settings)
            merged = {**current, **{k: v for k, v in body.items()
                                    if k in AppSettings.__dataclass_fields__}}
            new_settings = _settings_from_dict(merged)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        applied = await store.try_update(new_settings)
        return {"applied": applied, "config": asdict(store.settings)}
```

- [ ] **Step 4: Update `GET /api/team` handler in `main.py`**

Replace the body of `team()`:
```python
    @app.get("/api/team")
    async def team() -> dict:
        s = store.settings
        agents = build_agents(s)
        agent_list = []
        for role, defn in agents.items():
            cfg = s.agent_configs.get(role) or get_default_engine_config(role, s)
            # Resolve the effective model: explicit config > engine smart default
            if cfg.model_config.model:
                model = cfg.model_config.model
            elif cfg.engine == "copilot":
                model = _COPILOT_DEFAULTS.get(role, "")
            else:
                model = getattr(s, _CLAUDE_TIER.get(role, "sonnet_model"), "")
            agent_list.append({
                "role": role,
                "model": model,
                "description": defn.description,
                "engine": cfg.engine,
                "config_mode": cfg.model_config.mode,
            })
        return {
            "agents": agent_list,
            "pipeline": ["Design Loop", "Implementation Loop", "Commit & PR", "PR Poll"],
            "cycle_limits": {
                "max_design_cycles": s.max_design_cycles,
                "max_build_cycles": s.max_build_cycles,
                "max_impl_cycles": s.max_impl_cycles,
                "max_pr_cycles": s.max_pr_cycles,
            },
        }
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
pytest test_main.py::TestApiSettings test_main.py::TestApiTeam -v 2>&1 | tail -10
```
Expected: all pass

- [ ] **Step 6: Run full suite**

```bash
pytest test_main.py -v 2>&1 | tail -5
```
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: update API endpoints for agent_configs and engine/config_mode fields"
```

---

## Chunk 4: UI Changes

### Task 8: Add Agents section to Settings tab

**Files:**
- Modify: `static/index.html`

The Settings view currently has: Models → Orchestration Mode → Cycle Limits → Save. We insert an Agents section and Copilot Connection subsection between Cycle Limits and Save.

- [ ] **Step 1: Insert the Agents section HTML**

In `static/index.html`, find this block (after the Cycle Limits `</div>` closing tag, before the Save button `<div class="flex items-center gap-3">`):

```html
        <div class="flex items-center gap-3">
          <button
            @click="saveSettings()"
```

Insert before it:
```html
        <h2 class="text-xs text-gray-400 uppercase tracking-wide mb-4">Agents</h2>
        <div class="space-y-3 mb-6">
          <template x-for="agent in agentRows" :key="agent.role">
            <div class="border border-gray-700 rounded-lg p-3">
              <!-- Header: role + engine toggle + model + advanced toggle -->
              <div class="flex items-center gap-3 mb-2 flex-wrap">
                <span class="text-xs font-semibold text-gray-200 w-32 flex-shrink-0" x-text="agent.role"></span>
                <div class="flex rounded overflow-hidden border border-gray-700">
                  <button @click="setAgentEngine(agent.role, 'claude')"
                    :class="getAgentEngine(agent.role) === 'claude' ? 'bg-indigo-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'"
                    class="px-3 py-1 text-xs transition-colors">Claude</button>
                  <button @click="setAgentEngine(agent.role, 'copilot')"
                    :class="getAgentEngine(agent.role) === 'copilot' ? 'bg-green-700 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'"
                    class="px-3 py-1 text-xs border-l border-gray-700 transition-colors">Copilot</button>
                </div>
                <input type="text"
                  :value="getAgentModel(agent.role)"
                  @input="setAgentModel(agent.role, $event.target.value)"
                  class="flex-1 min-w-0 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-indigo-500"
                  placeholder="model name" />
                <button @click="toggleAgentAdvanced(agent.role)"
                  class="text-xs text-gray-500 hover:text-gray-300 flex-shrink-0"
                  x-text="isAgentAdvanced(agent.role) ? 'Simple ▲' : 'Advanced ▼'"></button>
              </div>
              <!-- Advanced fields -->
              <div x-show="isAgentAdvanced(agent.role)" class="mt-2 grid grid-cols-2 gap-2 pl-4">
                <div>
                  <label class="block text-xs text-gray-500 mb-1">Provider</label>
                  <select :value="getAgentField(agent.role, 'provider_type')"
                    @change="setAgentField(agent.role, 'provider_type', $event.target.value)"
                    class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-100 focus:outline-none">
                    <option value="openai">openai</option>
                    <option value="azure">azure</option>
                    <option value="anthropic">anthropic</option>
                  </select>
                </div>
                <div>
                  <label class="block text-xs text-gray-500 mb-1">Base URL</label>
                  <input type="text" :value="getAgentField(agent.role, 'base_url')"
                    @input="setAgentField(agent.role, 'base_url', $event.target.value)"
                    class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-100 focus:outline-none"
                    placeholder="https://..." />
                </div>
                <div>
                  <label class="block text-xs text-gray-500 mb-1">API Key</label>
                  <input type="password" :value="getAgentField(agent.role, 'api_key')"
                    @input="setAgentField(agent.role, 'api_key', $event.target.value)"
                    class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-100 focus:outline-none"
                    placeholder="sk-..." />
                </div>
                <div>
                  <label class="block text-xs text-gray-500 mb-1">Extra Params (JSON)</label>
                  <input type="text" :value="getAgentExtraParams(agent.role)"
                    @input="setAgentExtraParams(agent.role, $event.target.value)"
                    :class="agentExtraParamsError(agent.role) ? 'border-red-500' : 'border-gray-700'"
                    class="w-full bg-gray-800 border rounded px-2 py-1 text-xs text-gray-100 focus:outline-none font-mono"
                    placeholder="{}" />
                  <p x-show="agentExtraParamsError(agent.role)" class="text-red-400 text-xs mt-0.5"
                    x-text="agentExtraParamsError(agent.role)"></p>
                </div>
              </div>
              <!-- MCP Servers -->
              <div class="mt-2 pl-4">
                <button @click="toggleAgentMcp(agent.role)" class="text-xs text-gray-600 hover:text-gray-400">
                  MCP Servers
                  <span x-show="mcpServerCount(agent.role) > 0" x-text="'(' + mcpServerCount(agent.role) + ')'"></span>
                  <span x-text="isAgentMcpOpen(agent.role) ? '▲' : '▼'"></span>
                </button>
                <div x-show="isAgentMcpOpen(agent.role)" class="mt-2 space-y-1">
                  <template x-for="(server, idx) in getAgentMcpList(agent.role)" :key="idx">
                    <div class="flex items-center gap-2">
                      <input type="text" :value="server.name"
                        @input="setMcpName(agent.role, idx, $event.target.value)"
                        class="w-28 bg-gray-800 border border-gray-700 rounded px-2 py-0.5 text-xs focus:outline-none"
                        placeholder="name" />
                      <input type="text" :value="server.config"
                        @input="setMcpConfig(agent.role, idx, $event.target.value)"
                        class="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-0.5 text-xs font-mono focus:outline-none"
                        placeholder='{"type":"local","command":"npx","args":["./s.js"],"tools":["*"]}' />
                      <button @click="removeMcpServer(agent.role, idx)" class="text-red-500 hover:text-red-400 text-xs">×</button>
                    </div>
                  </template>
                  <button @click="addMcpServer(agent.role)" class="text-xs text-indigo-400 hover:text-indigo-300 mt-1">+ Add server</button>
                </div>
              </div>
            </div>
          </template>
        </div>

        <!-- Copilot Connection (shown when ≥1 agent uses Copilot engine) -->
        <template x-if="anyAgentUsesCopilot()">
          <div class="mb-6">
            <h2 class="text-xs text-gray-400 uppercase tracking-wide mb-4">Copilot Connection</h2>
            <div>
              <label class="block text-xs text-gray-400 mb-1">
                GitHub Token
                <span class="text-gray-600 font-normal">(optional — falls back to COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN)</span>
              </label>
              <input type="password" x-model="settingsForm.copilot_github_token"
                class="w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 focus:outline-none focus:border-indigo-500"
                placeholder="ghp_..." />
            </div>
          </div>
        </template>
```

- [ ] **Step 2a: Add agent data properties to Alpine.js `app()`**

In `static/index.html`, inside the `app()` return object, add the following data properties (after the `cycleFields` array):

```javascript
    // Agent config UI state
    agentRows: [
      { role: 'lead-developer' }, { role: 'developer' }, { role: 'code-reviewer' },
      { role: 'qa-engineer' },    { role: 'build-agent' }, { role: 'repo-manager' },
    ],
    _agentAdvanced: {},
    _agentMcpOpen: {},
    _agentMcpLists: {},
    _agentExtraParamsRaw: {},
    _agentExtraParamsErr: {},
    _copilotDefaults: {
      'lead-developer': 'claude-opus-4-6', 'developer': 'claude-sonnet-4-6',
      'code-reviewer': 'codex', 'qa-engineer': 'gpt-5.4',
      'build-agent': 'gpt-5-mini', 'repo-manager': 'gpt-5-mini',
    },
    _defaultEngines: {
      'lead-developer': 'claude', 'developer': 'claude',
      'code-reviewer': 'copilot', 'qa-engineer': 'copilot',
      'build-agent': 'copilot', 'repo-manager': 'copilot',
    },
    // Maps each role to the AppSettings field name holding its Claude tier model
    _claudeDefaultTier: {
      'lead-developer': 'opus_model', 'developer': 'sonnet_model',
      'code-reviewer': 'opus_model', 'qa-engineer': 'opus_model',
      'build-agent': 'haiku_model', 'repo-manager': 'haiku_model',
    },
```

- [ ] **Step 2b: Add agent helper methods to Alpine.js `app()`**

Add the following methods (after `saveSettings()`):

```javascript
    // ── Agent config helpers ──────────────────────────────────────
    _getAgentCfg(role) {
      if (!this.settingsForm.agent_configs) this.settingsForm.agent_configs = {};
      if (!this.settingsForm.agent_configs[role]) {
        this.settingsForm.agent_configs[role] = {
          engine: this._defaultEngines[role] || 'claude',
          model_config: { mode: 'simple', model: '', provider_type: 'openai',
                          base_url: '', api_key: '', extra_params: {} },
          mcp_servers: {},
        };
      }
      return this.settingsForm.agent_configs[role];
    },
    getAgentEngine(role) { return this._getAgentCfg(role).engine; },
    setAgentEngine(role, engine) {
      const cfg = this._getAgentCfg(role);
      cfg.engine = engine;
      if (cfg.model_config.mode === 'simple') {
        if (engine === 'copilot') {
          cfg.model_config.model = this._copilotDefaults[role] || '';
        } else {
          // Look up the per-role Claude tier field (e.g. 'opus_model', 'sonnet_model')
          const tierField = this._claudeDefaultTier[role] || 'sonnet_model';
          cfg.model_config.model = this.settingsForm[tierField] || '';
        }
      }
    },
    getAgentModel(role) { return this._getAgentCfg(role).model_config.model; },
    setAgentModel(role, val) { this._getAgentCfg(role).model_config.model = val; },
    isAgentAdvanced(role) { return !!this._agentAdvanced[role]; },
    toggleAgentAdvanced(role) {
      // Use object spread to trigger Alpine.js v3 reactivity on new keys
      const next = !this._agentAdvanced[role];
      this._agentAdvanced = { ...this._agentAdvanced, [role]: next };
      this._getAgentCfg(role).model_config.mode = next ? 'advanced' : 'simple';
    },
    getAgentField(role, field) { return this._getAgentCfg(role).model_config[field] || ''; },
    setAgentField(role, field, val) { this._getAgentCfg(role).model_config[field] = val; },
    getAgentExtraParams(role) {
      if (this._agentExtraParamsRaw[role] === undefined)
        this._agentExtraParamsRaw[role] = JSON.stringify(
          this._getAgentCfg(role).model_config.extra_params || {});
      return this._agentExtraParamsRaw[role];
    },
    setAgentExtraParams(role, val) {
      this._agentExtraParamsRaw = { ...this._agentExtraParamsRaw, [role]: val };
      try {
        this._getAgentCfg(role).model_config.extra_params = JSON.parse(val);
        this._agentExtraParamsErr = { ...this._agentExtraParamsErr, [role]: '' };
      } catch {
        this._agentExtraParamsErr = { ...this._agentExtraParamsErr, [role]: 'Invalid JSON' };
      }
    },
    agentExtraParamsError(role) { return this._agentExtraParamsErr[role] || ''; },
    isAgentMcpOpen(role) { return !!this._agentMcpOpen[role]; },
    toggleAgentMcp(role) {
      this._agentMcpOpen = { ...this._agentMcpOpen, [role]: !this._agentMcpOpen[role] };
    },
    mcpServerCount(role) { return Object.keys(this._getAgentCfg(role).mcp_servers || {}).length; },
    getAgentMcpList(role) {
      if (!this._agentMcpLists[role]) {
        const s = this._getAgentCfg(role).mcp_servers || {};
        this._agentMcpLists[role] = Object.entries(s).map(([name, cfg]) =>
          ({ name, config: JSON.stringify(cfg) }));
      }
      return this._agentMcpLists[role];
    },
    addMcpServer(role) {
      if (!this._agentMcpLists[role]) this._agentMcpLists[role] = [];
      this._agentMcpLists[role].push({ name: '', config: '{}' });
    },
    removeMcpServer(role, idx) { this._agentMcpLists[role].splice(idx, 1); this._syncMcp(role); },
    setMcpName(role, idx, val) { this._agentMcpLists[role][idx].name = val; this._syncMcp(role); },
    setMcpConfig(role, idx, val) { this._agentMcpLists[role][idx].config = val; this._syncMcp(role); },
    _syncMcp(role) {
      const servers = {};
      for (const { name, config } of (this._agentMcpLists[role] || []))
        if (name) try { servers[name] = JSON.parse(config); } catch {}
      this._getAgentCfg(role).mcp_servers = servers;
    },
    anyAgentUsesCopilot() {
      if (!this.settingsForm.agent_configs) return false;
      const explicit = Object.values(this.settingsForm.agent_configs).some(c => c.engine === 'copilot');
      const byDefault = Object.keys(this._defaultEngines).some(role =>
        !this.settingsForm.agent_configs[role] && this._defaultEngines[role] === 'copilot');
      return explicit || byDefault;
    },
    hasAgentExtraParamsErrors() {
      return Object.values(this._agentExtraParamsErr).some(e => !!e);
    },
```

- [ ] **Step 2c: Guard `saveSettings()` against JSON errors**

Update `saveSettings()` — add a guard at the top:
```javascript
    async saveSettings() {
      if (this.hasAgentExtraParamsErrors()) {
        this.settingsError = 'Fix JSON errors in Extra Params before saving.';
        return;
      }
      // ... rest of existing saveSettings logic unchanged ...
    },
```

- [ ] **Step 3: Verify in browser**

```bash
cd /Users/anil/Source/OLamo && source venv/bin/activate && python main.py --server --port 8001
```
Open http://localhost:8001 → Settings tab. Verify:
- 6 agent rows visible, each with engine toggle (Claude/Copilot) and model input
- Toggling engine resets model to smart default
- Advanced ▼ reveals Provider / Base URL / API Key / Extra Params fields
- MCP ▼ reveals add/remove server rows
- Copilot Connection section appears (since 4 agents default to Copilot)
- Saving sends agent_configs to API successfully

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat: add per-agent engine/model config to Settings UI"
```

---

### Task 9: Add engine badges to Team tab

**Files:**
- Modify: `static/index.html` — agent card template in Team view

- [ ] **Step 1: Update agent card template**

Find the existing card header in the Team view:
```html
            <div class="flex items-center gap-3 mb-2">
              <div :class="roleColor(agent.role)" class="w-2 h-2 rounded-full flex-shrink-0"></div>
              <span class="font-semibold text-sm" x-text="agent.role"></span>
              <span class="ml-auto text-xs px-2 py-0.5 bg-gray-800 rounded text-gray-400" x-text="agent.model"></span>
            </div>
```

Replace with:
```html
            <div class="flex items-center gap-3 mb-2">
              <div :class="roleColor(agent.role)" class="w-2 h-2 rounded-full flex-shrink-0"></div>
              <span class="font-semibold text-sm" x-text="agent.role"></span>
              <div class="ml-auto flex items-center gap-1.5">
                <span
                  :class="agent.engine === 'copilot'
                    ? 'bg-green-900/50 text-green-400 border-green-800'
                    : 'bg-indigo-900/50 text-indigo-400 border-indigo-800'"
                  class="text-xs px-1.5 py-0.5 rounded border"
                  x-text="agent.engine || 'claude'">
                </span>
                <span class="text-xs px-2 py-0.5 bg-gray-800 rounded text-gray-400" x-text="agent.model"></span>
              </div>
            </div>
```

- [ ] **Step 2: Verify in browser**

Reload http://localhost:8001 → Team tab. Verify:
- Each agent card shows an indigo `claude` badge or green `copilot` badge
- Model name still shows alongside the badge
- Badges match the smart defaults (lead-developer and developer: claude; rest: copilot)

- [ ] **Step 3: Run full test suite**

```bash
pytest test_main.py -v 2>&1 | tail -10
```
Expected: all pass

- [ ] **Step 4: Final commit**

```bash
git add static/index.html
git commit -m "feat: add engine badges to Team tab agent cards"
```
