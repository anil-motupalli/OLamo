# Per-Agent Engine & Model Selection Design

**Date:** 2026-03-16
**Status:** Approved
**Scope:** Orchestrated mode only (PM mode parked for future)

---

## Overview

Add the ability to configure each OLamo agent independently with its own agentic engine (Claude Agent SDK or GitHub Copilot SDK), model, and optional third-party provider (BYOK). Configuration is exposed in the web UI with simple and advanced model modes. Smart defaults are applied when no explicit config is provided.

---

## Section 1: Data Model

### New dataclasses in `main.py`

```python
@dataclass
class ModelConfig:
    mode: str = "simple"           # "simple" | "advanced"
    model: str = ""                # model name (both modes); "" = use smart default
    # Advanced-only fields (BYOK / third-party provider):
    provider_type: str = "openai"  # "openai" | "azure" | "anthropic"
    base_url: str = ""             # API endpoint URL
    api_key: str = ""              # API key
    extra_params: dict = field(default_factory=dict)  # e.g. {"reasoning_effort": "high"}

@dataclass
class AgentEngineConfig:
    engine: str = "claude"         # "claude" | "copilot"
    model_config: ModelConfig = field(default_factory=ModelConfig)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
```

### `AppSettings` additions

```python
agent_configs: dict[str, AgentEngineConfig] = field(default_factory=dict)
copilot_github_token: str = ""
```

Keys in `agent_configs` match agent role names: `"lead-developer"`, `"developer"`, `"code-reviewer"`, `"qa-engineer"`, `"build-agent"`, `"repo-manager"`. Missing keys fall back to smart defaults.

The existing `opus_model`, `sonnet_model`, `haiku_model` fields remain as Claude shorthand defaults.

### Smart defaults

| Agent | Default Engine | Claude Model (if engine=claude) | Copilot Model (if engine=copilot) |
|---|---|---|---|
| `lead-developer` | claude | `settings.opus_model` | `claude-opus-4-6` |
| `developer` | claude | `settings.sonnet_model` | `claude-sonnet-4-6` |
| `code-reviewer` | **copilot** | `settings.opus_model` ¹ | `codex` (exact model ID string passed to Copilot SDK) |
| `qa-engineer` | **copilot** | `settings.opus_model` ¹ | `gpt-5.4` |
| `build-agent` | **copilot** | `settings.haiku_model` ¹ | `gpt-5-mini` |
| `repo-manager` | **copilot** | `settings.haiku_model` ¹ | `gpt-5-mini` |

¹ The Claude Model column for copilot-defaulted agents is the fallback model used only if the user explicitly switches that agent to the Claude engine. It is not active in the default configuration.

Smart defaults are provided by a pure function `get_default_engine_config(role: str, settings: AppSettings) -> AgentEngineConfig`. For Claude-engine agents, the shorthand tier names (`opus`, `sonnet`, `haiku`) resolve to the corresponding `AppSettings` field values (`settings.opus_model`, `settings.sonnet_model`, `settings.haiku_model`) respectively.

---

## Section 2: Engine Abstraction

### Protocol

```python
class AgentEngine(Protocol):
    async def start(self) -> None: ...   # called once before first run(); no-op for ClaudeEngine
    async def stop(self) -> None: ...    # called once after pipeline finishes; no-op for ClaudeEngine
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
```

`ClaudeEngine.start()` and `ClaudeEngine.stop()` are no-ops. `CopilotEngine.start()` starts the `CopilotClient` process (and validates the GitHub token); `CopilotEngine.stop()` shuts it down. Both engines are called through the Protocol uniformly by the pipeline.

### `ClaudeEngine`

Wraps `claude_agent_sdk.query()`. `tools` passed as the `tools=` field of `ClaudeAgentOptions` (not `allowed_tools=`). `mcp_servers` passed through `ClaudeAgentOptions`. The full streaming loop (`async for msg in query(...)`) and all `on_event` emissions move inside `ClaudeEngine.run()` — this is an explicit refactor of the existing `call()` helper into the engine class. `AgentDefinition` is not used in this path; it remains PM-mode-only.

Advanced `ModelConfig` fields map to the subprocess env: `base_url` → `ANTHROPIC_BASE_URL`, `api_key` → `ANTHROPIC_API_KEY`. Per-agent `base_url` takes precedence over the global `settings.api_base_url` — the engine builds the env dict starting from `_make_env(settings)` and then overwrites with the per-agent values.

- **Stateless** — one `query()` call per `run()`, no shared state.

### `CopilotEngine`

Wraps `github-copilot-sdk` (`from copilot import CopilotClient`). Architecture mirrors Claude Agent SDK: JSON-RPC to Copilot CLI process.

- **Lifecycle:** One `CopilotClient` per pipeline run, started once, **fresh session per `run()` call** (no session reuse across agents), client stopped when pipeline finishes.
- **Tools:** All Copilot CLI built-in tools enabled by default (`--allow-all` equivalent) — file system, git, bash, web. No Python tool layer needed.
- **MCP servers:** Passed directly to `create_session(mcp_servers=...)` in the same dict format.
- **Simple model:** `create_session(model=model)` with no provider block.
- **Advanced model (BYOK):** `create_session(model=model, provider={type, base_url, api_key, ...})` — maps `ModelConfig` fields directly to the Copilot SDK `provider` config.
- **Events:** `assistant.message` → emit `agent_message`; `session.idle` → pipeline continues.
- **Auth:** `SubprocessConfig(github_token=copilot_github_token)` if set, otherwise falls back to env vars `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`.

