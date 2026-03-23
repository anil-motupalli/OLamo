"""Tests for OLamo pipeline components.

Run with:
    pytest test_main.py -v
    pytest test_main.py -v -k "TestSettingsStore"   # single group
"""

import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from main import (
    AGENT_CONFIGS,
    HAIKU_MODEL,
    MAX_BUILD_CYCLES,
    MAX_DESIGN_CYCLES,
    MAX_IMPL_CYCLES,
    MAX_PR_CYCLES,
    OPUS_MODEL,
    PM_MAIN_MODEL,
    SONNET_MODEL,
    _ALL_REVIEWERS,
    AgentEngineConfig,
    AppSettings,
    ApprovalGate,
    ClaudeEngine,
    CopilotEngine,
    ModelConfig,
    OLamoDb,
    RunRecord,
    RunManager,
    RunStatus,
    SettingsStore,
    SseBroadcaster,
    _extract_comment_ids,
    _make_env,
    _parse_stage_announcement,
    _reviewer_prompt,
    _settings_from_dict,
    build_agents,
    build_pm_prompt,
    get_default_engine_config,
    run_pipeline_orchestrated,
)


# ── Stage announcement parser ─────────────────────────────────────────────────

class TestParseStageAnnouncement:
    def test_parses_stage_1(self):
        assert _parse_stage_announcement("Moving to Stage 1 now") == "Stage 1"

    def test_parses_stage_2(self):
        assert _parse_stage_announcement("Advancing to Stage 2 — implementation") == "Stage 2"

    def test_parses_stage_3(self):
        assert _parse_stage_announcement("Beginning Stage 3") == "Stage 3"

    def test_parses_stage_4(self):
        assert _parse_stage_announcement("Entering Stage 4 — PR poll") == "Stage 4"

    def test_parses_design_cycle(self):
        assert _parse_stage_announcement("Design cycle 1/2 complete") == "Design cycle 1/2"

    def test_parses_design_cycle_second(self):
        assert _parse_stage_announcement("Design cycle 2/2") == "Design cycle 2/2"

    def test_parses_implementation_cycle(self):
        assert _parse_stage_announcement("Implementation cycle 2/3") == "Implementation cycle 2/3"

    def test_parses_pr_cycle(self):
        assert _parse_stage_announcement("PR cycle 1/2") == "PR cycle 1/2"

    def test_parses_ci_check_cycle(self):
        assert _parse_stage_announcement("CI check cycle 1/2") == "CI check cycle 1/2"

    def test_parses_ci_check_cycle_second(self):
        assert _parse_stage_announcement("CI check cycle 2/2") == "CI check cycle 2/2"

    def test_case_insensitive_stage(self):
        result = _parse_stage_announcement("STAGE 2 begins")
        assert result is not None
        assert "2" in result

    def test_returns_none_for_unrelated_text(self):
        assert _parse_stage_announcement("Nothing special here") is None

    def test_returns_none_for_empty_string(self):
        assert _parse_stage_announcement("") is None

    def test_returns_none_for_partial_match(self):
        # "Stage" without a number should not match
        assert _parse_stage_announcement("The staging environment is ready") is None


# ── AppSettings ───────────────────────────────────────────────────────────────

class TestAppSettings:
    def test_defaults_match_module_constants(self):
        s = AppSettings()
        assert s.pm_model == PM_MAIN_MODEL
        assert s.opus_model == OPUS_MODEL
        assert s.sonnet_model == SONNET_MODEL
        assert s.haiku_model == HAIKU_MODEL
        assert s.max_design_cycles == MAX_DESIGN_CYCLES
        assert s.max_build_cycles == MAX_BUILD_CYCLES
        assert s.max_impl_cycles == MAX_IMPL_CYCLES
        assert s.max_pr_cycles == MAX_PR_CYCLES

    def test_can_override_model(self):
        s = AppSettings(pm_model="opus")
        assert s.pm_model == "opus"
        # other fields unaffected
        assert s.opus_model == OPUS_MODEL

    def test_can_override_cycle_limits(self):
        s = AppSettings(max_design_cycles=5, max_pr_cycles=10)
        assert s.max_design_cycles == 5
        assert s.max_pr_cycles == 10
        assert s.max_impl_cycles == MAX_IMPL_CYCLES  # unaffected

    def test_two_defaults_are_equal(self):
        assert AppSettings() == AppSettings()

    def test_asdict_round_trips(self):
        s = AppSettings(pm_model="opus", max_pr_cycles=3)
        d = asdict(s)
        restored = AppSettings(**d)
        assert restored == s

    def test_agent_configs_defaults_empty(self):
        assert AppSettings().agent_configs == {}

    def test_copilot_github_token_defaults_empty(self):
        assert AppSettings().copilot_github_token == ""

    def test_post_init_raises_on_advanced_with_no_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            AppSettings(agent_configs={
                "developer": AgentEngineConfig(
                    model_config=ModelConfig(mode="advanced", model="gpt-4", base_url="")
                )
            })

    def test_post_init_ok_with_advanced_and_base_url(self):
        s = AppSettings(agent_configs={
            "developer": AgentEngineConfig(
                model_config=ModelConfig(mode="advanced", model="gpt-4",
                                         base_url="https://api.example.com")
            )
        })
        assert s.agent_configs["developer"].engine == "claude"

    def test_asdict_includes_agent_configs(self):
        s = AppSettings(agent_configs={"developer": AgentEngineConfig(engine="copilot")})
        d = asdict(s)
        assert d["agent_configs"]["developer"]["engine"] == "copilot"


# ── ModelConfig ───────────────────────────────────────────────────────────────

class TestModelConfig:
    def test_defaults(self):
        m = ModelConfig()
        assert m.mode == "simple"
        assert m.model == ""
        assert m.provider_type == "openai"
        assert m.base_url == ""
        assert m.api_key == ""
        assert m.extra_params == {}

    def test_advanced_mode(self):
        m = ModelConfig(mode="advanced", model="gpt-4", base_url="https://api.example.com", api_key="sk-test")
        assert m.mode == "advanced"
        assert m.model == "gpt-4"
        assert m.base_url == "https://api.example.com"

    def test_extra_params_default_independent(self):
        m1 = ModelConfig()
        m2 = ModelConfig()
        m1.extra_params["key"] = "val"
        assert m2.extra_params == {}


# ── AgentEngineConfig ─────────────────────────────────────────────────────────

