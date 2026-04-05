"""Tests for app.agents, app.settings, and related functions."""

import pytest
from dataclasses import asdict

from app.agents import AGENT_CONFIGS, build_agents
from app.models import (
    AgentEngineConfig,
    AppSettings,
    ModelConfig,
    _agent_engine_config_from_dict,
    _ALL_REVIEWERS,
    get_default_engine_config,
    _settings_from_dict,
)
from app.settings import SettingsStore


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
        _, tools, _ = AGENT_CONFIGS["code-reviewer"]
        assert "Bash" not in tools

    def test_repo_manager_uses_haiku(self):
        _, _, model_key = AGENT_CONFIGS["repo-manager"]
        assert model_key == "haiku_model"

    def test_lead_developer_uses_opus(self):
        _, _, model_key = AGENT_CONFIGS["lead-developer"]
        assert model_key == "opus_model"


class TestAgentConfigMerge:
    """Unit tests for per-run agent_configs merge in _execute_run."""

    def test_agent_config_override_takes_precedence(self):
        """Per-run agent_configs override replaces the global config for that role."""
        from dataclasses import replace

        base = AppSettings(agent_configs={
            "developer": AgentEngineConfig(engine="claude"),
            "reviewer": AgentEngineConfig(engine="claude"),
        })

        override_dict = {
            "engine": "copilot",
            "model_config": {
                "mode": "simple",
                "model": "gpt-5",
                "provider_type": "openai",
                "base_url": "",
                "api_key": "",
                "extra_params": {},
            },
            "mcp_servers": {},
        }

        run_agent_overrides = {"developer": override_dict}
        merged_agents = dict(base.agent_configs)
        for role, cfg_dict in run_agent_overrides.items():
            merged_agents[role] = _agent_engine_config_from_dict(cfg_dict)

        merged_settings = replace(base, agent_configs=merged_agents)

        assert merged_settings.agent_configs["developer"].engine == "copilot"
        assert merged_settings.agent_configs["developer"].model_config.model == "gpt-5"
        assert merged_settings.agent_configs["reviewer"].engine == "claude"

    def test_scalar_override_excludes_agent_configs(self):
        """agent_configs key is excluded from the scalar AppSettings merge to avoid TypeError."""
        base = AppSettings()
        raw_override = {
            "max_design_cycles": 7,
            "agent_configs": {"developer": {"engine": "copilot", "model_config": {}, "mcp_servers": {}}},
        }
        fields = set(AppSettings.__dataclass_fields__) - {"agent_configs"}
        filtered = {k: v for k, v in raw_override.items() if k in fields}
        settings = AppSettings(**{**asdict(base), **filtered})
        assert settings.max_design_cycles == 7
        assert settings.agent_configs == base.agent_configs

    def test_scalar_merge_with_nonempty_agent_configs_does_not_raise(self):
        """asdict(base) must not pass agent_configs plain dicts into AppSettings constructor."""
        base = AppSettings(agent_configs={"developer": AgentEngineConfig(engine="copilot")})

        fields = set(AppSettings.__dataclass_fields__) - {"agent_configs"}
        raw_override = {"max_design_cycles": 3}
        filtered = {k: v for k, v in raw_override.items() if k in fields}
        base_dict = {k: v for k, v in asdict(base).items() if k != "agent_configs"}
        settings = AppSettings(**{**base_dict, **filtered}, agent_configs=base.agent_configs)
        assert settings.max_design_cycles == 3
        assert settings.agent_configs["developer"].engine == "copilot"


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
