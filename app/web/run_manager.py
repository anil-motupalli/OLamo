"""RunManager — queues, executes, and tracks pipeline runs."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from ..models import (
    AppSettings,
    RunRecord,
    RunStatus,
    _agent_engine_config_from_dict,
)
from ..pipeline.helpers import ApprovalGate, _parse_stage_announcement
from ..pipeline.runner import run_pipeline
from .broadcaster import SseBroadcaster
from .database import OLamoDb


class RunManager:
    def __init__(self, broadcaster: SseBroadcaster, store: "SettingsStore", db_path: str = "olamo.db") -> None:
        self._broadcaster = broadcaster
        self._store = store
        self._db = OLamoDb(db_path)
        self._runs: dict[str, RunRecord] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self.pending_approvals: dict[str, ApprovalGate] = {}

    async def setup(self) -> None:
        """Open DB connection, ensure schema, and load existing runs into memory.

        Stale ``running`` tasks (from a previous server session that crashed or
        was restarted) are marked ``failed`` so the UI never shows phantom
        in-progress runs.  Stale ``queued`` tasks are re-enqueued so they will
        be picked up by the worker as soon as it starts.
        """
        await self._db.open()
        for run in await self._db.get_all_runs():
            self._runs[run.id] = run
            if run.status == RunStatus.RUNNING:
                run.status = RunStatus.FAILED
                run.error = "Server restarted while task was in progress"
                run.completed_at = datetime.now(timezone.utc).isoformat()
                await self._db.upsert_run(run)
                await self._db.upsert_run_state(run.id, current_stage="failed")
            elif run.status == RunStatus.QUEUED:
                self._queue.put_nowait(run.id)

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

    async def get_run_state(self, run_id: str) -> dict | None:
        return await self._db.get_run_state(run_id)

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
        # Initialise run_state so the /state endpoint is immediately queryable
        await self._db.upsert_run_state(run.id, current_stage="running")

        # Apply per-run settings override on top of global settings
        base = self._store.settings
        if run.settings_override:
            # Exclude agent_configs from scalar merge — it contains AgentEngineConfig objects
            # and must be merged separately below using dataclasses.replace
            fields = set(AppSettings.__dataclass_fields__) - {"agent_configs"}
            filtered = {k: v for k, v in run.settings_override.items() if k in fields}
            base_dict = {k: v for k, v in asdict(base).items() if k != "agent_configs"}
            settings = AppSettings(**{**base_dict, **filtered}, agent_configs=base.agent_configs)

            # Per-run agent config override (shallow per-role merge)
            run_agent_overrides = run.settings_override.get("agent_configs", {})
            if run_agent_overrides:
                merged_agents = dict(settings.agent_configs)
                for role, cfg_dict in run_agent_overrides.items():
                    merged_agents[role] = _agent_engine_config_from_dict(cfg_dict)
                settings = replace(settings, agent_configs=merged_agents)
        else:
            settings = base

        await self._store.lock()

        gate = ApprovalGate()
        self.pending_approvals[run.id] = gate

        async def on_event(evt: dict) -> None:
            await self._broadcaster.broadcast(evt)
            await self._db.insert_event(run.id, evt)
            if evt.get("type") == "stage_changed":
                stage = evt["stage"]
                cycle_info = _parse_stage_announcement(stage)
                await self._db.upsert_run_state(run.id, current_stage=stage, current_cycle=cycle_info)
            elif evt.get("type") == "agent_started":
                # Only update last_agent — do NOT overwrite current_stage
                await self._db.upsert_run_state(run.id, last_agent=evt.get("role"))

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
            await self._db.upsert_run_state(run.id, current_stage="completed")
            await self._broadcaster.broadcast(
                {"type": "run_completed", "run_id": run.id, "status": RunStatus.COMPLETED, "result": result[:500]}
            )
        except Exception as e:
            run.status = RunStatus.FAILED
            run.completed_at = datetime.now(timezone.utc).isoformat()
            run.error = str(e)
            await self._db.upsert_run(run)
            await self._db.upsert_run_state(run.id, current_stage="failed")
            await self._broadcaster.broadcast(
                {"type": "run_completed", "run_id": run.id, "status": RunStatus.FAILED, "error": str(e)}
            )
        finally:
            self.pending_approvals.pop(run.id, None)
            await self._store.unlock()