class TestAgentEngineConfig:
    def test_defaults(self):
        c = AgentEngineConfig()
        assert c.engine == "claude"
        assert isinstance(c.model_config, ModelConfig)
        assert c.mcp_servers == {}

    def test_copilot_engine(self):
        c = AgentEngineConfig(engine="copilot")
        assert c.engine == "copilot"

    def test_mcp_servers_default_independent(self):
        c1 = AgentEngineConfig()
        c2 = AgentEngineConfig()
        c1.mcp_servers["test"] = {}
        assert c2.mcp_servers == {}


# ── RunRecord ─────────────────────────────────────────────────────────────────

class TestRunRecord:
    def test_defaults_to_queued_status(self):
        r = RunRecord(id="abc", description="test")
        assert r.status == RunStatus.QUEUED

    def test_queued_at_iso_format(self):
        r = RunRecord(id="abc", description="test")
        assert r.queued_at is not None
        assert "T" in r.queued_at  # ISO 8601 contains "T"

    def test_optional_fields_start_as_none(self):
        r = RunRecord(id="abc", description="test")
        assert r.started_at is None
        assert r.completed_at is None
        assert r.error is None
        assert r.log_dir is None

    def test_run_status_values(self):
        assert RunStatus.QUEUED == "queued"
        assert RunStatus.RUNNING == "running"
        assert RunStatus.COMPLETED == "completed"
        assert RunStatus.FAILED == "failed"

    def test_pr_url_defaults_to_empty_string(self):
        r = RunRecord(id="abc", description="test")
        assert r.pr_url == ""

    def test_settings_override_defaults_to_empty_dict(self):
        r = RunRecord(id="abc", description="test")
        assert r.settings_override == {}

    def test_pr_url_can_be_set(self):
        r = RunRecord(id="abc", description="test", pr_url="https://github.com/org/repo/pull/42")
        assert r.pr_url == "https://github.com/org/repo/pull/42"

    def test_settings_override_can_be_set(self):
        r = RunRecord(id="abc", description="test", settings_override={"max_impl_cycles": 5})
        assert r.settings_override == {"max_impl_cycles": 5}


# ── ApprovalGate ──────────────────────────────────────────────────────────────

class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_is_waiting_false_before_wait(self):
        gate = ApprovalGate()
        assert not gate.is_waiting

    @pytest.mark.asyncio
    async def test_wait_stores_current_plan(self):
        gate = ApprovalGate()
        task = asyncio.create_task(gate.wait("my plan"))
        await asyncio.sleep(0)  # let the coroutine start
        assert gate.current_plan == "my plan"
        assert gate.is_waiting
        gate.resolve(approved=True)
        await task

    @pytest.mark.asyncio
    async def test_resolve_approved_returns_true(self):
        gate = ApprovalGate()
        task = asyncio.create_task(gate.wait("plan"))
        await asyncio.sleep(0)
        gate.resolve(approved=True)
        result = await task
        assert result == {"approved": True, "feedback": ""}

    @pytest.mark.asyncio
    async def test_resolve_with_feedback(self):
        gate = ApprovalGate()
        task = asyncio.create_task(gate.wait("plan"))
        await asyncio.sleep(0)
        gate.resolve(approved=False, feedback="needs more detail")
        result = await task
        assert result == {"approved": False, "feedback": "needs more detail"}

    @pytest.mark.asyncio
    async def test_is_waiting_false_after_resolve(self):
        gate = ApprovalGate()
        task = asyncio.create_task(gate.wait("plan"))
        await asyncio.sleep(0)
        gate.resolve(approved=True)
        await task
        assert not gate.is_waiting

    def test_resolve_without_wait_does_not_raise(self):
        gate = ApprovalGate()
        gate.resolve(approved=True)  # should not raise


# ── _reviewer_prompt ──────────────────────────────────────────────────────────

class TestReviewerPrompt:
    def test_code_reviewer_prompt(self):
        prompt = _reviewer_prompt("code-reviewer", "the plan", "")
        assert "Review the implementation" in prompt

    def test_qa_engineer_prompt_includes_plan(self):
        prompt = _reviewer_prompt("qa-engineer", "my plan", "")
        assert "REVIEW CODE" in prompt
        assert "my plan" in prompt

    def test_lead_developer_prompt_includes_plan(self):
        prompt = _reviewer_prompt("lead-developer", "my plan", "")
        assert "REVIEW IMPLEMENTATION" in prompt
        assert "my plan" in prompt

    def test_diff_ctx_appended(self):
        prompt = _reviewer_prompt("code-reviewer", "p", "\ndiff --git a/f b/f")
        assert "diff --git" in prompt

    def test_all_reviewers_constant_has_three_entries(self):
        assert len(_ALL_REVIEWERS) == 3

    def test_all_reviewers_contains_expected_roles(self):
        assert "code-reviewer" in _ALL_REVIEWERS
        assert "qa-engineer" in _ALL_REVIEWERS
        assert "lead-developer" in _ALL_REVIEWERS


# ── build_pm_prompt ───────────────────────────────────────────────────────────

class TestBuildPmPrompt:
    def test_contains_all_four_stage_headers(self):
        prompt = build_pm_prompt(AppSettings())
        for stage in ("STAGE 1", "STAGE 2", "STAGE 3", "STAGE 4"):
            assert stage in prompt, f"Missing {stage}"

    def test_embeds_design_cycle_limit(self):
        s = AppSettings(max_design_cycles=7)
        prompt = build_pm_prompt(s)
        assert "7" in prompt

    def test_embeds_impl_cycle_limit(self):
        s = AppSettings(max_impl_cycles=9)
        prompt = build_pm_prompt(s)
        assert "9" in prompt

    def test_embeds_pr_cycle_limit(self):
        s = AppSettings(max_pr_cycles=4)
        prompt = build_pm_prompt(s)
        assert "4" in prompt

    def test_has_explicit_build_failure_gate(self):
        prompt = build_pm_prompt(AppSettings())
        assert "Only proceed to 2c if build-agent reports SUCCESS" in prompt

    def test_has_addressed_id_tracking(self):
        prompt = build_pm_prompt(AppSettings())
        assert "Exclude these IDs" in prompt

    def test_has_diff_handoff_for_reviewers(self):
        prompt = build_pm_prompt(AppSettings())
        assert "git diff" in prompt

    def test_has_mark_comments_addressed(self):
        prompt = build_pm_prompt(AppSettings())
        assert "MARK COMMENTS ADDRESSED" in prompt

    def test_has_ci_check_polling(self):
        prompt = build_pm_prompt(AppSettings())
        assert "POLL CI CHECKS" in prompt

    def test_has_checks_passing_gate(self):
        prompt = build_pm_prompt(AppSettings())
        assert "CHECKS PASSING" in prompt

    def test_has_ci_check_cycle_announcement(self):
        prompt = build_pm_prompt(AppSettings())
        assert "CI check cycle" in prompt

    def test_different_settings_produce_different_prompts(self):
        p1 = build_pm_prompt(AppSettings(max_design_cycles=2))
        p2 = build_pm_prompt(AppSettings(max_design_cycles=5))
        assert p1 != p2


