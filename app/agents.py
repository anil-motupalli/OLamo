"""System prompts, agent configs, build_agents(), and build_pm_prompt()."""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from .models import AppSettings
from .constants import AGENT_TOOLS, _CLAUDE_TIER
from .prompts import load_character, load_prompt


def build_pm_prompt(s: AppSettings) -> str:
    return load_prompt("pm", "pipeline", {
        "max_design_cycles": str(s.max_design_cycles),
        "max_build_cycles":  str(s.max_build_cycles),
        "max_impl_cycles":   str(s.max_impl_cycles),
        "max_pr_cycles":     str(s.max_pr_cycles),
    })


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
            prompt=load_character("lead-developer"),
            tools=AGENT_TOOLS["lead-developer"],
            model=s.opus_model,
        ),
        "developer": AgentDefinition(
            description=(
                "Developer. Implements code exactly as specified in the plan. "
                "Also fixes issues when given review findings (FIX ISSUES) or build failures "
                "(FIX BUILD FAILURE). Does nothing except write code."
            ),
            prompt=load_character("developer"),
            tools=AGENT_TOOLS["developer"],
            model=s.sonnet_model,
        ),
        "code-reviewer": AgentDefinition(
            description=(
                "Code Reviewer. Reviews code for bugs, security vulnerabilities, performance issues, "
                "and code quality problems. Reports findings with file, severity, and suggested fix. "
                "Use after developer finishes implementation, as part of the parallel review phase."
            ),
            prompt=load_character("code-reviewer"),
            tools=AGENT_TOOLS["code-reviewer"],
            model=s.opus_model,
        ),
        "qa-engineer": AgentDefinition(
            description=(
                "QA Engineer. Two modes: (1) REVIEW DESIGN — evaluate the implementation plan for "
                "testability and completeness before any code is written. "
                "(2) REVIEW CODE — run tests, verify implementation matches requirements, "
                "and report PASS/FAIL per test scenario."
            ),
            prompt=load_character("qa-engineer"),
            tools=AGENT_TOOLS["qa-engineer"],
            model=s.opus_model,
        ),
        "build-agent": AgentDefinition(
            description=(
                "Build Agent. Installs dependencies, runs the build/compilation, and runs a smoke test. "
                "Reports SUCCESS or FAILURE with complete command output (including full error text on "
                "failure). Use after developer finishes implementation."
            ),
            prompt=load_character("build-agent"),
            tools=AGENT_TOOLS["build-agent"],
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
            prompt=load_character("repo-manager"),
            tools=AGENT_TOOLS["repo-manager"],
            model=s.haiku_model,
        ),
    }


# ---------------------------------------------------------------------------
# Agent configs — single source of truth for orchestrated mode
# ---------------------------------------------------------------------------

# Maps role → (system_prompt, tools, settings attribute name for model)
AGENT_CONFIGS: dict[str, tuple[str, list[str], str]] = {
    role: (load_character(role), AGENT_TOOLS[role], _CLAUDE_TIER[role])
    for role in AGENT_TOOLS
}


# ---------------------------------------------------------------------------
# Backward compat — tests import these names from app (via app/__init__.py)
# ---------------------------------------------------------------------------
LEAD_DEV_SYSTEM_PROMPT      = load_character("lead-developer")
DEVELOPER_SYSTEM_PROMPT     = load_character("developer")
QA_SYSTEM_PROMPT            = load_character("qa-engineer")
CODE_REVIEWER_SYSTEM_PROMPT = load_character("code-reviewer")
BUILD_SYSTEM_PROMPT         = load_character("build-agent")
REPO_MANAGER_SYSTEM_PROMPT  = load_character("repo-manager")
