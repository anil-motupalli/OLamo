"""RunManager — queues, executes, and tracks pipeline runs (parallel + resumable)."""

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

_DEFAULT_MAX_CONCURRENT = 5


class RunManager:
    def __init__(
        self,
        broadcaster: SseBroadcaster,
        store: "SettingsStore",
        db_path: str = "olamo.db",
        max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
    ) -> None:
        self._broadcaster = broadcaster
        self._store = store
        self._db = OLamoDb(db_path)
        self._runs: dict[str, RunRecord] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.pending_approvals: dict[str, ApprovalGate] = {}

    async def setup(self) -> None:
        """Open DB, ensure schema, surface stale runs.

        Tasks that were ``running`` when the server last stopped become
        ``interrupted`` — they can be resumed manually via ``resume()``.
        Tasks that were ``queued`` but never started are left as-is; they
        will be picked up automatically once the server is healthy.
        """
        await self._db.open()
        for run in await self._db.get_all_runs():
            self._runs[run.id] = run
            if run.status == RunStatus.RUNNING:
                run.status = RunStatus.INTERRUPTED
                await self._db.upsert_run(run)
                await self._db.upsert_run_state(run.id, current_stage="interrupted")
            elif run.status == RunStatus.QUEUED:
                self._spawn(run)

    async def resume(self, run_id: str) -> RunRecord | None:
        """Re-queue an interrupted run so it resumes from its last checkpoint."""
        run = self._runs.get(run_id)
        if run is None or run.status != RunStatus.INTERRUPTED:
            return None
        run.status = RunStatus.QUEUED
        run.started_at = None
        run.error = None
        await self._db.upsert_run(run)
        self._spawn(run)
        return run

    async def close(self) -> None:
        # Cancel all active tasks cleanly
        for task in list(self._active_tasks.values()):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)
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
        await self._db.insert_event(run.id, {"type": "run_queued", "run_id": run.id, "description": description})
        await self._broadcaster.broadcast({"type": "run_queued", "run_id": run.id, "description": description})
        self._spawn(run)
        return run

    def _spawn(self, run: RunRecord) -> None:
        """Create an asyncio Task for a run (respects semaphore for concurrency)."""
        task = asyncio.create_task(self._run_with_semaphore(run), name=f"run-{run.id}")
        self._active_tasks[run.id] = task
        task.add_done_callback(lambda _: self._active_tasks.pop(run.id, None))

    async def _run_with_semaphore(self, run: RunRecord) -> None:
        async with self._semaphore:
            await self._execute_run(run)

    async def get_run_events(self, run_id: str) -> list[dict]:
        return await self._db.get_events(run_id)

    async def get_run_state(self, run_id: str) -> dict | None:
        return await self._db.get_run_state(run_id)

    async def get_event_content_path(self, run_id: str, seq: int) -> str | None:
        return await self._db.get_event_content_path(run_id, seq)

    @property
    def all_runs(self) -> list[RunRecord]:
        return sorted(self._runs.values(), key=lambda r: r.queued_at, reverse=True)

    def get_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def start(self) -> None:
        pass  # No longer needed — tasks are spawned directly in setup()/enqueue()

    async def _execute_run(self, run: RunRecord) -> None:
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc).isoformat()
        log_dir = Path("logs") / run.id
        log_dir.mkdir(parents=True, exist_ok=True)
        run.log_dir = str(log_dir)
        await self._db.upsert_run(run)
        await self._db.upsert_run_state(run.id, current_stage="running")
        await self._store.lock()

        started_evt = {"type": "run_started", "run_id": run.id}
        await self._broadcaster.broadcast(started_evt)
        await self._db.insert_event(run.id, started_evt)

        # Apply per-run settings override on top of global settings
        base = self._store.settings
        if run.settings_override:
            fields = set(AppSettings.__dataclass_fields__) - {"agent_configs"}
            filtered = {k: v for k, v in run.settings_override.items() if k in fields}
            base_dict = {k: v for k, v in asdict(base).items() if k != "agent_configs"}
            settings = AppSettings(**{**base_dict, **filtered}, agent_configs=base.agent_configs)
            run_agent_overrides = run.settings_override.get("agent_configs", {})
            if run_agent_overrides:
                merged_agents = dict(settings.agent_configs)
                for role, cfg_dict in run_agent_overrides.items():
                    merged_agents[role] = _agent_engine_config_from_dict(cfg_dict)
                settings = replace(settings, agent_configs=merged_agents)
        else:
            settings = base

        # Load checkpoint for resumability
        checkpoint = await self._db.load_checkpoint(run.id)

        gate = ApprovalGate()
        self.pending_approvals[run.id] = gate

        async def on_event(evt: dict) -> None:
            await self._broadcaster.broadcast(evt)
            seq = await self._db.insert_event(run.id, evt)
            evt_type = evt.get("type")
            if evt_type == "stage_changed":
                stage = evt["stage"]
                cycle_info = _parse_stage_announcement(stage)
                await self._db.upsert_run_state(run.id, current_stage=stage, current_cycle=cycle_info)
            elif evt_type == "agent_started":
                await self._db.upsert_run_state(run.id, last_agent=evt.get("role"))
            elif evt_type == "agent_completed":
                await self._db.upsert_run_state(
                    run.id,
                    last_agent=evt.get("role"),
                    last_agent_ok=evt.get("success"),
                    last_summary=evt.get("summary"),
                )
            # Attach seq back onto the event so listeners can use it (e.g. for content fetch)
            evt["seq"] = seq

        async def on_approval_required(spec: str) -> dict:
            # Store spec content to disk so the UI can fetch it
            spec_seq_placeholder = {"type": "awaiting_approval", "run_id": run.id}
            content_dir = log_dir / "content"
            content_dir.mkdir(parents=True, exist_ok=True)
            # We broadcast first to get a seq, then update content_path
            spec_summary = spec[:300].rstrip()
            evt = {
                "type": "awaiting_approval",
                "run_id": run.id,
                "specSummary": spec_summary,
                "developerResponse": "",
            }
            await self._broadcaster.broadcast(evt)
            seq = await self._db.insert_event(run.id, evt)
            spec_path = content_dir / f"spec-{seq}.md"
            spec_path.write_text(spec, encoding="utf-8")
            # Update the DB row to record the content_path
            await self._db.update_event_content_path(run_id=run.id, seq=seq, content_path=str(spec_path))
            # Tell the gate to wait
            return await gate.wait(spec)

        async def save_ckpt(data: dict) -> None:
            await self._db.save_checkpoint(run.id, data)

        try:
            result = await run_pipeline(
                run.description, settings, on_event,
                pr_url=run.pr_url,
                on_approval_required=on_approval_required,
                checkpoint=checkpoint,
                save_checkpoint=save_ckpt,
                log_dir=str(log_dir),
            )
            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc).isoformat()
            await self._db.upsert_run(run)
            await self._db.upsert_run_state(run.id, current_stage="completed")
            completed_evt = {"type": "run_completed", "run_id": run.id, "status": "completed", "result": result[:500]}
            await self._broadcaster.broadcast(completed_evt)
            await self._db.insert_event(run.id, completed_evt)
        except Exception as e:
            run.status = RunStatus.FAILED
            run.completed_at = datetime.now(timezone.utc).isoformat()
            run.error = str(e)
            await self._db.upsert_run(run)
            await self._db.upsert_run_state(run.id, current_stage="failed")
            failed_evt = {"type": "run_completed", "run_id": run.id, "status": "failed", "error": str(e)}
            await self._broadcaster.broadcast(failed_evt)
            await self._db.insert_event(run.id, failed_evt)
        finally:
            self.pending_approvals.pop(run.id, None)
            await self._store.unlock()
