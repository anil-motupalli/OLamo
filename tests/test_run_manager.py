"""Tests for app.web.run_manager.RunManager."""

import pytest
import pytest_asyncio

from app.models import RunStatus
from app.settings import SettingsStore
from app.web.broadcaster import SseBroadcaster
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
        await mgr2.close()
        assert found is not None
        assert found.description == "persist me"
        assert found.status == RunStatus.QUEUED

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
