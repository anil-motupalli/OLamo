"""Tests for app.web.database.OLamoDb."""

import pytest

from app.models import RunRecord, RunStatus
from app.web.database import OLamoDb


class TestOLamoDb:
    @pytest.mark.asyncio
    async def test_open_creates_schema(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        async with db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            tables = {row[0] async for row in cur}
        await db.close()
        assert {"runs", "events", "run_state"} <= tables

    @pytest.mark.asyncio
    async def test_upsert_and_get_run(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test run")
        await db.upsert_run(run)
        rows = await db.get_all_runs()
        await db.close()
        assert len(rows) == 1
        assert rows[0].id == "r1"
        assert rows[0].description == "test run"
        assert rows[0].status == RunStatus.QUEUED

    @pytest.mark.asyncio
    async def test_upsert_run_updates_status(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test")
        await db.upsert_run(run)
        run.status = RunStatus.RUNNING
        run.started_at = "2026-01-01T00:00:00+00:00"
        await db.upsert_run(run)
        rows = await db.get_all_runs()
        await db.close()
        assert rows[0].status == RunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_insert_and_get_events(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test")
        await db.upsert_run(run)
        await db.insert_event("r1", {"type": "stage_changed", "stage": "Stage 1"})
        await db.insert_event("r1", {"type": "agent_started", "role": "developer"})
        events = await db.get_events("r1")
        await db.close()
        assert len(events) == 2
        assert events[0]["type"] == "stage_changed"
        assert events[1]["role"] == "developer"

    @pytest.mark.asyncio
    async def test_events_ordered_by_seq(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test")
        await db.upsert_run(run)
        for i in range(5):
            await db.insert_event("r1", {"i": i})
        events = await db.get_events("r1")
        await db.close()
        assert [e["i"] for e in events] == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_upsert_run_state(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test")
        await db.upsert_run(run)
        await db.upsert_run_state("r1", "Stage 2")
        await db.upsert_run_state("r1", "Stage 3")
        async with db._conn.execute("SELECT current_stage FROM run_state WHERE run_id='r1'") as cur:
            row = await cur.fetchone()
        await db.close()
        assert row[0] == "Stage 3"

    @pytest.mark.asyncio
    async def test_settings_override_round_trips(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test", settings_override={"max_impl_cycles": 7})
        await db.upsert_run(run)
        rows = await db.get_all_runs()
        await db.close()
        assert rows[0].settings_override == {"max_impl_cycles": 7}

    @pytest.mark.asyncio
    async def test_pr_url_persisted(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test", pr_url="https://github.com/x/y/pull/1")
        await db.upsert_run(run)
        rows = await db.get_all_runs()
        await db.close()
        assert rows[0].pr_url == "https://github.com/x/y/pull/1"

    @pytest.mark.asyncio
    async def test_save_and_load_checkpoint(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test")
        await db.upsert_run(run)

        data = {"completed_stage": 1, "plan": "some plan", "addressed_ids": [1, 2]}
        await db.save_checkpoint("r1", data)
        loaded = await db.load_checkpoint("r1")
        await db.close()

        assert loaded == data

    @pytest.mark.asyncio
    async def test_load_checkpoint_returns_none_when_not_set(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test")
        await db.upsert_run(run)

        result = await db.load_checkpoint("r1")
        await db.close()

        assert result is None

    @pytest.mark.asyncio
    async def test_save_checkpoint_overwrites_previous(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test")
        await db.upsert_run(run)

        await db.save_checkpoint("r1", {"completed_stage": 1})
        await db.save_checkpoint("r1", {"completed_stage": 3, "pr_result": "https://github.com/x/y/pull/5"})
        loaded = await db.load_checkpoint("r1")
        await db.close()

        assert loaded == {"completed_stage": 3, "pr_result": "https://github.com/x/y/pull/5"}

    @pytest.mark.asyncio
    async def test_get_run_state_includes_checkpoint_data(self, tmp_path):
        db = OLamoDb(str(tmp_path / "test.db"))
        await db.open()
        run = RunRecord(id="r1", description="test")
        await db.upsert_run(run)
        await db.save_checkpoint("r1", {"completed_stage": 2})

        state = await db.get_run_state("r1")
        await db.close()

        assert state is not None
        assert "checkpoint_data" in state
        # checkpoint_data is the raw JSON string stored in the column
        assert state["checkpoint_data"] is not None
