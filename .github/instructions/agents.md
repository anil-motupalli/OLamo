# Agents

## Character files

Each agent's system prompt is a markdown file at `agents/<role>.md`. `app/prompts.py:load_character(role)` reads it at startup. To change an agent's behavior, edit the `.md` file — no Python changes needed.

All agent files begin with a link to `.github/copilot-instructions.md` so agents know where to look for repo conventions.

Roles: `lead-developer`, `developer`, `qa-engineer`, `code-reviewer`, `build-agent`, `repo-manager`

> **Note:** `code-reviewer` is still a valid agent config, but it is not used as a reviewer in the pipeline. The active reviewers are `qa-engineer` and `lead-developer` (2 reviewers, not 3).

## Tools per agent (from `config/defaults.json`)

| Role | Tools |
|---|---|
| lead-developer | Read, Glob, Grep, WebSearch, WebFetch |
| developer | Read, Write, Edit, Bash, Glob, Grep |
| qa-engineer | Read, Bash, Glob, Grep |
| code-reviewer | Read, Glob, Grep |
| build-agent | Bash, Read, Glob |
| repo-manager | Bash, Read |

## Reviewer roles

**`qa-engineer`** covers bugs, security, performance, code quality, and test coverage. When reviewing design it evaluates testability, completeness, clarity, and design quality. Output is always a **structured JSON** object.

**`lead-developer`** reviews implementation for **spec conformance** only — it checks that the code matches the approved plan. Output is always a **structured JSON** object.

## Structured review JSON format

All reviewers output **raw JSON** (no markdown fences, no preamble):

```json
{
  "decision": "Approved" | "NeedsImprovement",
  "findings": [
    {
      "id": "f1",
      "type": "Bug|Security|Performance|CodeQuality|MissingTest|ConformanceViolation|Testability|Completeness|Clarity|DesignQuality",
      "severity": "Critical|MustHave|GoodToHave|Nit",
      "file": "src/foo.py",
      "line": 42,
      "description": "...",
      "suggestion": "..."
    }
  ]
}
```

Use `app.pipeline.helpers.parse_review_json(text)` to parse this output. It handles markdown fences, inline JSON, greedy extraction, and falls back to text heuristics.

## Per-finding response format

After receiving review findings, lead-developer (for design) and developer (for implementation) output:

```
[full plan or implementation notes]
---FINDING_RESPONSES---
[{"id": "f1", "action": "ADDRESSED|FIXED|PUSHBACK", "explanation": "..."}]
```

Use `app.pipeline.helpers.parse_finding_responses(text)` to split on the separator and parse the JSON array.

## Review loops and per-finding responses

**Design loop:** QA reviews the plan and outputs structured JSON findings. Lead-developer receives findings by ID, responds per-finding (ADDRESSED/PUSHBACK), and outputs revised plan + `---FINDING_RESPONSES---` + JSON. QA receives the separator section and weighs pushbacks before its next review.

**Implementation loop:** Developer implements and outputs `---FINDING_RESPONSES---` section for any prior findings. Reviewers (qa-engineer, lead-developer) output structured JSON. The pipeline passes findings with IDs back to the developer next cycle.

## AGENT_CONFIGS

`app/agents.py` exposes `AGENT_CONFIGS: dict[str, tuple[str, list[str], str]]` — maps role → `(system_prompt, tools, claude_tier_attr)`. This is the single source of truth used by `orchestrated.py` to call agents. If you add a new role, add it to `config/defaults.json` (tools + tiers) and create `agents/<role>.md`.
