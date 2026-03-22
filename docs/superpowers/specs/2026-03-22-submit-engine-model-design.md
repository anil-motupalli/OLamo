# Submit-Time Engine/Model Selection & Terminology Rename Design

**Date:** 2026-03-22
**Status:** Approved
**Scope:** Web UI (submit form + Settings tab) + pipeline merge logic

---

## Overview

Two related changes:

1. **Submit form agent cards:** The task submission form gains a data-driven grid of per-agent cards — one per agent returned by `GET /api/team` — showing engine and model. Each card has a `✎` indicator and expands inline to allow per-run engine/model overrides. A "Save as default" button persists changes to global settings.

2. **Terminology rename:** The `"Simple"` / `"Advanced"` mode labels throughout the UI are renamed to `"Subscription"` / `"API (BYOK)"` to make the billing implication explicit. Internal `ModelConfig.mode` values (`"simple"` / `"advanced"`) are unchanged.

---

## Section 1: Backend

### Pipeline merge for per-run overrides

`RunRecord.settings_override` already accepts arbitrary dict data. This spec defines a new key it can contain:

```python
settings_override = {
    "agent_configs": {
        "developer": {
            "engine": "copilot",
            "model_config": { "mode": "simple", "model": "gpt-5", ... },
            "mcp_servers": {}
        }
    }
    # existing cycle limit keys also allowed: max_design_cycles, etc.
}
```

At the start of `run_pipeline_orchestrated`, after loading global `settings`, merge per-run overrides:

```python
run_agent_overrides = run.settings_override.get("agent_configs", {})
if run_agent_overrides:
    merged = dict(settings.agent_configs)
    for role, cfg_dict in run_agent_overrides.items():
        merged[role] = _agent_engine_config_from_dict(cfg_dict)
    settings = dataclasses.replace(settings, agent_configs=merged)
```

This is a shallow per-role merge: roles present in `run_agent_overrides` replace the global config for that role; all others use global settings.

`POST /api/runs` already accepts `settings_override` — no endpoint changes needed.

### Testing

- `test_run_agent_config_override_takes_precedence` — create a run with `settings_override.agent_configs` overriding one role; assert `_resolve(role)` in the pipeline returns the overridden engine/model while other roles use global defaults.

---

## Section 2: Frontend — Submit Form

### Agent cards grid

Below the task description textarea, a responsive CSS grid renders one card per agent (data sourced from `GET /api/team` on page load, stored in `this.team`).

Each card (default/collapsed state):
- Role name (bold)
- Engine badge: indigo for `claude`, green for `copilot`
- Model name (muted text)
- `✎` icon in top-right corner
- On hover: border intensifies (indigo or green), cursor becomes pointer

Expanded state (click to open, only one open at a time):
- Engine toggle: `[Claude] [Copilot]`
- Model text input (pre-filled with current model)
- `Subscription ▲` / `API (BYOK) ▼` toggle
- When `API (BYOK)` expanded: Provider dropdown, Base URL, API Key, Extra Params inputs
- A second click on the card (or clicking another card) collapses it

### "Save as default" button

Appears below the card grid when any card's config differs from `this.team` (the loaded defaults). Clicking it calls `PUT /api/settings` with `{ agent_configs: <current overrides> }` and hides the button on success.

### Form submission

`POST /api/runs` payload:
```json
{
  "description": "...",
  "settings_override": {
    "agent_configs": { "<role>": { ... } }
  }
}
```

Only roles that were explicitly changed by the user are included in `settings_override.agent_configs`. Unchanged roles are omitted (they fall back to global settings in the pipeline).

### Alpine.js state additions (in `app()`)

```javascript
_submitAgentOverrides: {},   // role → AgentEngineConfig dict, only modified roles
_submitCardOpen: null,       // role string of currently expanded card, or null
```

---

## Section 3: Terminology Rename

The rename applies to **display labels only**. The underlying `ModelConfig.mode` values `"simple"` and `"advanced"` are preserved in Python, the database, and JavaScript state to avoid a data migration.

### Changes in `static/index.html`

| Before | After |
|---|---|
| `'Simple ▲'` (button text) | `'Subscription ▲'` |
| `'Advanced ▼'` (button text) | `'API (BYOK) ▼'` |
| Any tooltip or label referencing "simple mode" | "Subscription mode (no per-request cost)" |
| Any tooltip or label referencing "advanced mode" | "API (BYOK) mode (pay per request)" |

Affected locations: the `x-text` expression on `toggleAgentAdvanced` buttons in the Settings tab Agents section, and the equivalent in the new submit form expanded card.

### No changes in `main.py`

`ModelConfig.mode`, `__post_init__` validation, and `ClaudeEngine`/`CopilotEngine` mode checks all reference `"simple"` / `"advanced"` — these stay as-is.

---

## Section 4: Error Handling

| Scenario | Behaviour |
|---|---|
| `GET /api/team` fails on page load | Submit form shows cards with placeholder "–" model names; submit still works with global settings |
| `PUT /api/settings` fails on "Save as default" | Show inline error below the button; don't block submission |
| Invalid `API (BYOK)` JSON in Extra Params on submit | Client-side validation blocks submission, same as Settings tab today |
| `POST /api/runs` receives malformed `agent_configs` | Existing `_agent_engine_config_from_dict` raises `TypeError`; caught as 422 in `update_settings` (already fixed); run creation handler should similarly catch and return 422 |

---

## Out of Scope

- Per-run MCP server overrides (global settings only)
- Showing per-run engine/model in the run detail view / run history
- Model autocomplete or validation against a model list