# ── build_agents ──────────────────────────────────────────────────────────────

class TestBuildAgents:
    def test_returns_all_six_agents(self):
        agents = build_agents(AppSettings())
        expected = {
            "lead-developer", "developer", "code-reviewer",
            "qa-engineer", "build-agent", "repo-manager",
        }
        assert set(agents.keys()) == expected

    def test_lead_developer_uses_opus_model(self):
        s = AppSettings(opus_model="my-opus")
        assert build_agents(s)["lead-developer"].model == "my-opus"

    def test_code_reviewer_uses_opus_model(self):
        s = AppSettings(opus_model="my-opus")
        assert build_agents(s)["code-reviewer"].model == "my-opus"

    def test_qa_engineer_uses_opus_model(self):
        s = AppSettings(opus_model="my-opus")
        assert build_agents(s)["qa-engineer"].model == "my-opus"

    def test_developer_uses_sonnet_model(self):
        s = AppSettings(sonnet_model="my-sonnet")
        assert build_agents(s)["developer"].model == "my-sonnet"

    def test_build_agent_uses_haiku_model(self):
        s = AppSettings(haiku_model="my-haiku")
        assert build_agents(s)["build-agent"].model == "my-haiku"

    def test_repo_manager_uses_haiku_model(self):
        s = AppSettings(haiku_model="my-haiku")
        assert build_agents(s)["repo-manager"].model == "my-haiku"

    def test_all_agents_have_descriptions(self):
        for role, defn in build_agents(AppSettings()).items():
            assert defn.description, f"{role} has no description"

    def test_all_agents_have_tools(self):
        for role, defn in build_agents(AppSettings()).items():
            assert defn.tools, f"{role} has no tools"

    def test_repo_manager_description_mentions_all_five_modes(self):
        desc = build_agents(AppSettings())["repo-manager"].description
        for mode_keyword in ("commit", "POLL PR COMMENTS", "PUSH CHANGES", "MARK COMMENTS ADDRESSED", "POLL CI CHECKS"):
            assert mode_keyword.lower() in desc.lower(), f"Missing '{mode_keyword}' in repo-manager description"


# ── SettingsStore ─────────────────────────────────────────────────────────────

class TestSettingsStore:
    @pytest.mark.asyncio
    async def test_starts_unlocked_with_defaults(self):
        store = SettingsStore()
        assert not store.is_locked
        assert store.settings == AppSettings()

    @pytest.mark.asyncio
    async def test_lock_sets_locked(self):
        store = SettingsStore()
        await store.lock()
        assert store.is_locked

    @pytest.mark.asyncio
    async def test_unlock_clears_locked(self):
        store = SettingsStore()
        await store.lock()
        await store.unlock()
        assert not store.is_locked

    @pytest.mark.asyncio
    async def test_try_update_applies_when_not_locked(self):
        store = SettingsStore()
        new = AppSettings(pm_model="opus")
        applied = await store.try_update(new)
        assert applied is True
        assert store.settings.pm_model == "opus"

    @pytest.mark.asyncio
    async def test_try_update_queues_and_returns_false_when_locked(self):
        store = SettingsStore()
        await store.lock()
        new = AppSettings(pm_model="opus")
        applied = await store.try_update(new)
        assert applied is False
        assert store.settings.pm_model != "opus"  # not applied yet

    @pytest.mark.asyncio
    async def test_unlock_applies_pending_settings(self):
        store = SettingsStore()
        await store.lock()
        await store.try_update(AppSettings(pm_model="opus"))
        await store.unlock()
        assert store.settings.pm_model == "opus"
        assert not store.is_locked

    @pytest.mark.asyncio
    async def test_unlock_without_pending_keeps_original(self):
        store = SettingsStore()
        original = store.settings.pm_model
        await store.lock()
        await store.unlock()
        assert store.settings.pm_model == original

    @pytest.mark.asyncio
    async def test_second_update_while_locked_replaces_pending(self):
        store = SettingsStore()
        await store.lock()
        await store.try_update(AppSettings(pm_model="first"))
        await store.try_update(AppSettings(pm_model="second"))
        await store.unlock()
        assert store.settings.pm_model == "second"


# ── SseBroadcaster ────────────────────────────────────────────────────────────

class TestSseBroadcaster:
    @pytest.mark.asyncio
    async def test_connect_returns_uuid_and_queue(self):
        b = SseBroadcaster()
        cid, q = await b.connect()
        assert len(cid) == 36  # UUID4 format "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        assert isinstance(q, asyncio.Queue)

    @pytest.mark.asyncio
    async def test_broadcast_delivers_json_to_client(self):
        b = SseBroadcaster()
        _, q = await b.connect()
        await b.broadcast({"type": "ping", "value": 42})
        data = q.get_nowait()
        event = json.loads(data)
        assert event["type"] == "ping"
        assert event["value"] == 42

    @pytest.mark.asyncio
    async def test_broadcast_delivers_to_all_connected_clients(self):
        b = SseBroadcaster()
        _, q1 = await b.connect()
        _, q2 = await b.connect()
        _, q3 = await b.connect()
        await b.broadcast({"type": "multi"})
        assert not q1.empty()
        assert not q2.empty()
        assert not q3.empty()

    @pytest.mark.asyncio
    async def test_disconnect_sends_none_sentinel(self):
        b = SseBroadcaster()
        cid, q = await b.connect()
        await b.disconnect(cid)
        sentinel = await q.get()
        assert sentinel is None

    @pytest.mark.asyncio
    async def test_disconnected_client_receives_no_further_broadcasts(self):
        b = SseBroadcaster()
        cid, q = await b.connect()
        await b.disconnect(cid)
        await q.get()  # consume sentinel
        await b.broadcast({"type": "after-disconnect"})
        assert q.empty()

    @pytest.mark.asyncio
    async def test_broadcast_with_no_clients_does_not_raise(self):
        b = SseBroadcaster()
        await b.broadcast({"type": "no-clients"})  # should not raise

    @pytest.mark.asyncio
    async def test_broadcast_serialises_nested_dict(self):
        b = SseBroadcaster()
        _, q = await b.connect()
        payload = {"type": "nested", "data": {"a": [1, 2, 3]}}
        await b.broadcast(payload)
        received = json.loads(q.get_nowait())
        assert received["data"]["a"] == [1, 2, 3]