### Tool name mapping

`tools` list uses Claude Code names (e.g. `["Read", "Bash"]`) for `ClaudeEngine`. The `tools` parameter is included in the Protocol for uniformity; `CopilotEngine` ignores it by design — all built-in tools are available by default. Extra tools beyond built-ins are provided via `mcp_servers`.

---

## Section 3: UI Changes

### Settings tab — "Agents" section

New section added below existing Models section. One row per agent (6 total).

**Simple mode (default):**
```
[lead-developer]  Engine: [Claude] [Copilot]   Model: [claude-opus-4.6   ▾]  [Advanced ▼]
```
- Engine toggle: pill buttons (Claude/Copilot), same style as Orchestration Mode toggle
- Model: text input pre-filled with smart default for selected engine
- Switching engine auto-resets model to the smart default for the new engine

**Advanced mode (expanded):**
```
[lead-developer]  Engine: [Claude] [Copilot]   Model: [my-model          ]  [Simple ▲]
  Provider: [openai ▼]   Base URL: [https://...        ]   API Key: [••••••••]
  Extra params (JSON): { "reasoning_effort": "high" }
```
- Provider type dropdown: `openai` | `azure` | `anthropic`
- Base URL and API key text inputs
- Extra params: single-line JSON input for engine-specific options

**MCP servers** (collapsible, below model row):
- Key-value editor: `name → {type, command/url, args, tools}`
- Add/remove rows

**Copilot Connection subsection** (shown when ≥1 agent uses Copilot engine):
- `GitHub Token` — password input, optional (falls back to env vars)

### Team tab — agent card updates

Each agent card gains an engine badge alongside the existing model badge:
- `claude` → indigo badge
- `copilot` → green badge

### API changes

- `GET /api/settings` — response includes `agent_configs` dict and `copilot_github_token`
- `PUT /api/settings` — accepts `agent_configs` dict and `copilot_github_token`
- `GET /api/team` — each agent entry gains `engine` (`"claude"` | `"copilot"`) and `config_mode` (`"simple"` | `"advanced"`, from `ModelConfig.mode`) fields. Agents absent from `agent_configs` always report `config_mode: "simple"` and the smart-default model for the active engine.

---

## Section 4: Integration, Error Handling & Testing

### Integration with orchestrated pipeline

`call(role, prompt)` in `run_pipeline_orchestrated`:
1. Looks up `AgentEngineConfig` for `role` from `settings.agent_configs`
2. Falls back to `get_default_engine_config(role, settings)` if not set
3. Resolves effective model (explicit or smart default)
4. Calls `engine.run(role, prompt, system_prompt, tools, model, model_config, mcp_servers, on_event)`

Engine instances (`ClaudeEngine`, `CopilotEngine`) are created once at the start of `run_pipeline_orchestrated` and shared across all `call()` invocations. `CopilotEngine` is only instantiated if at least one agent uses the Copilot engine. The `CopilotEngine` instance wraps a single `CopilotClient` process; a new session is created inside each `run()` call and closed before `run()` returns.

**PM mode:** No changes. Engine selection is silently ignored when `orchestration_mode = "pm"`.

### Error handling

| Scenario | Behaviour |
|---|---|
| Copilot CLI not installed | `SystemExit` at `CopilotClient.start()` with install instructions |
| Missing GitHub token (no env var, no setting) | `RuntimeError` raised at `CopilotEngine.start()` (called at pipeline start, before any agent runs) with a clear message listing the expected env vars |
| Copilot SDK exception during `run()` | Wrapped in `RuntimeError`, propagates to existing pipeline error handler |
| Invalid `extra_params` JSON in UI | Client-side validation; invalid JSON shown inline, save blocked |
| Advanced model missing `base_url` | Validated in `AppSettings.__post_init__`: iterates all `agent_configs` entries and raises `ValueError` if any has `mode=="advanced"` and empty `base_url`. This surfaces as an HTTP 422 to the UI caller via the `PUT /api/settings` handler. |
| Advanced model `provider_type` invalid | `provider_type` defaults to `"openai"` and is always valid as a fallback; no explicit validation needed beyond what the underlying SDK enforces |
| Advanced model missing `api_key` | `api_key` is optional — some providers (e.g. Ollama) use ambient auth. No validation error; the underlying SDK will surface an auth error if required by the provider. |

### Testing

- `get_default_engine_config(role, settings)` — pure function, unit tested for all 6 roles
- `CopilotEngine.run()` — unit tested with a mock `CopilotClient` (session event loop is simple)
- `ClaudeEngine` path — no new tests needed, unchanged

---

## Out of Scope

- PM mode engine selection (parked for future)
- Per-run agent config overrides (only global settings for now)
- Model dropdown/autocomplete (free-text input only)
