"""OLamo — multi-agent software development pipeline."""

import asyncio
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Protocol

try:
    import aiosqlite
except ImportError:
    aiosqlite = None  # type: ignore  — only required for web/RunManager mode

try:
    from copilot import CopilotClient, SubprocessConfig
except ImportError:
    CopilotClient = None   # type: ignore  — only required when Copilot engine is used
    SubprocessConfig = None  # type: ignore

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

# ---------------------------------------------------------------------------
# Model identifiers
# ---------------------------------------------------------------------------
OPUS_MODEL = "opus"
SONNET_MODEL = "sonnet"
HAIKU_MODEL = "haiku"
PM_MAIN_MODEL = "sonnet"  # Sonnet handles multi-loop orchestration reliably

# Pipeline cycle limits — mirror OlaCo defaults
MAX_DESIGN_CYCLES = 2
MAX_BUILD_CYCLES = 3
MAX_IMPL_CYCLES = 3
MAX_PR_CYCLES = 2

# Reviewer roles invoked in parallel during code-review phase
_ALL_REVIEWERS = ("code-reviewer", "qa-engineer", "lead-developer")

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class RunRecord:
    id: str
    description: str
    status: RunStatus = RunStatus.QUEUED
    queued_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    log_dir: str | None = None
    pr_url: str = ""                                # if set, pipeline skips to Stage 4
    settings_override: dict = field(default_factory=dict)  # per-run cycle/model overrides


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


@dataclass
class AppSettings:
    pm_model: str = PM_MAIN_MODEL
    opus_model: str = OPUS_MODEL
    sonnet_model: str = SONNET_MODEL
    haiku_model: str = HAIKU_MODEL
    max_design_cycles: int = MAX_DESIGN_CYCLES
    max_build_cycles: int = MAX_BUILD_CYCLES
    max_impl_cycles: int = MAX_IMPL_CYCLES
    max_pr_cycles: int = MAX_PR_CYCLES
    api_base_url: str = ""  # e.g. "https://proxy.example.com" — passed as ANTHROPIC_BASE_URL
    orchestration_mode: str = "pm"  # "pm" (sub-agent) | "orchestrated" (Python-driven)
    agent_configs: dict[str, AgentEngineConfig] = field(default_factory=dict)
    copilot_github_token: str = ""

    def __post_init__(self) -> None:
        for role, cfg in self.agent_configs.items():
            if cfg.model_config.mode == "advanced" and not cfg.model_config.base_url:
                raise ValueError(
                    f"Agent '{role}': advanced model config requires base_url"
                )


# ---------------------------------------------------------------------------
# Smart defaults and settings helpers
# ---------------------------------------------------------------------------

