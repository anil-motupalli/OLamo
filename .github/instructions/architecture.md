# Architecture

## Directory layout

```
main.py                     Entry point — CLI args, dispatch to server or CLI runner
config/defaults.json        Single source of truth for all defaults (models, limits, tools)
olamo-settings.json         Runtime settings (JSONC) — overrides defaults per deployment
agents/
  <role>.md                 Agent system prompts (character files)
  prompts/<role>/<task>.md  Per-task prompt templates with {{token}} substitution
app/
  constants.py              Loads config/defaults.json; re-exports named constants
  agents.py                 Builds AgentDefinition dicts and AGENT_CONFIGS (role→system_prompt+tools+tier)
  prompts.py                load_character(role) and load_prompt(role, task, tokens)
  settings.py               SettingsStore — JSONC read/write with lock/unlock for in-flight runs
  models/                   Dataclasses: AppSettings, AgentEngineConfig, ModelConfig, RunRecord, RunStatus
  engines/                  One file per engine (claude, copilot, openai_compat, codex, mock, base)
  pipeline/                 Orchestration logic (see pipeline.md)
  web/                      FastAPI app, SSE broadcaster, RunManager, SQLite DB, GitHub helpers
static/index.html           Full frontend — single-file Alpine.js + Tailwind UI (~2200 lines)
```

## Request flow (web mode)

```
Browser → POST /api/runs
  → RunManager.enqueue()
  → asyncio.Semaphore (max 5 concurrent)
  → run_pipeline() [runner.py]
    → run_pipeline_orchestrated() [orchestrated.py]
      → call(role, prompt)  ←→  Engine.run()
  → SSE events → GET /api/events → Browser
```

## Engine abstraction

All engines implement the same interface (`app/engines/base.py`):

```python
async def start() -> None
async def stop() -> None
async def run(role, prompt, system_prompt, tools, model, model_config, mcp_servers, on_event, **kwargs) -> str
```

- **ClaudeEngine** — `claude-agent-sdk`; spawns Claude Code CLI subprocess per call
- **CopilotEngine** — `github-copilot-sdk`; creates a new session per call; uses event-driven `session.send()` + `session.on()` (not `send_and_wait`) with a 1800s default timeout configurable via `model_config.extra_params["timeout_seconds"]`
- **OpenAIEngine** — any OpenAI-compatible endpoint (z.ai, Azure, etc.) via `openai` SDK
- **CodexEngine** — `codex-app-server-sdk`
- **MockEngine** — returns canned responses; used in headless/dry-run mode

Only engines whose roles are actually configured are instantiated. Engine instances are started once at pipeline start and shared across all `call()` invocations in a run.

## SSE event types

Events emitted by `on_event` callbacks and broadcast to the browser:

| type | key fields |
|---|---|
| `stage_changed` | `stage`, `cycle` |
| `agent_started` | `role`, `action` |
| `agent_message` | `role`, `text` |
| `agent_tool_call` | `role`, `tool_name`, `args_preview` |
| `agent_tool_result` | `role`, `tool_name`, `result_preview` |
| `agent_completed` | `role`, `success`, `elapsed_ms`, `summary` |
| `awaiting_approval` | `run_id`, `spec` |
| `design_approval_received` | `run_id`, `approved` |

## Database

SQLite via `aiosqlite`. Schema and queries in `app/web/database.py` (`OLamoDb`). Stores `runs` and `run_state` (checkpoint JSON per run). `RunManager` loads all runs on startup — interrupted runs surface as `INTERRUPTED` and can be resumed via `POST /api/runs/{id}/resume`.
