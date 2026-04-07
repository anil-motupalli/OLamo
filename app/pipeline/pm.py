"""run_pipeline_pm — PM LLM sub-agent orchestration mode."""

from __future__ import annotations

from typing import Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from ..models import AppSettings
from ..agents import build_agents, build_pm_prompt
from .helpers import _make_env, _parse_stage_announcement


async def run_pipeline_pm(
    task: str,
    settings: AppSettings,
    on_event: Callable[[dict], Awaitable[None]],
    pr_url: str = "",
    on_approval_required: Callable[[str], Awaitable[dict]] | None = None,
    run_id: str | None = None,
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
