"""Tests for app.models dataclasses."""

from dataclasses import asdict

from app.models import (
    HAIKU_MODEL,
    MAX_BUILD_CYCLES,
    MAX_DESIGN_CYCLES,
    MAX_IMPL_CYCLES,
    MAX_PR_CYCLES,
    OPUS_MODEL,
    PM_MAIN_MODEL,
    SONNET_MODEL,
    AgentEngineConfig,
    AppSettings,
    ModelConfig,
    RunRecord,
    RunStatus,
)


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
        assert s.opus_model == OPUS_MODEL

    def test_can_override_cycle_limits(self):
        s = AppSettings(max_design_cycles=5, max_pr_cycles=10)
        assert s.max_design_cycles == 5
        assert s.max_pr_cycles == 10
        assert s.max_impl_cycles == MAX_IMPL_CYCLES

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

    def test_post_init_accepts_advanced_with_no_base_url(self):
        s = AppSettings(agent_configs={
            "developer": AgentEngineConfig(
                model_config=ModelConfig(mode="advanced", model="gpt-4", base_url="")
            )
        })
        assert s.agent_configs["developer"].model_config.mode == "advanced"

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
        assert RunStatus.INTERRUPTED == "interrupted"

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