# ── RunManager ────────────────────────────────────────────────────────────────

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


# ── OLamoDb ───────────────────────────────────────────────────────────────────

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
        await db.upsert_run_state("r1", "Stage 3")  # update
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


# ── AGENT_CONFIGS ─────────────────────────────────────────────────────────────

class TestAgentConfigs:
    def test_all_six_roles_present(self):
        expected = {"lead-developer", "developer", "code-reviewer", "qa-engineer", "build-agent", "repo-manager"}
        assert set(AGENT_CONFIGS.keys()) == expected

    def test_each_entry_has_three_elements(self):
        for role, cfg in AGENT_CONFIGS.items():
            assert len(cfg) == 3, f"{role} config should be (prompt, tools, model_key)"

    def test_model_keys_exist_on_app_settings(self):
        fields = AppSettings.__dataclass_fields__
        for role, (_, _, model_key) in AGENT_CONFIGS.items():
            assert model_key in fields, f"{role} references unknown model key '{model_key}'"

    def test_all_agents_have_tools(self):
        for role, (_, tools, _) in AGENT_CONFIGS.items():
            assert tools, f"{role} has empty tools list"

    def test_all_agents_have_prompts(self):
        for role, (prompt, _, _) in AGENT_CONFIGS.items():
            assert prompt.strip(), f"{role} has empty system prompt"

    def test_developer_has_write_tool(self):
        _, tools, _ = AGENT_CONFIGS["developer"]
        assert "Write" in tools

    def test_code_reviewer_has_no_bash(self):
        # Reviewer should read only, not execute
        _, tools, _ = AGENT_CONFIGS["code-reviewer"]
        assert "Bash" not in tools

    def test_repo_manager_uses_haiku(self):
        _, _, model_key = AGENT_CONFIGS["repo-manager"]
        assert model_key == "haiku_model"

    def test_lead_developer_uses_opus(self):
        _, _, model_key = AGENT_CONFIGS["lead-developer"]
        assert model_key == "opus_model"


# ── _extract_comment_ids ──────────────────────────────────────────────────────

class TestExtractCommentIds:
    def test_extracts_single_id(self):
        text = "ID: 42\nauthor: alice\nbody: fix this"
        assert _extract_comment_ids(text) == ["42"]

    def test_extracts_multiple_ids(self):
        text = "ID: 101\n...\nID: 202\n...\nID: 303"
        assert _extract_comment_ids(text) == ["101", "202", "303"]

    def test_case_insensitive(self):
        assert _extract_comment_ids("id: abc123") == ["abc123"]

    def test_returns_empty_for_no_ids(self):
        assert _extract_comment_ids("NO ACTIONABLE COMMENTS") == []

    def test_returns_empty_for_empty_string(self):
        assert _extract_comment_ids("") == []

    def test_handles_alphanumeric_ids(self):
        assert _extract_comment_ids("ID: PR-456") == ["PR-456"]


# ── _make_env ─────────────────────────────────────────────────────────────────

class TestMakeEnv:
    def test_always_unsets_claudecode(self):
        env = _make_env(AppSettings())
        assert env.get("CLAUDECODE") == ""

    def test_no_base_url_when_empty(self):
        env = _make_env(AppSettings(api_base_url=""))
        assert "ANTHROPIC_BASE_URL" not in env

    def test_sets_base_url_when_provided(self):
        env = _make_env(AppSettings(api_base_url="https://proxy.example.com"))
        assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example.com"


# ── AppSettings orchestration_mode ────────────────────────────────────────────

class TestOrchestrationMode:
    def test_default_is_pm(self):
        assert AppSettings().orchestration_mode == "pm"

    def test_can_set_orchestrated(self):
        s = AppSettings(orchestration_mode="orchestrated")
        assert s.orchestration_mode == "orchestrated"

    def test_settings_endpoint_includes_mode(self):
        d = asdict(AppSettings())
        assert "orchestration_mode" in d


# ── FastAPI endpoints ─────────────────────────────────────────────────────────

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

