# OLamo — Copilot Instructions

OLamo is a multi-agent AI coding pipeline. It accepts a task description, runs it through a deterministic design → implementation → review → PR loop, and surfaces a pull request. It supports Claude, GitHub Copilot (BYOK), OpenAI-compatible, and Codex engines, with each agent configurable independently.

## Quick reference

| Topic | File |
|---|---|
| Build, test, run commands | [instructions/commands.md](instructions/commands.md) |
| Architecture overview | [instructions/architecture.md](instructions/architecture.md) |
| Pipeline stages & loops | [instructions/pipeline.md](instructions/pipeline.md) |
| Agent system prompts & review flow | [instructions/agents.md](instructions/agents.md) |
| Settings & configuration | [instructions/settings.md](instructions/settings.md) |

## Most important conventions at a glance

- All defaults (model names, cycle limits, agent tools, engine assignments) live in **`config/defaults.json`** — never hardcode them.
- Agent system prompts are **markdown files** in `agents/<role>.md`. Changing an agent's behavior means editing that file, not Python code.
- The pipeline has two modes (`orchestration_mode`): **`orchestrated`** (Python-driven, supports mixed engines — the active mode) and **`pm`** (LLM orchestrator, Claude-only). All new work goes into `orchestrated`.
- Settings are read from **`olamo-settings.json`** (JSONC supported). The example is `olamo-settings.example.jsonc`.
- Tests use **pytest-asyncio** with `asyncio_mode = auto`. Engine tests are excluded from the default run.
