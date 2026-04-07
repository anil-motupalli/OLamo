# Settings & Configuration

## Layers (lowest → highest priority)

1. **`config/defaults.json`** — compile-time defaults for models, cycle limits, agent tools, engine assignments. Loaded by `app/constants.py` into named constants. Never edit for a single deployment.
2. **`olamo-settings.json`** — runtime overrides (JSONC with `//` comments supported). Copy from `olamo-settings.example.jsonc`. This file is read by `SettingsStore` on startup and re-read on settings API calls.
3. **API** — `PUT /api/settings` accepts a JSON body and persists to `olamo-settings.json`. Settings updates are locked while a run is in progress (`SettingsStore.lock/unlock`).

## Key settings fields (`AppSettings`)

| Field | Default | Notes |
|---|---|---|
| `orchestration_mode` | `"pm"` | Use `"orchestrated"` for mixed-engine support |
| `opus_model` / `sonnet_model` / `haiku_model` | `"opus"` / `"sonnet"` / `"haiku"` | Passed to Claude engine |
| `max_design_cycles` | 5 | QA→refine loops in Stage 1 |
| `max_impl_cycles` | 5 | implement→review loops in Stage 2 |
| `max_build_cycles` | 3 | build→fix loops per impl cycle |
| `max_pr_cycles` | 3 | CI + PR comment cycles |
| `api_base_url` | `""` | Custom Anthropic base URL for Claude engine |
| `copilot_github_token` | `""` | Leave empty to use `gh auth login` |
| `headless` | `false` | MockEngine; no real API calls |

## Per-agent engine config (`agent_configs`)

Each role can have an independent `AgentEngineConfig`:

```jsonc
"agent_configs": {
  "developer": {
    "engine": "claude",           // "claude" | "copilot" | "openai" | "codex"
    "model_config": {
      "mode": "simple",           // "simple" (subscription) | "advanced" (BYOK)
      "model": "sonnet",
      "provider_type": "openai",  // for advanced: "openai" | "azure" | "anthropic" | "bedrock"
      "base_url": "",             // required for advanced mode
      "api_key": "",              // required for advanced mode
      "extra_params": {
        "timeout_seconds": 1800   // Copilot engine timeout (default 1800s)
      }
    },
    "mcp_servers": {}             // MCP server configs passed to the engine
  }
}
```

If `agent_configs` omits a role, `get_default_engine_config(role, settings)` is used, which reads from `config/defaults.json`.

## z.ai / OpenAI-compatible BYOK

Set `engine: "openai"` and:
```jsonc
"model_config": {
  "mode": "advanced",
  "base_url": "https://api.z.ai/api/paas/v4",
  "api_key": "<your-key>",
  "model": "glm-5v-turbo"
}
```

## SettingsStore lock semantics

`SettingsStore.lock()` is called when a run starts, `unlock()` when it ends. A `PUT /api/settings` during a locked period queues the update as `_pending` — it applies automatically on unlock. This prevents mid-run settings changes from corrupting in-flight pipelines.
