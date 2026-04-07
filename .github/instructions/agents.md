# Agents

## Character files

Each agent's system prompt is a markdown file at `agents/<role>.md`. `app/prompts.py:load_character(role)` reads it at startup. To change an agent's behavior, edit the `.md` file — no Python changes needed.

Roles: `lead-developer`, `developer`, `qa-engineer`, `code-reviewer`, `build-agent`, `repo-manager`

## Tools per agent (from `config/defaults.json`)

| Role | Tools |
|---|---|
| lead-developer | Read, Glob, Grep, WebSearch, WebFetch |
| developer | Read, Write, Edit, Bash, Glob, Grep |
| qa-engineer | Read, Bash, Glob, Grep |
| code-reviewer | Read, Glob, Grep |
| build-agent | Bash, Read, Glob |
| repo-manager | Bash, Read |

## Prompt templates

Per-task prompts live at `agents/prompts/<role>/<task>.md` with `{{token}}` substitution. `load_prompt(role, task, tokens)` renders them. Currently used for the `pm` orchestration mode (`agents/prompts/pm/pipeline.md`).

## Review loops and per-finding responses

Agents don't use explicit keyword modes — behavior is described naturally in the character file based on what context is present.

**Design loop:** When lead-developer refines a plan it must produce a `## Response to QA Findings` section with per-finding `ADDRESSED: ...` or `PUSHBACK: ...`. QA receives this section alongside the revised plan and decides per-finding whether to accept the pushback or retain the finding.

**Implementation loop:** When developer implements against review findings it must produce a `## Response to Review Findings` section with per-finding `FIXED: ...` or `PUSHBACK: ...`. All three reviewers receive this section as context in the next cycle.

The pipeline uses `APPROVED` and `NEEDS IMPROVEMENT` as the only control-flow keywords it parses from reviewer output.

## AGENT_CONFIGS

`app/agents.py` exposes `AGENT_CONFIGS: dict[str, tuple[str, list[str], str]]` — maps role → `(system_prompt, tools, claude_tier_attr)`. This is the single source of truth used by `orchestrated.py` to call agents. If you add a new role, add it to `config/defaults.json` (tools + tiers) and create `agents/<role>.md`.
