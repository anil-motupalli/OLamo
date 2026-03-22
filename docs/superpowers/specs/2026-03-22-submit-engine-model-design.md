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

The merge happens in `RunManager._execute_run`, after the existing scalar override merge. The existing scalar merge must explicitly **exclude** `agent_configs` to prevent it from being passed as a raw dict into `AppSettings`:

```python
# Existing scalar merge — exclude agent_configs to avoid type collision
fields = {f.name for f in dataclasses.fields(AppSettings)} - {"agent_configs"}
filtered = {k: v for k, v in run.settings_override.items() if k in fields}
settings = AppSettings(**{**asdict(base), **filtered})

# New: per-run agent config override
run_agent_overrides = run.settings_override.get("agent_configs", {})
if run_agent_overrides:
    merged_agents = dict(settings.agent_configs)
    for role, cfg_dict in run_agent_overrides.items():
        merged_agents[role] = _agent_engine_config_from_dict(cfg_dict)
    from dataclasses import replace  # add to top-level imports
    settings = replace(settings, agent_configs=merged_agents)
```

Add `replace` to the existing `from dataclasses import asdict, dataclass, field` import line.

This is a shallow per-role merge: roles present in `run_agent_overrides` replace the global config for that role; all others use global settings.

`POST /api/runs` already accepts `settings_override` — no endpoint changes needed.

### Testing

- `test_run_agent_config_override_takes_precedence` — construct a `RunRecord` with `settings_override={"agent_configs": {"developer": {"engine": "copilot", "model_config": {...}, "mcp_servers": {}}}}` and default global settings; call `_execute_run`; assert the developer agent resolves to copilot engine while all other agents use global defaults.
- `test_existing_scalar_overrides_still_work` — verify that `max_design_cycles` override in `settings_override` still applies correctly after the `agent_configs` exclusion change.

---

## Section 2: Frontend — Submit Form

### Agent cards grid

Below the task description textarea, a responsive CSS grid (wraps at any column count) renders one card per agent — sourced from `this.team.agents` (already loaded by `GET /api/team` on page load).

Each card (default/collapsed state):
- Role name (bold)
- Engine badge: indigo for `claude`, green for `copilot`
- Model name (muted text)
- `✎` icon in top-right corner
- On hover: border intensifies (indigo or green), cursor becomes pointer

Expanded state (click to open, only one open at a time):
- Engine toggle: `[Claude] [Copilot]`
- Model text input (pre-filled with current model)
- `Subscription ▲` / `API (BYOK) ▼` toggle (labels only; internal mode values remain `"simple"` / `"advanced"`)
- When `API (BYOK)` expanded: Provider dropdown, Base URL, API Key, Extra Params inputs
- A second click on the card (or clicking another card) collapses it

### Alpine.js state additions (in `app()`)

```javascript
_submitAgentCfgs: {},   // role → full AgentEngineConfig-shaped dict, initialized from this.team.agents
_submitCardOpen: null,  // role string of currently expanded card, or null
_savingDefaults: false, // true while PUT /api/settings is in flight
```

`_submitAgentCfgs` is populated (or refreshed) each time `loadTeam()` completes, by mapping `this.team.agents` into a dict keyed by role. Card edits mutate `_submitAgentCfgs[role]` directly. This is the single source of truth for both display and submission.

**Dirty detection** (used to show "Save as default" and to build `settings_override.agent_configs`): at submit time and for button visibility, compare each role's current `_submitAgentCfgs[role]` against the corresponding entry in `this.team.agents` using a JSON-string equality check (`JSON.stringify`). Only roles that differ are included in `settings_override.agent_configs`. This naturally handles "change back to default" — if a user edits a card and then restores it, the comparison returns equal and the role is excluded.

### "Save as default" button

Appears below the card grid when any role's `_submitAgentCfgs[role]` differs from its `this.team.agents` counterpart (dirty detection above). Clicking it:
1. Sets `_savingDefaults = true` (disables both the "Save as default" and "Submit" buttons while in flight)
2. Calls `PUT /api/settings` with `{ agent_configs: <full _submitAgentCfgs dict> }` — the full dict, not just changed roles, so the server replaces the entire stored `agent_configs`
3. On success: re-calls `loadTeam()` to refresh the baseline; hides button
4. On failure: shows inline error; clears `_savingDefaults`

The "Submit" button is disabled while `_savingDefaults` is true to prevent a race between the settings write and the run creation.

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

`settings_override.agent_configs` contains only the roles that differ from `this.team.agents` (dirty detection). If no roles differ, `settings_override` is omitted from the payload entirely.

---

## Section 3: Terminology Rename

The rename applies to **display labels only**. The underlying `ModelConfig.mode` values `"simple"` and `"advanced"` are preserved in Python, the database, and JavaScript state to avoid a data migration.

### Changes in `static/index.html`

Two separate locations:

**Settings tab — Agents section** (existing code, line ~336):
```javascript
// Before:
x-text="isAgentAdvanced(agent.role) ? 'Simple ▲' : 'Advanced ▼'"
// After:
x-text="isAgentAdvanced(agent.role) ? 'Subscription ▲' : 'API (BYOK) ▼'"
```

**Submit form — expanded card** (new code added by this spec):
Same `Subscription ▲` / `API (BYOK) ▼` labels on the mode toggle button in the expanded card UI.

### No changes in `main.py`

`ModelConfig.mode`, `__post_init__` validation, and `ClaudeEngine`/`CopilotEngine` mode checks all reference `"simple"` / `"advanced"` — these stay as-is.

---

## Section 4: Error Handling

| Scenario | Behaviour |
|---|---|
| `GET /api/team` fails on page load | Submit form shows no agent cards (grid is empty); submit still works without `settings_override` |
| `PUT /api/settings` fails on "Save as default" | Show inline error below the button; clear `_savingDefaults`; do not block submission |
| Invalid `API (BYOK)` JSON in Extra Params on submit | Client-side validation blocks submission, same as Settings tab today |
| `POST /api/runs` receives malformed `agent_configs` | Parsing occurs in `_execute_run` (not at request time); malformed config raises an exception that sets `run.status = FAILED` with an error message — this is the correct outcome |
| `_savingDefaults` true when user clicks Submit | Submit button is disabled; no double-flight possible |

---

## Out of Scope

- Per-run MCP server overrides (global settings only)
- Showing per-run engine/model in the run detail view / run history
- Model autocomplete or validation against a model list