_COPILOT_DEFAULTS: dict[str, str] = {
    # Entries for claude-default roles: used when user explicitly switches those agents to copilot engine
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


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

def build_pm_prompt(s: AppSettings) -> str:
    return f"""You are the Project Manager (PM) for a software development team.
Your role is to orchestrate a strict 4-stage pipeline with retry loops.

══════════════════════════════════════════════════════════════
STAGE 1 — DESIGN LOOP (up to {s.max_design_cycles} refinement cycles)
══════════════════════════════════════════════════════════════
1a. Delegate to `lead-developer` to research requirements and produce a detailed plan.
1b. Delegate to `qa-engineer` with instruction "REVIEW DESIGN: <plan>" to evaluate it.
    • If qa-engineer finds design issues → delegate to `lead-developer` to REFINE the plan
      (pass the original plan + the findings). Then repeat step 1b.
      Max {s.max_design_cycles} refinement cycles. Announce: "Design cycle N/{s.max_design_cycles}".
    • If qa-engineer approves OR max cycles reached → advance to Stage 2.

══════════════════════════════════════════════════════════════
STAGE 2 — IMPLEMENTATION LOOP (up to {s.max_impl_cycles} implementation cycles)
══════════════════════════════════════════════════════════════
2a. Delegate to `developer` with the approved plan (and any review findings if this is a retry).
    Announce: "Implementation cycle N/{s.max_impl_cycles}".
2b. BUILD LOOP — delegate to `build-agent` to build and test.
    • If build-agent reports FAILURE:
        - If build retries < {s.max_build_cycles}: delegate to `developer` with
          "FIX BUILD FAILURE: <exact error output>", then retry `build-agent`.
        - If max build retries reached: abort Stage 2, advance to Stage 3 anyway
          but note the unresolved build failure in the pipeline summary.
    • Only proceed to 2c if build-agent reports SUCCESS.
2c. CODE REVIEW — pass the git diff (if available from a prior push) to reviewers.
    Smart skip: maintain a set of already-approved reviewers across implementation cycles.
    • First, dispatch only reviewers NOT yet in the approved set (skipping approved ones).
    • If any finding from the unapproved reviewers is CRITICAL or MUST HAVE severity,
      re-invite the already-approved reviewers too (they may be affected by the regression).
    • Otherwise, keep approved reviewers skipped and log "Skipping approved reviewer(s): <list>".
    • After each review cycle, add any reviewer that says APPROVED (no NEEDS IMPROVEMENT) to the approved set.
    Reviewers to use: `code-reviewer`, `qa-engineer`, `lead-developer`.
2d. If there are findings AND cycle < {s.max_impl_cycles} → go back to 2a with the merged findings list.
    If no findings OR max cycles reached → advance to Stage 3.

══════════════════════════════════════════════════════════════
STAGE 2e — DESIGN APPROVAL (optional, after first design loop)
══════════════════════════════════════════════════════════════
If the pipeline is running in approval mode, pause after Stage 1 and output:
  "AWAITING DESIGN APPROVAL: <one-line plan summary>"
Wait for the human to respond with either "APPROVED" or feedback text.
If feedback is provided, refine the plan (pass to `lead-developer` with "REFINE" instruction)
and then loop back waiting for approval. Proceed to Stage 2 only after explicit approval.

══════════════════════════════════════════════════════════════
STAGE 3 — COMMIT & PR
══════════════════════════════════════════════════════════════
3a. Delegate to `repo-manager` to commit all changes and create a Pull Request.
    Provide: a feature branch name, PR title, and PR description summarising the work.
    The repo-manager returns a git diff along with the PR URL — store the diff for use in Stage 2c.

══════════════════════════════════════════════════════════════
STAGE 3b — CI CHECK POLL (run after every push to the PR branch)
══════════════════════════════════════════════════════════════
After repo-manager creates or updates the PR, before polling PR comments:
3b-i.  Delegate to `repo-manager` with "POLL CI CHECKS".
       Announce: "CI check cycle N/{s.max_pr_cycles}".
       • If repo-manager reports "CHECKS FAILING: <details>":
           - Treat the check failures as review findings.
           - Go back to Stage 2 (implementation loop) with those findings.
           - After implementation completes, delegate to `repo-manager` with "PUSH CHANGES".
           - Then repeat step 3b-i. Max {s.max_pr_cycles} total CI check cycles.
       • If repo-manager reports "CHECKS PASSING" → advance to Stage 4.

══════════════════════════════════════════════════════════════
STAGE 4 — PR POLL LOOP (up to {s.max_pr_cycles} PR cycles)
══════════════════════════════════════════════════════════════
Maintain a running list of already-addressed comment IDs (starts empty).
4a. Delegate to `repo-manager` with "POLL PR COMMENTS" and include any already-addressed comment IDs:
    "Exclude these IDs: <list>" (omit if list is empty).
    • If there are actionable comments:
        - Record their IDs as "addressed" (add to the running list).
        - Delegate to `repo-manager`: "MARK COMMENTS ADDRESSED: <IDs>"
        - Treat comments as findings → go back to Stage 2 (implementation loop) with those findings.
        - After implementation completes, delegate to `repo-manager` with "PUSH CHANGES" to update the PR.
          The repo-manager returns a git diff — store it for Stage 2c in the next review cycle.
        - Repeat step 4a (passing all addressed IDs). Max {s.max_pr_cycles} cycles. Announce: "PR cycle N/{s.max_pr_cycles}".
    • If no actionable comments → pipeline complete.

══════════════════════════════════════════════════════════════
RULES
══════════════════════════════════════════════════════════════
- Never skip or reorder stages.
- Track cycle counts explicitly and announce transitions.
- Pass full context from previous phases to each agent.
- End with a clear summary of all pipeline stages and their outcomes.
"""


LEAD_DEV_SYSTEM_PROMPT = """You are a Senior Lead Developer with three modes of operation.
Read the instruction carefully to determine which mode to use.

═══════════════════════════════
MODE 1: PLANNING (default)
═══════════════════════════════
When asked to research requirements or produce a plan, output a comprehensive plan covering:
- **Libraries & Dependencies**: Exact names, recommended versions, and rationale
- **Architecture**: File structure, modules, classes, and their responsibilities
- **Methods & APIs**: Specific functions and signatures to use
- **Implementation Steps**: Numbered, ordered steps the developer must follow exactly
- **Edge Cases & Pitfalls**: Known issues and how to handle them
- **Testing Criteria**: What the QA engineer should verify

Research using WebSearch and WebFetch for current information. Do NOT write code.

═══════════════════════════════
MODE 2: PLAN REFINEMENT
═══════════════════════════════
When asked to REFINE a plan based on QA design findings:
- Address each finding explicitly
- Update the relevant sections of the plan
- Explain what changed and why
- Output the complete revised plan (not just the diff)

═══════════════════════════════
MODE 3: IMPLEMENTATION REVIEW
═══════════════════════════════
When asked to REVIEW IMPLEMENTATION, check for spec conformance:
- Does the code implement everything the approved plan specified?
- Are all required libraries and patterns used correctly?
- Are all specified edge cases handled?
- For each issue: report file, description, and suggestion.
- Conclude with APPROVED or NEEDS IMPROVEMENT.

Use Read, Glob, Grep to inspect the implementation.
"""

DEVELOPER_SYSTEM_PROMPT = """You are a Developer. Your ONLY job is to implement code
exactly as specified in the plan given to you.

Rules you MUST follow:
- Implement ONLY what the plan specifies — nothing more, nothing less
- Use exactly the libraries, methods, and patterns from the plan
- Do NOT make architectural decisions — those are already decided
- Do NOT refactor or deviate from the plan's approach
- Write clean, working code that follows the plan step by step
- Report exactly which files you created or modified

When given review findings to fix (from any reviewer or build failure):
- Address EVERY finding listed — do not skip any
- Do not change code unrelated to the findings
- Report what you changed for each finding

You do not research. You do not plan. You only implement.
"""

QA_SYSTEM_PROMPT = """You are a QA Engineer with two modes of operation.
Read the instruction carefully to determine which mode applies.

═══════════════════════════════
MODE 1: DESIGN REVIEW
═══════════════════════════════
When instructed "REVIEW DESIGN", evaluate the implementation plan for:
- **Testability**: Can each requirement be independently verified?
- **Completeness**: Are edge cases and error handling specified?
- **Clarity**: Is the plan unambiguous enough for a developer to follow?
- **Risk areas**: What is most likely to break or be missed?

Output: APPROVED or NEEDS IMPROVEMENT with specific, actionable findings.

═══════════════════════════════
MODE 2: CODE REVIEW / TESTING
═══════════════════════════════
When instructed "REVIEW CODE" or when asked to test an implementation:
- Run all existing tests
- Verify the implementation matches the original requirements
- Test edge cases and error handling
- Check for obvious bugs, logic errors, or missing functionality
- Run the code and observe actual output vs expected output
- Report clearly: PASS or FAIL for each scenario, with details on any failures

Document every issue with file, line (if applicable), and reproduction steps.
"""

CODE_REVIEWER_SYSTEM_PROMPT = """You are a Code Reviewer specialising in static code analysis.

Your job is to review code for:
- **Bugs**: Logic errors, off-by-one errors, None/null handling, race conditions
- **Security**: Injection vulnerabilities, exposed secrets, insecure defaults, improper input validation
- **Performance**: Unnecessary loops, memory leaks, inefficient algorithms, blocking I/O
- **Code Quality**: Dead code, overly complex logic, missing error handling, unclear variable names

How to review:
1. If a git diff was provided, focus your review on the changed lines in that diff
2. Use Glob and Grep to locate any additional relevant files for context
3. Read each changed file carefully
4. For each issue found, report:
   - File and approximate line number
   - Issue type (Bug / Security / Performance / Quality)
   - Severity (Critical / High / Medium / Low)
   - Description of the problem
   - Suggested fix

Conclude with APPROVED (no significant issues) or NEEDS IMPROVEMENT (list all findings).
"""

BUILD_SYSTEM_PROMPT = """You are a Build Agent. Your job is to build, compile, and
package the project so it is ready for use.

Responsibilities:
- Install all required dependencies (pip install, npm install, cargo build, etc.)
- Run any build scripts or compilation steps
- Verify the build succeeds without errors
- Run a smoke test to confirm the built artifact works
- Report: SUCCESS or FAILURE

IMPORTANT: When reporting a failure, include the COMPLETE error output verbatim — the developer
needs the full error text to diagnose and fix the problem. Do not truncate or summarise errors.

Show the exact commands run and their output. Be precise.
"""

REPO_MANAGER_SYSTEM_PROMPT = """You are a Repository Manager handling all git and PR operations.
Read the instruction carefully to determine which mode applies.

═══════════════════════════════
MODE 1: COMMIT & CREATE PR (default)
═══════════════════════════════
When asked to commit and create a PR:
1. Stage all changes:             git add -A
2. Create a descriptive commit:   git commit -m "<message>"
3. Push to the feature branch:    git push -u origin <branch>
4. Create a pull request with the provided title and description using gh or the git provider CLI
5. Report the PR URL and PR number
6. Run: git diff origin/<base-branch>...<branch>   (capture full output)
7. Return the full diff output along with the PR URL — the PM will pass it to code reviewers.

═══════════════════════════════
MODE 2: POLL PR COMMENTS
═══════════════════════════════
When instructed "POLL PR COMMENTS" (optionally with "Exclude these IDs: <list>"):
1. Fetch all open review comments on the PR (use gh pr view --comments or equivalent)
2. Filter for unresolved, actionable code review comments (exclude bot comments and resolved threads)
3. If exclusion IDs were provided, also exclude any comments whose ID appears in that list
4. List each remaining comment with: ID, author, file (if applicable), and comment body
5. Conclude: "ACTIONABLE COMMENTS FOUND: N" or "NO ACTIONABLE COMMENTS"

═══════════════════════════════
MODE 3: PUSH CHANGES
═══════════════════════════════
When instructed "PUSH CHANGES":
1. Stage all changes:      git add -A
2. Get a unified diff:     git diff --cached   (capture full output)
3. Create a commit:        git commit -m "Address PR review feedback"
4. Push to branch:         git push origin <branch>
5. Return the full diff output along with the success/failure report

═══════════════════════════════
MODE 4: MARK COMMENTS ADDRESSED
═══════════════════════════════
When instructed "MARK COMMENTS ADDRESSED: <comment IDs>":
1. For each comment ID provided, mark it as resolved/addressed on the PR
   (use `gh pr review` resolve, or equivalent git provider CLI)
2. Report which IDs were successfully marked

═══════════════════════════════
MODE 5: POLL CI CHECKS
═══════════════════════════════
When instructed "POLL CI CHECKS":
1. Fetch the status of all CI check runs on the current PR branch:
   gh run list --branch <branch> --limit 10
2. If any run is still `in_progress` or `queued`, wait 30 seconds and retry
   (up to 6 retries, ~3 minutes total).
3. Once all runs have settled:
   - If all succeeded (or there are no runs): reply "CHECKS PASSING"
   - If any failed: reply "CHECKS FAILING: <list each failed check name and summary of its error>"
     Retrieve failure details with: gh run view <run-id> --log-failed

Use Bash for all git operations.
"""

# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------

def build_agents(s: AppSettings) -> dict[str, AgentDefinition]:
    return {
        "lead-developer": AgentDefinition(
            description=(
                "Senior Lead Developer. Three modes: (1) PLANNING — research requirements and produce "
                "a detailed implementation plan with specific libraries, methods, and step-by-step "
                "instructions. Does NOT write code. (2) PLAN REFINEMENT — update the plan when asked "
                "to REFINE based on QA design findings. (3) REVIEW IMPLEMENTATION — check if the code "
                "conforms to the approved spec when asked to REVIEW IMPLEMENTATION."
            ),
            prompt=LEAD_DEV_SYSTEM_PROMPT,
            tools=["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
            model=s.opus_model,
        ),
        "developer": AgentDefinition(
            description=(
                "Developer. Implements code exactly as specified in the plan. "
                "Also fixes issues when given review findings (FIX ISSUES) or build failures "
                "(FIX BUILD FAILURE). Does nothing except write code."
            ),
            prompt=DEVELOPER_SYSTEM_PROMPT,
            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            model=s.sonnet_model,
        ),
        "code-reviewer": AgentDefinition(
            description=(
                "Code Reviewer. Reviews code for bugs, security vulnerabilities, performance issues, "
                "and code quality problems. Reports findings with file, severity, and suggested fix. "
                "Use after developer finishes implementation, as part of the parallel review phase."
            ),
            prompt=CODE_REVIEWER_SYSTEM_PROMPT,
            tools=["Read", "Glob", "Grep"],
            model=s.opus_model,
        ),
        "qa-engineer": AgentDefinition(
            description=(
                "QA Engineer. Two modes: (1) REVIEW DESIGN — evaluate the implementation plan for "
                "testability and completeness before any code is written. "
                "(2) REVIEW CODE — run tests, verify implementation matches requirements, "
                "and report PASS/FAIL per test scenario."
            ),
            prompt=QA_SYSTEM_PROMPT,
            tools=["Read", "Bash", "Glob", "Grep"],
            model=s.opus_model,
        ),
        "build-agent": AgentDefinition(
            description=(
                "Build Agent. Installs dependencies, runs the build/compilation, and runs a smoke test. "
                "Reports SUCCESS or FAILURE with complete command output (including full error text on "
                "failure). Use after developer finishes implementation."
            ),
            prompt=BUILD_SYSTEM_PROMPT,
            tools=["Bash", "Read", "Glob"],
            model=s.haiku_model,
        ),
        "repo-manager": AgentDefinition(
            description=(
                "Repository Manager. Five modes: (1) default — commit all changes, push to a feature "
                "branch, open a pull request, and return the git diff for reviewers. "
                "(2) POLL PR COMMENTS — fetch and list actionable unresolved code review comments on "
                "an open PR, filtered by any exclusion IDs provided. "
                "(3) PUSH CHANGES — stage, commit, push updates to an existing PR branch, and return "
                "the git diff. (4) MARK COMMENTS ADDRESSED — resolve specific comment IDs on the PR. "
                "(5) POLL CI CHECKS — wait for all CI runs on the PR branch to complete and report "
                "CHECKS PASSING or CHECKS FAILING with failure details."
            ),
            prompt=REPO_MANAGER_SYSTEM_PROMPT,
            tools=["Bash", "Read"],
            model=s.haiku_model,
        ),
    }


# ---------------------------------------------------------------------------
# Agent configs — single source of truth for orchestrated mode
# ---------------------------------------------------------------------------

# Maps role → (system_prompt, tools, settings attribute name for model)
AGENT_CONFIGS: dict[str, tuple[str, list[str], str]] = {
    "lead-developer": (LEAD_DEV_SYSTEM_PROMPT,  ["Read", "Glob", "Grep", "WebSearch", "WebFetch"], "opus_model"),
    "developer":      (DEVELOPER_SYSTEM_PROMPT,  ["Read", "Write", "Edit", "Bash", "Glob", "Grep"], "sonnet_model"),
    "code-reviewer":  (CODE_REVIEWER_SYSTEM_PROMPT, ["Read", "Glob", "Grep"],                       "opus_model"),
    "qa-engineer":    (QA_SYSTEM_PROMPT,          ["Read", "Bash", "Glob", "Grep"],                 "opus_model"),
    "build-agent":    (BUILD_SYSTEM_PROMPT,        ["Bash", "Read", "Glob"],                        "haiku_model"),
    "repo-manager":   (REPO_MANAGER_SYSTEM_PROMPT, ["Bash", "Read"],                                "haiku_model"),
}


# ---------------------------------------------------------------------------
# Stage announcement parser
# ---------------------------------------------------------------------------

_STAGE_RE = re.compile(
    r"(Stage [1-4]|Design cycle \d+/\d+|Implementation cycle \d+/\d+|PR cycle \d+/\d+|CI check cycle \d+/\d+)",
    re.IGNORECASE,
)


def _parse_stage_announcement(text: str) -> str | None:
    m = _STAGE_RE.search(text)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Pipeline runners
# ---------------------------------------------------------------------------

def _make_env(settings: AppSettings) -> dict[str, str]:
    """Build the subprocess env dict: bypass nested-session guard + optional base URL."""
    env: dict[str, str] = {"CLAUDECODE": ""}
    if settings.api_base_url:
        env["ANTHROPIC_BASE_URL"] = settings.api_base_url
    return env


# ── Mode A: PM sub-agent ─────────────────────────────────────────────────────

async def run_pipeline_pm(
    task: str,
    settings: AppSettings,
    on_event: Callable[[dict], Awaitable[None]],
    pr_url: str = "",
    on_approval_required: Callable[[str], Awaitable[dict]] | None = None,
) -> str:
    """Orchestration via a PM LLM that uses the Task tool to spawn sub-agents."""
    prompt = task
    if pr_url:
        prompt = (
            f"{task}\n\nNOTE: A pull request already exists at {pr_url}. "
            "Skip Stages 1, 2, and 3. Begin at Stage 3b (CI check poll) then Stage 4 (PR poll loop)."
        )

    options = ClaudeAgentOptions(
        system_prompt=build_pm_prompt(settings),
        model=settings.pm_model,
        allowed_tools=["Task"],
        agents=build_agents(settings),
        permission_mode="acceptEdits",
        env=_make_env(settings),
    )

    result_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    stage = _parse_stage_announcement(block.text)
                    if stage:
                        await on_event({"type": "stage_changed", "stage": stage})
                    await on_event({"type": "agent_message", "role": "PM", "text": block.text[:300]})
                elif isinstance(block, ToolUseBlock) and block.name == "Task":
                    agent = block.input.get("subagent_type", "unknown")
                    await on_event({"type": "agent_started", "role": agent})
        elif isinstance(message, ResultMessage):
            result_text = message.result

    return result_text


# ── Helpers for orchestrated mode ────────────────────────────────────────────

def _reviewer_prompt(role: str, plan: str, diff_ctx: str) -> str:
    """Build the review prompt for a given reviewer role."""
    if role == "code-reviewer":
        return f"Review the implementation.{diff_ctx}"
    if role == "qa-engineer":
        return f"REVIEW CODE:\nOriginal plan:\n{plan}{diff_ctx}"
    return f"REVIEW IMPLEMENTATION:\nOriginal plan:\n{plan}{diff_ctx}"  # lead-developer


class ApprovalGate:
    """Async gate that suspends the pipeline until a human approves or provides feedback."""

    def __init__(self) -> None:
        self._future: asyncio.Future | None = None
        self.current_plan: str = ""

    @property
    def is_waiting(self) -> bool:
        return self._future is not None and not self._future.done()

    async def wait(self, plan: str = "") -> dict:
        self.current_plan = plan
        loop = asyncio.get_event_loop()
        self._future = loop.create_future()
        return await self._future

    def resolve(self, approved: bool, feedback: str = "") -> None:
        if self._future and not self._future.done():
            self._future.set_result({"approved": approved, "feedback": feedback})


# ── Mode B: Python-orchestrated team ─────────────────────────────────────────

def _extract_comment_ids(text: str) -> list[str]:
    """Best-effort extraction of comment IDs from repo-manager poll output."""
    return re.findall(r"\bID[:\s]+(\S+)", text, re.IGNORECASE)


async def run_pipeline_orchestrated(
    task: str,
    settings: AppSettings,
    on_event: Callable[[dict], Awaitable[None]],
    pr_url: str = "",
    on_approval_required: Callable[[str], Awaitable[dict]] | None = None,
) -> str:
    """Orchestration driven entirely by Python — no PM LLM, deterministic loops."""

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

    async def stage(label: str) -> None:
        await on_event({"type": "stage_changed", "stage": label})

    try:
        plan = task        # used in reviewer prompts; overwritten in Stage 1 unless skipping
        last_diff = ""
        pr_result = pr_url  # overwritten in Stage 3 unless pr_url was provided

        if not pr_url:
            # ── Stage 1: Design Loop ──────────────────────────────────────────────────
            await stage("Stage 1")
            plan = await call("lead-developer", task)

            for i in range(settings.max_design_cycles):
                await stage(f"Design cycle {i + 1}/{settings.max_design_cycles}")
                qa_result = await call("qa-engineer", f"REVIEW DESIGN:\n{plan}")
                if "APPROVED" in qa_result.upper():
                    break
                if i < settings.max_design_cycles - 1:
                    plan = await call(
                        "lead-developer",
                        f"REFINE the following plan based on QA findings.\n\nPlan:\n{plan}\n\nFindings:\n{qa_result}",
                    )

            # Optional human approval gate after design loop
            if on_approval_required is not None:
                while True:
                    gate_result = await on_approval_required(plan)
                    if gate_result.get("approved"):
                        break
                    feedback = gate_result.get("feedback", "")
                    if feedback:
                        plan = await call(
                            "lead-developer",
                            f"REFINE the following plan based on human feedback.\n\nPlan:\n{plan}\n\nFeedback:\n{feedback}",
                        )

            # ── Stage 2: Implementation Loop ─────────────────────────────────────────
            await stage("Stage 2")
            findings = ""
            already_approved: set[str] = set()

            for impl_cycle in range(settings.max_impl_cycles):
                await stage(f"Implementation cycle {impl_cycle + 1}/{settings.max_impl_cycles}")

                impl_prompt = plan if not findings else f"{plan}\n\nReview findings to address:\n{findings}"
                await call("developer", impl_prompt)

                # Build loop
                build_ok = False
                build_output = ""
                for build_cycle in range(settings.max_build_cycles):
                    build_output = await call("build-agent", "Build and test the project.")
                    if "SUCCESS" in build_output.upper():
                        build_ok = True
                        break
                    if build_cycle < settings.max_build_cycles - 1:
                        await call("developer", f"FIX BUILD FAILURE:\n{build_output}")

                if not build_ok:
                    findings = f"Build failed after {settings.max_build_cycles} retries:\n{build_output}"
                    break

                # Code review with smart skip — only re-invite approved reviewers on critical findings
                diff_ctx = f"\nGit diff for context:\n{last_diff}" if last_diff else ""
                pending = [r for r in _ALL_REVIEWERS if r not in already_approved]

                reviewer_results: dict[str, str] = {}
                if pending:
                    results = await asyncio.gather(
                        *[call(r, _reviewer_prompt(r, plan, diff_ctx)) for r in pending]
                    )
                    for role, result in zip(pending, results):
                        reviewer_results[role] = result
                        if "NEEDS IMPROVEMENT" not in result.upper():
                            already_approved.add(role)

                combined = "\n".join(reviewer_results.values())
                has_critical = any(kw in combined.upper() for kw in ("CRITICAL", "MUST HAVE", "MUST-HAVE"))
                if has_critical and already_approved:
                    reinvite = list(already_approved)
                    re_results = await asyncio.gather(
                        *[call(r, _reviewer_prompt(r, plan, diff_ctx)) for r in reinvite]
                    )
                    for role, result in zip(reinvite, re_results):
                        reviewer_results[role] = result
                        if "NEEDS IMPROVEMENT" in result.upper():
                            already_approved.discard(role)
                elif already_approved:
                    await on_event({
                        "type": "agent_message", "role": "orchestrator",
                        "text": f"Skipping approved reviewer(s): {', '.join(sorted(already_approved))}",
                    })

                findings = "\n\n---\n\n".join(
                    r for r in reviewer_results.values() if "NEEDS IMPROVEMENT" in r.upper()
                )
                if not findings:
                    break

            # ── Stage 3: Commit & PR ──────────────────────────────────────────────────
            await stage("Stage 3")
            pr_result = await call(
                "repo-manager",
                f"Commit all changes and create a Pull Request.\n"
                f"Branch: feature/{re.sub(r'[^a-z0-9]+', '-', task[:50].lower()).strip('-')}\n"
                f"Title: {task[:72]}\nDescription: Implemented via OLamo orchestrated pipeline.",
            )
            last_diff = pr_result

        # ── Stage 3b: CI Check Polling ────────────────────────────────────────────
        for ci_cycle in range(settings.max_pr_cycles):
            await stage(f"CI check cycle {ci_cycle + 1}/{settings.max_pr_cycles}")
            check_result = await call("repo-manager", "POLL CI CHECKS")
            if "CHECKS PASSING" in check_result.upper():
                break

            await call("developer", f"Fix the following CI check failures:\n{check_result}")

            build_output = await call("build-agent", "Build and test the project.")
            if "FAILURE" in build_output.upper():
                await call("developer", f"FIX BUILD FAILURE:\n{build_output}")
                await call("build-agent", "Build and test the project.")

            last_diff = await call("repo-manager", "PUSH CHANGES")

        # ── Stage 4: PR Poll Loop ─────────────────────────────────────────────────
        await stage("Stage 4")
        addressed_ids: list[str] = []

        for pr_cycle in range(settings.max_pr_cycles):
            await stage(f"PR cycle {pr_cycle + 1}/{settings.max_pr_cycles}")

            exclude = f" Exclude these IDs: {', '.join(addressed_ids)}" if addressed_ids else ""
            poll_result = await call("repo-manager", f"POLL PR COMMENTS.{exclude}")

            if "NO ACTIONABLE COMMENTS" in poll_result.upper():
                break

            new_ids = _extract_comment_ids(poll_result)
            if new_ids:
                addressed_ids.extend(new_ids)
                await call("repo-manager", f"MARK COMMENTS ADDRESSED: {', '.join(new_ids)}")

            await call("developer", f"Address the following PR review comments:\n{poll_result}")

            build_output = await call("build-agent", "Build and test the project.")
            if "FAILURE" in build_output.upper():
                await call("developer", f"FIX BUILD FAILURE:\n{build_output}")
                await call("build-agent", "Build and test the project.")

            # One reviewer pass after PR comment fix
            diff_ctx = f"\nGit diff for context:\n{last_diff}" if last_diff else ""
            reviews = await asyncio.gather(
                *[call(r, _reviewer_prompt(r, plan, diff_ctx)) for r in _ALL_REVIEWERS]
            )
            review_findings = "\n\n---\n\n".join(r for r in reviews if "NEEDS IMPROVEMENT" in r.upper())
            if review_findings:
                await call("developer", f"Address review findings before pushing:\n{review_findings}")
                build_output = await call("build-agent", "Build and test the project.")
                if "FAILURE" in build_output.upper():
                    await call("developer", f"FIX BUILD FAILURE:\n{build_output}")
                    await call("build-agent", "Build and test the project.")

            last_diff = await call("repo-manager", "PUSH CHANGES")

        return f"Pipeline complete. PR: {pr_result[:200]}"
    finally:
        await claude_engine.stop()
        if copilot_engine:
            await copilot_engine.stop()


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def run_pipeline(
    task: str,
    settings: AppSettings,
    on_event: Callable[[dict], Awaitable[None]],
    pr_url: str = "",
    on_approval_required: Callable[[str], Awaitable[dict]] | None = None,
) -> str:
    if settings.orchestration_mode == "orchestrated":
        return await run_pipeline_orchestrated(task, settings, on_event, pr_url, on_approval_required)
    return await run_pipeline_pm(task, settings, on_event, pr_url, on_approval_required)


async def run_pipeline_cli(task: str, pr_url: str = "") -> None:
    print(f"\n{'=' * 60}")
    print("OLamo Development Pipeline")
    print(f"{'=' * 60}")
    print(f"Task: {task}\n")

    async def on_event(evt: dict) -> None:
        t = evt.get("type")
        if t == "agent_started":
            print(f"\n>>> Delegating to [{evt['role'].upper()}] ...")
        elif t == "agent_message":
            print(f"[{evt['role'].upper()}] {evt['text']}")
        elif t == "stage_changed":
            print(f"\n{'─' * 40}")
            print(f"[STAGE] {evt['stage']}")
            print(f"{'─' * 40}")

    async def on_approval_required(plan: str) -> dict:
        print(f"\n{'=' * 60}")
        print("AWAITING DESIGN APPROVAL")
        print(f"{'=' * 60}")
        print(plan)
        print("\nEnter 'APPROVED' or type feedback to refine:")
        response = input("> ").strip()
        if response.upper() == "APPROVED":
            return {"approved": True, "feedback": ""}
        return {"approved": False, "feedback": response}

    try:
        result = await run_pipeline(task, AppSettings(), on_event, pr_url=pr_url, on_approval_required=on_approval_required)
        print(f"\n{'=' * 60}")
        print("Pipeline Complete")
        print(f"{'=' * 60}")
        print(result)
    except CLINotFoundError:
        print("Error: Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)
    except CLIConnectionError as e:
        print(f"Error: Could not connect to Claude Code CLI: {e}")
        sys.exit(1)
    except ClaudeSDKError as e:
        print(f"Error: SDK error: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Web server infrastructure
# ---------------------------------------------------------------------------

class SettingsStore:
    def __init__(self) -> None:
        self._settings = AppSettings()
        self._locked = False
        self._pending: AppSettings | None = None
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> AppSettings:
        return self._settings

    @property
    def is_locked(self) -> bool:
        return self._locked

    async def lock(self) -> None:
        async with self._lock:
            self._locked = True

    async def unlock(self) -> None:
        async with self._lock:
            self._locked = False
            if self._pending is not None:
                self._settings = self._pending
                self._pending = None

    async def try_update(self, new_settings: AppSettings) -> bool:
        async with self._lock:
            if self._locked:
                self._pending = new_settings
                return False
            self._settings = new_settings
            return True


class SseBroadcaster:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def connect(self) -> tuple[str, asyncio.Queue]:
        cid = str(uuid.uuid4())
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._queues[cid] = q
        return cid, q

    async def disconnect(self, cid: str) -> None:
        async with self._lock:
            q = self._queues.pop(cid, None)
        if q is not None:
            await q.put(None)  # sentinel

    async def broadcast(self, event: dict) -> None:
        data = json.dumps(event)
        async with self._lock:
            queues = list(self._queues.values())
        for q in queues:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass


class OLamoDb:
    """
    Thin async SQLite wrapper using aiosqlite.

    Three tables mirror OLaCo's schema:
      - runs       — one row per run; upserted on every status change
      - events     — append-only stream (seq AUTOINCREMENT) of pipeline events
      - run_state  — live single-row projection of the current stage per run

    aiosqlite serialises all writes through a background thread queue, which is
    semantically equivalent to OLaCo's SemaphoreSlim(1,1) write lock.
    WAL mode allows concurrent readers alongside the single writer.
    """

    def __init__(self, path: str = "olamo.db") -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None  # type: ignore

    async def open(self) -> None:
        if aiosqlite is None:
            raise SystemExit("aiosqlite not installed. Run: pip install aiosqlite")
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._ensure_schema()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _ensure_schema(self) -> None:
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id                TEXT PRIMARY KEY,
                description       TEXT NOT NULL,
                status            TEXT NOT NULL,
                queued_at         TEXT NOT NULL,
                started_at        TEXT,
                completed_at      TEXT,
                error             TEXT,
                log_dir           TEXT,
                pr_url            TEXT NOT NULL DEFAULT '',
                settings_override TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS events (
                seq    INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts     TEXT NOT NULL,
                data   TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);

            CREATE TABLE IF NOT EXISTS run_state (
                run_id        TEXT PRIMARY KEY,
                current_stage TEXT,
                updated_at    TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
        """)
        await self._conn.commit()

    def _row_to_run(self, row: aiosqlite.Row) -> "RunRecord":  # type: ignore
        return RunRecord(
            id=row["id"],
            description=row["description"],
            status=RunStatus(row["status"]),
            queued_at=row["queued_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
            log_dir=row["log_dir"],
            pr_url=row["pr_url"] or "",
            settings_override=json.loads(row["settings_override"] or "{}"),
        )

    async def upsert_run(self, run: "RunRecord") -> None:
        await self._conn.execute(
            """
            INSERT INTO runs
              (id, description, status, queued_at, started_at, completed_at, error, log_dir, pr_url, settings_override)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status            = excluded.status,
                started_at        = excluded.started_at,
                completed_at      = excluded.completed_at,
                error             = excluded.error,
                log_dir           = excluded.log_dir
            """,
            (
                run.id, run.description, run.status.value, run.queued_at,
                run.started_at, run.completed_at, run.error, run.log_dir,
                run.pr_url, json.dumps(run.settings_override),
            ),
        )
        await self._conn.commit()

    async def get_all_runs(self) -> list["RunRecord"]:
        async with self._conn.execute(
            "SELECT * FROM runs ORDER BY queued_at DESC"
        ) as cur:
            return [self._row_to_run(row) async for row in cur]

    async def insert_event(self, run_id: str, data: dict) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        cur = await self._conn.execute(
            "INSERT INTO events (run_id, ts, data) VALUES (?, ?, ?)",
            (run_id, ts, json.dumps(data)),
        )
        await self._conn.commit()
        return cur.lastrowid  # equivalent to OLaCo's last_insert_rowid()

    async def get_events(self, run_id: str) -> list[dict]:
        async with self._conn.execute(
            "SELECT data FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ) as cur:
            return [json.loads(row["data"]) async for row in cur]

    async def upsert_run_state(self, run_id: str, current_stage: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO run_state (run_id, current_stage, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                current_stage = excluded.current_stage,
                updated_at    = excluded.updated_at
            """,
            (run_id, current_stage, ts),
        )
        await self._conn.commit()


class RunManager:
    def __init__(self, broadcaster: SseBroadcaster, store: SettingsStore, db_path: str = "olamo.db") -> None:
        self._broadcaster = broadcaster
        self._store = store
        self._db = OLamoDb(db_path)
        self._runs: dict[str, RunRecord] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self.pending_approvals: dict[str, ApprovalGate] = {}

    async def setup(self) -> None:
        """Open DB connection, ensure schema, and load existing runs into memory."""
        await self._db.open()
        for run in await self._db.get_all_runs():
            self._runs[run.id] = run

    async def close(self) -> None:
        await self._db.close()

    async def enqueue(self, description: str, pr_url: str = "", settings_override: dict | None = None) -> RunRecord:
        run = RunRecord(
            id=str(uuid.uuid4()),
            description=description,
            pr_url=pr_url,
            settings_override=settings_override or {},
        )
        self._runs[run.id] = run
        await self._db.upsert_run(run)
        self._queue.put_nowait(run.id)
        return run

    async def get_run_events(self, run_id: str) -> list[dict]:
        return await self._db.get_events(run_id)

    @property
    def all_runs(self) -> list[RunRecord]:
        return sorted(self._runs.values(), key=lambda r: r.queued_at, reverse=True)

    def get_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def start(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        while True:
            run_id = await self._queue.get()
            run = self._runs.get(run_id)
            if run is not None:
                await self._execute_run(run)

    async def _execute_run(self, run: RunRecord) -> None:
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc).isoformat()
        log_dir = Path("logs") / run.id
        log_dir.mkdir(parents=True, exist_ok=True)
        run.log_dir = str(log_dir)
        await self._db.upsert_run(run)

        # Apply per-run settings override on top of global settings
        base = self._store.settings
        if run.settings_override:
            fields = AppSettings.__dataclass_fields__
            filtered = {k: v for k, v in run.settings_override.items() if k in fields}
            settings = AppSettings(**{**asdict(base), **filtered})
        else:
            settings = base

        await self._store.lock()

        gate = ApprovalGate()
        self.pending_approvals[run.id] = gate

        async def on_event(evt: dict) -> None:
            await self._broadcaster.broadcast(evt)
            await self._db.insert_event(run.id, evt)
            if evt.get("type") == "stage_changed":
                await self._db.upsert_run_state(run.id, evt["stage"])

        async def on_approval_required(plan: str) -> dict:
            await self._broadcaster.broadcast({"type": "approval_required", "run_id": run.id, "plan": plan})
            return await gate.wait(plan)

        try:
            result = await run_pipeline(
                run.description, settings, on_event,
                pr_url=run.pr_url,
                on_approval_required=on_approval_required,
            )
            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc).isoformat()
            await self._db.upsert_run(run)
            await self._broadcaster.broadcast(
                {"type": "run_completed", "run_id": run.id, "status": RunStatus.COMPLETED, "result": result[:500]}
            )
        except Exception as e:
            run.status = RunStatus.FAILED
            run.completed_at = datetime.now(timezone.utc).isoformat()
            run.error = str(e)
            await self._db.upsert_run(run)
            await self._broadcaster.broadcast(
                {"type": "run_completed", "run_id": run.id, "status": RunStatus.FAILED, "error": str(e)}
            )
        finally:
            self.pending_approvals.pop(run.id, None)
            await self._store.unlock()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

def create_app():  # noqa: ANN201
    try:
        from contextlib import asynccontextmanager

        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, JSONResponse
        from sse_starlette.sse import EventSourceResponse
    except ImportError as exc:
        raise SystemExit(
            "Web dependencies missing. Install with:\n"
            "  pip install fastapi uvicorn[standard] sse-starlette aiofiles aiosqlite"
        ) from exc

    broadcaster = SseBroadcaster()
    store = SettingsStore()
    manager = RunManager(broadcaster, store)
    static_dir = Path(__file__).parent / "static"

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN001
        await manager.setup()
        manager.start()
        yield
        await manager.close()

    app = FastAPI(title="OLamo", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/events")
    async def sse_stream() -> EventSourceResponse:
        cid, q = await broadcaster.connect()

        async def generator():
            try:
                while True:
                    data = await q.get()
                    if data is None:
                        break
                    yield {"data": data}
            finally:
                await broadcaster.disconnect(cid)

        return EventSourceResponse(generator())

    @app.get("/api/runs")
    async def list_runs() -> list[dict]:
        return [asdict(r) for r in manager.all_runs]

    @app.post("/api/runs", status_code=201)
    async def create_run(request: Request) -> dict:
        body = await request.json()
        description = (body.get("description") or "").strip()
        if not description:
            raise HTTPException(status_code=400, detail="description required")
        pr_url = (body.get("pr_url") or "").strip()
        settings_override = body.get("settings_override") or {}
        run = await manager.enqueue(description, pr_url=pr_url, settings_override=settings_override)
        return asdict(run)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        run = manager.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return asdict(run)

    @app.get("/api/runs/{run_id}/approval")
    async def get_approval(run_id: str) -> dict:
        if manager.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        gate = manager.pending_approvals.get(run_id)
        if gate is None or not gate.is_waiting:
            return {"waiting": False, "plan": ""}
        return {"waiting": True, "plan": gate.current_plan}

    @app.post("/api/runs/{run_id}/approval")
    async def resolve_approval(run_id: str, request: Request) -> dict:
        if manager.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        gate = manager.pending_approvals.get(run_id)
        if gate is None or not gate.is_waiting:
            raise HTTPException(status_code=409, detail="run is not awaiting approval")
        body = await request.json()
        gate.resolve(bool(body.get("approved", False)), (body.get("feedback") or "").strip())
        return {"ok": True}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> list[dict]:
        if manager.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return await manager.get_run_events(run_id)

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

    @app.get("/api/settings")
    async def get_settings() -> dict:
        return {"config": asdict(store.settings), "is_locked": store.is_locked}

    @app.put("/api/settings")
    async def update_settings(request: Request) -> dict:
        body = await request.json()
        try:
            current = asdict(store.settings)
            merged = {**current, **{k: v for k, v in body.items()
                                    if k in AppSettings.__dataclass_fields__}}
            new_settings = _settings_from_dict(merged)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=str(e))
        applied = await store.try_update(new_settings)
        return {"applied": applied, "config": asdict(store.settings)}

    @app.get("/{path:path}")
    async def spa_fallback(path: str) -> FileResponse:
        index = static_dir / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(status_code=404)

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="OLamo development pipeline")
    parser.add_argument("task", nargs="*", help="Task description (CLI mode)")
    parser.add_argument("--server", action="store_true", help="Run web server")
    parser.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    parser.add_argument("--pr-url", default="", help="Existing PR URL — skip Stages 1-3 and resume from CI check poll")
    args = parser.parse_args()

    if args.server:
        try:
            import uvicorn
        except ImportError:
            print("uvicorn not installed. Run: pip install uvicorn[standard]")
            sys.exit(1)
        print(f"Starting OLamo server on http://0.0.0.0:{args.port}")
        uvicorn.run(create_app(), host="0.0.0.0", port=args.port)
        return

    if args.task:
        task = " ".join(args.task)
    else:
        task = input("Describe the task for OLamo: ").strip()
        if not task:
            print("No task provided. Exiting.")
            sys.exit(1)

    asyncio.run(run_pipeline_cli(task, pr_url=args.pr_url))


if __name__ == "__main__":
    main()
