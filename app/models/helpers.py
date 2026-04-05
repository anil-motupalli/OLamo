from __future__ import annotations
from ..constants import _DEFAULT_ENGINES, _CLAUDE_TIER, _COPILOT_DEFAULTS
from .model_config import ModelConfig
from .agent_engine_config import AgentEngineConfig
from .app_settings import AppSettings


def get_default_engine_config(role: str, settings: AppSettings) -> AgentEngineConfig:
    engine = _DEFAULT_ENGINES.get(role, "claude")
    if engine == "copilot":
        model = _COPILOT_DEFAULTS.get(role, "")
    else:
        tier_field = _CLAUDE_TIER.get(role, "sonnet_model")
        model = getattr(settings, tier_field)
    return AgentEngineConfig(engine=engine, model_config=ModelConfig(model=model))


def _agent_engine_config_from_dict(d: dict) -> AgentEngineConfig:
    mc = d.get("model_config") or {}
    known_mc = set(ModelConfig.__dataclass_fields__)
    return AgentEngineConfig(
        engine=d.get("engine", "claude"),
        model_config=ModelConfig(**{k: v for k, v in mc.items() if k in known_mc}) if mc else ModelConfig(),
        mcp_servers=d.get("mcp_servers") or {},
    )


def _settings_from_dict(d: dict) -> AppSettings:
    d = dict(d)
    agent_configs_raw = d.pop("agent_configs", None) or {}
    filtered = {k: v for k, v in d.items() if k in AppSettings.__dataclass_fields__}
    agent_configs = {
        role: _agent_engine_config_from_dict(cfg) if isinstance(cfg, dict) else cfg
        for role, cfg in agent_configs_raw.items()
    }
    return AppSettings(**filtered, agent_configs=agent_configs)