from starlette.testclient import TestClient  # noqa: E402  (after importorskip guard)
from main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<html>OLamo</html>")
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestApiSettings:
    def test_get_settings_returns_defaults(self, client):
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data
        assert "is_locked" in data
        assert data["is_locked"] is False
        assert data["config"]["pm_model"] == PM_MAIN_MODEL

    def test_put_settings_updates_config(self, client):
        resp = client.put("/api/settings", json={"pm_model": "opus", "max_pr_cycles": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is True
        assert data["config"]["pm_model"] == "opus"
        assert data["config"]["max_pr_cycles"] == 5

    def test_put_settings_ignores_unknown_keys(self, client):
        resp = client.put("/api/settings", json={"unknown_field": "value", "pm_model": "haiku"})
        assert resp.status_code == 200
        assert resp.json()["config"]["pm_model"] == "haiku"

    def test_get_settings_includes_agent_configs(self, client):
        assert "agent_configs" in client.get("/api/settings").json()["config"]

    def test_get_settings_includes_copilot_github_token(self, client):
        assert "copilot_github_token" in client.get("/api/settings").json()["config"]

    def test_put_settings_accepts_agent_configs(self, client):
        payload = {"agent_configs": {"developer": {
            "engine": "copilot",
            "model_config": {"mode": "simple", "model": "gpt-5",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {}},
            "mcp_servers": {}
        }}}
        resp = client.put("/api/settings", json=payload)
        assert resp.status_code == 200
        assert resp.json()["config"]["agent_configs"]["developer"]["engine"] == "copilot"

    def test_put_settings_advanced_missing_base_url_returns_422(self, client):
        payload = {"agent_configs": {"developer": {
            "engine": "claude",
            "model_config": {"mode": "advanced", "model": "gpt-4",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {}},
            "mcp_servers": {}
        }}}
        resp = client.put("/api/settings", json=payload)
        assert resp.status_code == 422


class TestApiRuns:
    def test_list_runs_initially_empty(self, client):
        resp = client.get("/api/runs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_run_returns_201(self, client):
        resp = client.post("/api/runs", json={"description": "build a feature"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["description"] == "build a feature"
        assert data["status"] == "queued"
        assert "id" in data

    def test_create_run_appears_in_list(self, client):
        client.post("/api/runs", json={"description": "listed task"})
        runs = client.get("/api/runs").json()
        assert len(runs) == 1
        assert runs[0]["description"] == "listed task"

    def test_create_run_missing_description_returns_400(self, client):
        resp = client.post("/api/runs", json={})
        assert resp.status_code == 400

    def test_get_run_by_id(self, client):
        created = client.post("/api/runs", json={"description": "get by id"}).json()
        resp = client.get(f"/api/runs/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]

    def test_get_run_unknown_id_returns_404(self, client):
        resp = client.get("/api/runs/nonexistent-id")
        assert resp.status_code == 404

    def test_run_events_empty_for_new_run(self, client):
        created = client.post("/api/runs", json={"description": "no events yet"}).json()
        resp = client.get(f"/api/runs/{created['id']}/events")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_run_events_unknown_run_returns_404(self, client):
        resp = client.get("/api/runs/bad-id/events")
        assert resp.status_code == 404

    def test_create_run_with_pr_url(self, client):
        resp = client.post("/api/runs", json={"description": "fix PR", "pr_url": "https://github.com/org/repo/pull/5"})
        assert resp.status_code == 201
        assert resp.json()["pr_url"] == "https://github.com/org/repo/pull/5"

    def test_create_run_with_settings_override(self, client):
        resp = client.post("/api/runs", json={"description": "fast run", "settings_override": {"max_impl_cycles": 1}})
        assert resp.status_code == 201
        assert resp.json()["settings_override"] == {"max_impl_cycles": 1}

    def test_create_run_without_pr_url_defaults_to_empty(self, client):
        resp = client.post("/api/runs", json={"description": "normal run"})
        assert resp.status_code == 201
        assert resp.json()["pr_url"] == ""


class TestApiApproval:
    def test_get_approval_returns_404_for_unknown_run(self, client):
        resp = client.get("/api/runs/no-such-id/approval")
        assert resp.status_code == 404

    def test_get_approval_not_waiting_for_queued_run(self, client):
        created = client.post("/api/runs", json={"description": "task"}).json()
        resp = client.get(f"/api/runs/{created['id']}/approval")
        assert resp.status_code == 200
        assert resp.json()["waiting"] is False
        assert resp.json()["plan"] == ""

    def test_post_approval_returns_409_when_not_waiting(self, client):
        created = client.post("/api/runs", json={"description": "task"}).json()
        resp = client.post(f"/api/runs/{created['id']}/approval", json={"approved": True})
        assert resp.status_code == 409

    def test_post_approval_returns_404_for_unknown_run(self, client):
        resp = client.post("/api/runs/no-such-id/approval", json={"approved": True})
        assert resp.status_code == 404


class TestApiTeam:
    def test_team_returns_all_six_agents(self, client):
        resp = client.get("/api/team")
        assert resp.status_code == 200
        data = resp.json()
        roles = {a["role"] for a in data["agents"]}
        expected = {"lead-developer", "developer", "code-reviewer", "qa-engineer", "build-agent", "repo-manager"}
        assert roles == expected

    def test_team_returns_pipeline_stages(self, client):
        data = client.get("/api/team").json()
        assert len(data["pipeline"]) == 4

    def test_team_returns_cycle_limits(self, client):
        data = client.get("/api/team").json()
        limits = data["cycle_limits"]
        assert limits["max_design_cycles"] == MAX_DESIGN_CYCLES
        assert limits["max_build_cycles"] == MAX_BUILD_CYCLES
        assert limits["max_impl_cycles"] == MAX_IMPL_CYCLES
        assert limits["max_pr_cycles"] == MAX_PR_CYCLES

    def test_each_agent_has_model_and_description(self, client):
        agents = client.get("/api/team").json()["agents"]
        for agent in agents:
            assert agent["model"], f"{agent['role']} missing model"
            assert agent["description"], f"{agent['role']} missing description"

    def test_each_agent_has_engine_field(self, client):
        for agent in client.get("/api/team").json()["agents"]:
            assert agent["engine"] in ("claude", "copilot"), f"{agent['role']} bad engine"

    def test_each_agent_has_config_mode_field(self, client):
        for agent in client.get("/api/team").json()["agents"]:
            assert agent["config_mode"] in ("simple", "advanced"), f"{agent['role']} bad config_mode"

    def test_default_engines_match_smart_defaults(self, client):
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        assert agents["lead-developer"]["engine"] == "claude"
        assert agents["developer"]["engine"] == "claude"
        assert agents["code-reviewer"]["engine"] == "copilot"
        assert agents["qa-engineer"]["engine"] == "copilot"
        assert agents["build-agent"]["engine"] == "copilot"
        assert agents["repo-manager"]["engine"] == "copilot"

    def test_copilot_default_agents_report_copilot_model(self, client):
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        # Smart default models for copilot-engine agents
        assert agents["code-reviewer"]["model"] == "codex"
        assert agents["qa-engineer"]["model"] == "gpt-5.4"
        assert agents["build-agent"]["model"] == "gpt-5-mini"
        assert agents["repo-manager"]["model"] == "gpt-5-mini"

    def test_claude_default_agents_report_claude_tier_model(self, client):
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        assert agents["lead-developer"]["model"] == OPUS_MODEL
        assert agents["developer"]["model"] == SONNET_MODEL

    def test_agent_config_override_reflected_in_team(self, client):
        # PUT an override for developer → copilot + explicit model
        client.put("/api/settings", json={"agent_configs": {"developer": {
            "engine": "copilot",
            "model_config": {"mode": "simple", "model": "gpt-5",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {}},
            "mcp_servers": {}
        }}})
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        assert agents["developer"]["engine"] == "copilot"
        assert agents["developer"]["model"] == "gpt-5"
        assert agents["developer"]["config_mode"] == "simple"

    def test_put_settings_unknown_model_config_key_returns_422(self, client):
        payload = {"agent_configs": {"developer": {
            "engine": "claude",
            "model_config": {"mode": "simple", "model": "claude-sonnet-4-6",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {},
                             "unknown_key": "surprise"},
            "mcp_servers": {}
        }}}
        resp = client.put("/api/settings", json=payload)
        assert resp.status_code == 422


class TestSpaFallback:
    def test_root_serves_index_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_unknown_path_serves_index_html(self, client):
        resp = client.get("/some/spa/route")
        assert resp.status_code == 200


# ── get_default_engine_config ──────────────────────────────────────────────

class TestGetDefaultEngineConfig:
    def test_lead_developer_defaults_to_claude(self):
        assert get_default_engine_config("lead-developer", AppSettings()).engine == "claude"

    def test_developer_defaults_to_claude(self):
        assert get_default_engine_config("developer", AppSettings()).engine == "claude"

    def test_code_reviewer_defaults_to_copilot(self):
        assert get_default_engine_config("code-reviewer", AppSettings()).engine == "copilot"

    def test_qa_engineer_defaults_to_copilot(self):
        assert get_default_engine_config("qa-engineer", AppSettings()).engine == "copilot"

    def test_build_agent_defaults_to_copilot(self):
        assert get_default_engine_config("build-agent", AppSettings()).engine == "copilot"

    def test_repo_manager_defaults_to_copilot(self):
        assert get_default_engine_config("repo-manager", AppSettings()).engine == "copilot"

    def test_lead_developer_claude_model_resolves_from_settings(self):
        cfg = get_default_engine_config("lead-developer", AppSettings(opus_model="my-opus"))
        assert cfg.model_config.model == "my-opus"

    def test_developer_claude_model_resolves_from_settings(self):
        cfg = get_default_engine_config("developer", AppSettings(sonnet_model="my-sonnet"))
        assert cfg.model_config.model == "my-sonnet"

    def test_code_reviewer_copilot_model_is_codex(self):
        assert get_default_engine_config("code-reviewer", AppSettings()).model_config.model == "codex"

    def test_qa_engineer_copilot_model(self):
        assert get_default_engine_config("qa-engineer", AppSettings()).model_config.model == "gpt-5.4"

    def test_build_agent_copilot_model(self):
        assert get_default_engine_config("build-agent", AppSettings()).model_config.model == "gpt-5-mini"

    def test_repo_manager_copilot_model(self):
        assert get_default_engine_config("repo-manager", AppSettings()).model_config.model == "gpt-5-mini"

    def test_all_six_roles_covered(self):
        roles = ["lead-developer", "developer", "code-reviewer", "qa-engineer", "build-agent", "repo-manager"]
        for role in roles:
            cfg = get_default_engine_config(role, AppSettings())
            assert cfg.engine in ("claude", "copilot")
            assert cfg.model_config.model != ""


# ── _settings_from_dict ────────────────────────────────────────────────────

class TestSettingsFromDict:
    def test_plain_settings_round_trips(self):
        s = AppSettings(pm_model="opus")
        restored = _settings_from_dict(asdict(s))
        assert restored.pm_model == "opus"
        assert isinstance(restored, AppSettings)

    def test_agent_configs_deserialized_as_dataclasses(self):
        d = asdict(AppSettings(agent_configs={
            "developer": AgentEngineConfig(
                engine="copilot",
                model_config=ModelConfig(mode="simple", model="gpt-5")
            )
        }))
        s = _settings_from_dict(d)
        assert isinstance(s.agent_configs["developer"], AgentEngineConfig)
        assert isinstance(s.agent_configs["developer"].model_config, ModelConfig)
        assert s.agent_configs["developer"].engine == "copilot"
        assert s.agent_configs["developer"].model_config.model == "gpt-5"

    def test_missing_agent_configs_defaults_to_empty(self):
        d = asdict(AppSettings())
        d.pop("agent_configs")
        s = _settings_from_dict(d)
        assert s.agent_configs == {}

    def test_does_not_mutate_input(self):
        d = asdict(AppSettings())
        original_keys = set(d.keys())
        _settings_from_dict(d)
        assert set(d.keys()) == original_keys


# ── ClaudeEngine ──────────────────────────────────────────────────────────

class TestClaudeEngine:
    @pytest.mark.asyncio
    async def test_start_is_noop(self):
        engine = ClaudeEngine(AppSettings())
        await engine.start()  # must not raise

    @pytest.mark.asyncio
    async def test_stop_is_noop(self):
        engine = ClaudeEngine(AppSettings())
        await engine.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_run_returns_result_message_text(self):
        from claude_agent_sdk import ResultMessage
        mock_result = MagicMock(spec=ResultMessage)
        mock_result.result = "implementation done"

        async def fake_query(**kwargs):
            yield mock_result

        with patch("main.query", fake_query):
            engine = ClaudeEngine(AppSettings())
            result = await engine.run(
                role="developer", prompt="implement X",
                system_prompt="You are a developer", tools=["Read", "Write"],
                model="sonnet", model_config=ModelConfig(),
                mcp_servers={}, on_event=AsyncMock(),
            )
        assert result == "implementation done"

    @pytest.mark.asyncio
    async def test_run_emits_agent_message_for_text_blocks(self):
        from claude_agent_sdk import AssistantMessage, TextBlock, ResultMessage
        mock_msg = MagicMock(spec=AssistantMessage)
        mock_block = MagicMock(spec=TextBlock)
        mock_block.text = "Working on it..."
        mock_msg.content = [mock_block]
        mock_result = MagicMock(spec=ResultMessage)
        mock_result.result = "done"

        events = []
        async def on_event(evt): events.append(evt)

        async def fake_query(**kwargs):
            yield mock_msg
            yield mock_result

        with patch("main.query", fake_query):
            engine = ClaudeEngine(AppSettings())
            await engine.run(
                role="developer", prompt="x", system_prompt="sys",
                tools=[], model="sonnet", model_config=ModelConfig(),
                mcp_servers={}, on_event=on_event,
            )
        agent_msgs = [e for e in events if e["type"] == "agent_message"]
        assert len(agent_msgs) == 1
        assert agent_msgs[0]["role"] == "developer"
        assert "Working on it" in agent_msgs[0]["text"]

    @pytest.mark.asyncio
    async def test_run_per_agent_base_url_overrides_global(self):
        captured = {}

        async def fake_query(**kwargs):
            captured.update(kwargs.get("options").env or {})
            from claude_agent_sdk import ResultMessage
            mock = MagicMock(spec=ResultMessage)
            mock.result = ""
            yield mock

        with patch("main.query", fake_query):
            engine = ClaudeEngine(AppSettings(api_base_url="https://global.example.com"))
            await engine.run(
                role="developer", prompt="x", system_prompt="sys", tools=[],
                model="gpt-4",
                model_config=ModelConfig(mode="advanced", model="gpt-4",
                                          base_url="https://per-agent.example.com",
                                          api_key="sk-test"),
                mcp_servers={}, on_event=AsyncMock(),
            )
        assert captured.get("ANTHROPIC_BASE_URL") == "https://per-agent.example.com"
        assert captured.get("ANTHROPIC_API_KEY") == "sk-test"

    @pytest.mark.asyncio
    async def test_run_passes_mcp_servers_to_options(self):
        captured_options = {}

        async def fake_query(**kwargs):
            captured_options.update({"mcp_servers": kwargs.get("options").mcp_servers})
            from claude_agent_sdk import ResultMessage
            mock = MagicMock(spec=ResultMessage)
            mock.result = ""
            yield mock

        mcp = {"my-server": {"type": "local", "command": "node", "args": ["srv.js"]}}
        with patch("main.query", fake_query):
            engine = ClaudeEngine(AppSettings())
            await engine.run(
                role="developer", prompt="x", system_prompt="sys", tools=[],
                model="sonnet", model_config=ModelConfig(),
                mcp_servers=mcp, on_event=AsyncMock(),
            )
        assert captured_options["mcp_servers"] == mcp


class TestCopilotEngine:
    def _make_mock_session(self, result_content="copilot result"):
        """Build a mock session that fires assistant.message then session.idle."""
        session = MagicMock()
        handlers = []
        session.on = lambda h: handlers.append(h)
        session.disconnect = AsyncMock()

        async def fake_send(prompt):
            msg_evt = MagicMock()
            msg_evt.type.value = "assistant.message"
            msg_evt.data.content = result_content
            for h in handlers:
                h(msg_evt)
            idle_evt = MagicMock()
            idle_evt.type.value = "session.idle"
            for h in handlers:
                h(idle_evt)

        session.send = fake_send
        return session

    @pytest.mark.asyncio
    async def test_start_raises_if_sdk_missing(self):
        with patch("main.CopilotClient", None):
            engine = CopilotEngine(AppSettings())
            with pytest.raises(SystemExit):
                await engine.start()

    @pytest.mark.asyncio
    async def test_run_returns_assistant_message_content(self):
        session = self._make_mock_session("feature implemented")
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        with patch("main.CopilotClient", return_value=mock_client), \
             patch("main.SubprocessConfig"):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            result = await engine.run(
                role="qa-engineer", prompt="test it", system_prompt="You are QA",
                tools=[], model="gpt-5.4", model_config=ModelConfig(),
                mcp_servers={}, on_event=AsyncMock(),
            )
            await engine.stop()

        assert result == "feature implemented"

    @pytest.mark.asyncio
    async def test_run_emits_agent_message_event(self):
        session = self._make_mock_session("result text")
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        events = []
        async def on_event(e): events.append(e)

        with patch("main.CopilotClient", return_value=mock_client), \
             patch("main.SubprocessConfig"):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            await engine.run(
                role="qa-engineer", prompt="test", system_prompt="sys",
                tools=[], model="gpt-5.4", model_config=ModelConfig(),
                mcp_servers={}, on_event=on_event,
            )

        agent_msgs = [e for e in events if e["type"] == "agent_message"]
        assert len(agent_msgs) == 1
        assert agent_msgs[0]["role"] == "qa-engineer"

    @pytest.mark.asyncio
    async def test_run_passes_mcp_servers_to_create_session(self):
        session = self._make_mock_session()
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        mcp = {"my-server": {"type": "local", "command": "python", "args": ["./s.py"], "tools": ["*"]}}

        with patch("main.CopilotClient", return_value=mock_client), \
             patch("main.SubprocessConfig"):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            await engine.run(
                role="build-agent", prompt="build", system_prompt="sys",
                tools=[], model="gpt-5-mini", model_config=ModelConfig(),
                mcp_servers=mcp, on_event=AsyncMock(),
            )

        call_cfg = mock_client.create_session.call_args[0][0]
        assert call_cfg["mcp_servers"] == mcp

    @pytest.mark.asyncio
    async def test_run_passes_provider_config_in_advanced_mode(self):
        session = self._make_mock_session()
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        mc = ModelConfig(mode="advanced", model="my-model", provider_type="openai",
                         base_url="https://custom.api.com", api_key="sk-custom")

        with patch("main.CopilotClient", return_value=mock_client), \
             patch("main.SubprocessConfig"):
            engine = CopilotEngine(AppSettings(copilot_github_token="gh-tok"))
            await engine.start()
            await engine.run(
                role="code-reviewer", prompt="review", system_prompt="sys",
                tools=[], model="my-model", model_config=mc,
                mcp_servers={}, on_event=AsyncMock(),
            )

        call_cfg = mock_client.create_session.call_args[0][0]
        assert call_cfg["provider"]["type"] == "openai"
        assert call_cfg["provider"]["base_url"] == "https://custom.api.com"
        assert call_cfg["provider"]["api_key"] == "sk-custom"


# ── Orchestration engine routing ──────────────────────────────────────────────

class TestOrchestrationEngineRouting:
    @pytest.mark.asyncio
    async def test_claude_engine_agents_invoke_query(self):
        """Agents configured for claude engine go through ClaudeEngine (query())."""
        from claude_agent_sdk import ResultMessage
        query_calls = []

        async def fake_query(**kwargs):
            query_calls.append(kwargs)
            mock = MagicMock(spec=ResultMessage)
            mock.result = "APPROVED"
            yield mock

        settings = AppSettings(
            orchestration_mode="orchestrated",
            max_design_cycles=1,
            max_impl_cycles=1,
            max_build_cycles=1,
            max_pr_cycles=1,
            agent_configs={role: AgentEngineConfig(engine="claude")
                           for role in ["lead-developer", "developer", "code-reviewer",
                                        "qa-engineer", "build-agent", "repo-manager"]},
        )

        with patch("main.query", fake_query):
            events = []
            on_event = AsyncMock(side_effect=lambda e: events.append(e))
            await run_pipeline_orchestrated(
                task="add hello world",
                settings=settings,
                on_event=on_event,
            )

        # With max_design_cycles=1, max_impl_cycles=1, max_build_cycles=1 the pipeline
        # runs at minimum: lead-dev plan, qa review, developer impl, build-agent, code-reviewer,
        # qa-engineer, lead-dev review, repo-manager = 8+ calls
        assert len(query_calls) >= 8

    @pytest.mark.asyncio
    async def test_copilot_engine_agents_invoke_copilot_client(self):
        """Agents configured for copilot engine go through CopilotEngine."""
        session = MagicMock()
        handlers = []
        session.on = lambda h: handlers.append(h)
        session.disconnect = AsyncMock()

        async def fake_send(prompt):
            msg_evt = MagicMock()
            msg_evt.type.value = "assistant.message"
            msg_evt.data.content = "APPROVED"
            for h in handlers: h(msg_evt)
            idle_evt = MagicMock()
            idle_evt.type.value = "session.idle"
            for h in handlers: h(idle_evt)

        session.send = fake_send

        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=session)

        settings = AppSettings(
            orchestration_mode="orchestrated",
            copilot_github_token="gh-tok",
            max_design_cycles=1,
            max_impl_cycles=1,
            max_build_cycles=1,
            max_pr_cycles=1,
            agent_configs={role: AgentEngineConfig(engine="copilot",
                                                    model_config=ModelConfig(model="gpt-5"))
                           for role in ["lead-developer", "developer", "code-reviewer",
                                        "qa-engineer", "build-agent", "repo-manager"]},
        )

        with patch("main.CopilotClient", return_value=mock_client), \
             patch("main.SubprocessConfig"):
            events = []
            on_event = AsyncMock(side_effect=lambda e: events.append(e))
            await run_pipeline_orchestrated(
                task="add hello world",
                settings=settings,
                on_event=on_event,
            )

        # start() called once, stop() called once, create_session() called ≥8 times
        assert mock_client.start.call_count == 1
        assert mock_client.stop.call_count == 1
        assert mock_client.create_session.call_count >= 8


class TestApiPrs:
    def test_get_prs_gh_not_installed(self, client, monkeypatch):
        """Returns error field and empty prs list when gh binary not found."""
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError()
        monkeypatch.setattr("main.subprocess.run", fake_run)
        resp = client.get("/api/prs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["prs"] == []
        assert "error" in data
        assert data["error"]

    def test_get_prs_not_in_git_repo(self, client, monkeypatch):
        """Returns error field when gh exits non-zero (e.g. not in a git repo)."""
        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "not a git repository"
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["prs"] == []
        assert "error" in data
        assert data["error"]

    def test_get_prs_auth_authenticated(self, client, monkeypatch):
        """Returns authenticated:true with username when gh auth status exits 0."""
        class FakeResult:
            returncode = 0
            stdout = "anil"
            stderr = ""
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs/auth")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["user"] == "anil"

    def test_get_prs_auth_not_authenticated(self, client, monkeypatch):
        """Returns authenticated:false when gh auth status exits non-zero."""
        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "not logged in"
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs/auth")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["user"] is None

    def test_post_prs_auth_login_gh_not_installed(self, client, monkeypatch):
        """Returns error when gh binary is not found."""
        def fake_run_missing(cmd, **kwargs):
            raise FileNotFoundError()
        monkeypatch.setattr("main.subprocess.run", fake_run_missing)
        resp = client.post("/api/prs/auth/login")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "not installed" in data["error"]

    def test_post_prs_auth_login_returns_opening_browser(self, client, monkeypatch):
        """Returns opening_browser status when gh is available."""
        import unittest.mock as mock

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: FakeResult())
        monkeypatch.setattr("main.asyncio.create_task", mock.Mock())
        resp = client.post("/api/prs/auth/login")
        assert resp.status_code == 200
        assert resp.json()["status"] == "opening_browser"

    def test_get_prs_returns_list(self, client, monkeypatch):
        """Lists PRs, sets olamo_created for runs with matching PR number, normalizes author."""
        import json

        gh_prs = json.dumps([
            {
                "number": 42,
                "title": "Add dark mode",
                "url": "https://github.com/owner/repo/pull/42",
                "headRefName": "feature/dark-mode",
                "author": {"login": "anil"},
            },
            {
                "number": 41,
                "title": "Fix nav bug",
                "url": "https://github.com/owner/repo/pull/41",
                "headRefName": "fix/nav",
                "author": {"login": "bob"},
            },
        ])
        gh_repo = json.dumps({"nameWithOwner": "owner/repo"})

        def fake_run(cmd, **kwargs):
            class R:
                returncode = 0
                stderr = ""
            r = R()
            r.stdout = gh_repo if "repo" in cmd else gh_prs
            return r

        monkeypatch.setattr("main.subprocess.run", fake_run)

        # Create a run with PR #42 so it becomes olamo_created
        client.post("/api/runs", json={
            "description": "fix PR",
            "pr_url": "https://github.com/owner/repo/pull/42",
        })

        resp = client.get("/api/prs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo"] == "owner/repo"
        assert len(data["prs"]) == 2

        pr42 = next(p for p in data["prs"] if p["number"] == 42)
        pr41 = next(p for p in data["prs"] if p["number"] == 41)
        assert pr42["olamo_created"] is True
        assert pr41["olamo_created"] is False
        assert pr42["author"] == "anil"   # normalized from {"login": "anil"}
        assert pr41["author"] == "bob"

    def test_get_pr_check_returns_data(self, client, monkeypatch):
        """Passes gh pr view JSON through to caller."""
        import json
        gh_data = {
            "comments": [],
            "reviews": [],
            "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
        }

        class FakeResult:
            returncode = 0
            stderr = ""
        r = FakeResult()
        r.stdout = json.dumps(gh_data)
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: r)

        resp = client.get("/api/prs/42/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "statusCheckRollup" in data
        assert data["statusCheckRollup"][0]["conclusion"] == "SUCCESS"

    def test_get_pr_check_error(self, client, monkeypatch):
        """Returns error field with HTTP 200 when gh fails."""
        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "PR not found"
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs/99/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert data["error"]
