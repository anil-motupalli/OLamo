"""run_pipeline() dispatcher and run_pipeline_cli() for CLI mode."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Awaitable, Callable

from claude_agent_sdk import CLIConnectionError, CLINotFoundError, ClaudeSDKError

from ..models import AppSettings
from ..settings import SettingsStore
from .pm import run_pipeline_pm
from .orchestrated import run_pipeline_orchestrated


async def run_pipeline(
    task: str,
    settings: AppSettings,
    on_event: Callable[[dict], Awaitable[None]],
    pr_url: str = "",
    on_approval_required: Callable[[str], Awaitable[dict]] | None = None,
    checkpoint: dict | None = None,
    save_checkpoint: Callable[[dict], Awaitable[None]] | None = None,
    log_dir: str | None = None,
) -> str:
    if settings.orchestration_mode == "orchestrated":
        return await run_pipeline_orchestrated(
            task, settings, on_event, pr_url, on_approval_required,
            checkpoint=checkpoint, save_checkpoint=save_checkpoint,
            log_dir=log_dir,
        )
    return await run_pipeline_pm(task, settings, on_event, pr_url, on_approval_required)


async def run_pipeline_cli(
    task: str,
    pr_url: str = "",
    settings_file: Path | None = None,
    headless: bool = False,
) -> None:
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
        settings = SettingsStore(settings_file=settings_file).settings
        if headless:
            from dataclasses import replace
            settings = replace(settings, headless=True, orchestration_mode="orchestrated")
            print("[HEADLESS MODE] Using MockEngine — no real API calls will be made.\n")
        result = await run_pipeline(task, settings, on_event, pr_url=pr_url, on_approval_required=on_approval_required)
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
