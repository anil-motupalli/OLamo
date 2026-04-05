"""Tests for app.web.run_manager.RunManager."""

import asyncio
from unittest.mock import patch

import pytest
import pytest_asyncio

from app.models import RunRecord, RunStatus
from app.settings import SettingsStore
from app.web.broadcaster import SseBroadcaster
from app.web.database import OLamoDb
from app.web.run_manager import RunManager


@pytest_asyncio.fixture()
async def run_manager(tmp_path, monkeypatch):
    """Fresh RunManager backed by an in-process SQLite DB in a temp directory."""
    monkeypatch.chdir(tmp_path)
    mgr = RunManager(SseBroadcaster(), SettingsStore(), db_path=str(tmp_path / "test.db"))
    await mgr.setup()
    yield mgr
    await mgr.close()


class TestRunManager:
    @pytest.mark.asyncio
    async def test_enqueue_returns_run_with_queued_status(self, run_manager):
        run = await run_manager.enqueue("add hello world")
        assert run.description == "add hello world"
        assert run.status == RunStatus.QUEUED

    @pytest.mark.asyncio
    async def test_enqueued_run_appears_in_all_runs(self, run_manager):
        run = await run_manager.enqueue("my task")
        ids = [r.id for r in run_manager.all_runs]
        assert run.id in ids

    @pytest.mark.asyncio
    async def test_get_run_retrieves_by_id(self, run_manager):
        run = await run_manager.enqueue("findable")
        found = run_manager.get_run(run.id)
        assert found is not None
        assert found.id == run.id
        assert found.description == "findable"

    @pytest.mark.asyncio
    async def test_get_run_returns_none_for_unknown_id(self, run_manager):
        assert run_manager.get_run("does-not-exist") is None

    @pytest.mark.asyncio
    async def test_all_runs_sorted_newest_first(self, run_manager):
        r1 = await run_manager.enqueue("first")
        r2 = await run_manager.enqueue("second")
        runs = run_manager.all_runs
        assert runs[0].id == r2.id
        assert runs[1].id == r1.id

    @pytest.mark.asyncio
    async def test_persists_runs_to_db(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        db_path = str(tmp_path / "shared.db")
        mgr1 = RunManager(SseBroadcaster(), SettingsStore(), db_path=db_path)
        await mgr1.setup()
        run = await mgr1.enqueue("persist me")
        await mgr1.close()

        mgr2 = RunManager(SseBroadcaster(), SettingsStore(), db_path=db_path)
        await mgr2.setup()
        found = mgr2.get_run(run.id)
        # Capture description and status before close() may mutate the run object
        found_description = found.description if found else None
        found_id = found.id if found else None
        await mgr2.close()
        assert found is not None
        assert found_description == "persist me"
        assert found_id == run.id

    @pytest.mark.asyncio
    async def test_multiple_enqueues_all_persisted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        db_path = str(tmp_path / "shared.db")
        mgr = RunManager(SseBroadcaster(), SettingsStore(), db_path=db_path)
        await mgr.setup()
        ids = {(await mgr.enqueue(f"task {i}")).id for i in range(3)}
        await mgr.close()

        mgr2 = RunManager(SseBroadcaster(), SettingsStore(), db_path=db_path)
        await mgr2.setup()
        assert {r.id for r in mgr2.all_runs} == ids
        await mgr2.close()

    @pytest.mark.asyncio
    async def test_enqueue_with_pr_url(self, run_manager):
        run = await run_manager.enqueue("fix PR", pr_url="https://github.com/org/repo/pull/7")
        assert run.pr_url == "https://github.com/org/repo/pull/7"

    @pytest.mark.asyncio
    async def test_enqueue_with_settings_override(self, run_manager):
        run = await run_manager.enqueue("task", settings_override={"max_impl_cycles": 1})
        assert run.settings_override == {"max_impl_cycles": 1}

    @pytest.mark.asyncio
    async def test_enqueue_default_pr_url_is_empty(self, run_manager):
        run = await run_manager.enqueue("task")
        assert run.pr_url == ""

    @pytest.mark.asyncio
    async def test_enqueue_default_settings_override_is_empty(self, run_manager):
        run = await run_manager.enqueue("task")
        assert run.settings_override == {}

    @pytest.mark.asyncio
    async def test_pending_approvals_starts_empty(self, run_manager):
        assert run_manager.pending_approvals == {}

    @pytest.mark.asyncio
    async def test_setup_resets_stale_running_to_interrupted(self, tmp_path, monkeypatch):
        """Stale 'running' tasks from a crashed session become 'interrupted' — not auto-resumed."""
        monkeypatch.chdir(tmp_path)
        db_path = str(tmp_path / "shared.db")

        mgr1 = RunManager(SseBroadcaster(), SettingsStore(), db_path=db_path)
        await mgr1.setup()
        run = await mgr1.enqueue("stale task")
        # Simulate a crash by marking the run as running in the DB
        run.status = RunStatus.RUNNING
        run.started_at = "2024-01-01T00:00:00+00:00"
        await mgr1._db.upsert_run(run)
        await mgr1.close()

        mgr2 = RunManager(SseBroadcaster(), SettingsStore(), db_path=db_path)
        await mgr2.setup()
        found = mgr2.get_run(run.id)
        found_status = found.status if found else None
        await mgr2.close()

        assert found is not None
        assert found_status == RunStatus.INTERRUPTED, "Stale running task must become 'interrupted'"

    @pytest.mark.asyncio
    async def test_multiple_tasks_spawned_concurrently(self, tmp_path, monkeypatch):
        """Enqueueing multiple runs spawns concurrent asyncio Tasks (not serialised through a queue)."""
        monkeypatch.chdir(tmp_path)
        mgr = RunManager(
            SseBroadcaster(), SettingsStore(),
            db_path=str(tmp_path / "test.db"),
            max_concurrent=5,
        )
        await mgr.setup()

        await mgr.enqueue("task 1")
        await mgr.enqueue("task 2")
        await mgr.enqueue("task 3")

        # All 3 tasks should be tracked in _active_tasks immediately after enqueue
        assert len(mgr._active_tasks) == 3

        await mgr.close()
        assert len(mgr._active_tasks) == 0

    @pytest.mark.asyncio
    async def test_execute_run_passes_checkpoint_and_save_fn_to_pipeline(self, tmp_path, monkeypatch):
        """_execute_run loads checkpoint from DB and passes it + a save callable to run_pipeline."""
        monkeypatch.chdir(tmp_path)

        received_kwargs: dict = {}

        async def fake_pipeline(task, settings, on_event, **kwargs):
            received_kwargs.update(kwargs)
            return "done"

        with patch("app.web.run_manager.run_pipeline", fake_pipeline):
            mgr = RunManager(SseBroadcaster(), SettingsStore(), db_path=str(tmp_path / "test.db"))
            await mgr.setup()
            run = await mgr.enqueue("checkpoint test")

            # Wait for the run to complete (fake_pipeline returns immediately)
            for _ in range(100):
                await asyncio.sleep(0.05)
                if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    break

            await mgr.close()

        assert "checkpoint" in received_kwargs
        assert received_kwargs["checkpoint"] is None  # no prior checkpoint in DB
        assert callable(received_kwargs.get("save_checkpoint"))

    @pytest.mark.asyncio
    async def test_execute_run_resumes_from_saved_checkpoint(self, tmp_path, monkeypatch):
        """When a prior checkpoint exists in the DB, it is loaded and forwarded to run_pipeline."""
        monkeypatch.chdir(tmp_path)
        db_path = str(tmp_path / "test.db")

        prior_checkpoint = {"completed_stage": 1, "plan": "prior plan"}
        received_checkpoint: dict = {}

        async def fake_pipeline(task, settings, on_event, **kwargs):
            received_checkpoint["value"] = kwargs.get("checkpoint")
            return "done"

        # Pre-populate DB with a queued run and its checkpoint
        db = OLamoDb(db_path)
        await db.open()
        seed_run = RunRecord(id="resume-test-id", description="resume test")
        await db.upsert_run(seed_run)
        await db.save_checkpoint(seed_run.id, prior_checkpoint)
        await db.close()

        with patch("app.web.run_manager.run_pipeline", fake_pipeline):
            mgr = RunManager(SseBroadcaster(), SettingsStore(), db_path=db_path)
            await mgr.setup()  # loads the queued run and spawns its task

            found_run = mgr.get_run(seed_run.id)
            for _ in range(100):
                await asyncio.sleep(0.05)
                if found_run and found_run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    break

            await mgr.close()

        assert received_checkpoint.get("value") == prior_checkpoint
