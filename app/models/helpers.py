from __future__ import annotations
import logging
import os
from ..constants import _DEFAULT_ENGINES, _CLAUDE_TIER, _COPILOT_DEFAULTS
from .model_config import ModelConfig
from .agent_engine_config import AgentEngineConfig
from .app_settings import AppSettings

logger = logging.getLogger(__name__)


def resolve_secret(value: str) -> str:
    """Resolve a secret value that may be stored outside of code.

    Supported prefixes:

    - ``env:VAR_NAME``          — Read from the named environment variable.
      Store the key with ``export OLAMO_ZAI_KEY=sk-...`` (shell rc / CI secret).

    - ``keyring:service/account`` — Read from the OS keychain via the
      ``keyring`` library (macOS Keychain, Windows Credential Manager, Linux
      Secret Service). Install with ``pip install keyring`` and store with
      ``python -c "import keyring; keyring.set_password('olamo', 'zai_key', 'sk-...')"``
      then set ``api_key: keyring:olamo/zai_key`` in your settings file.

    Any other value is returned as-is (including empty strings and plain keys
    set directly — though the latter is discouraged for production use).
    """
    if not value:
        return value
    if value.startswith("env:"):
        var = value[4:]
        resolved = os.environ.get(var, "")
        if not resolved:
            logger.warning("Secret env var %r is not set", var)
        return resolved
    if value.startswith("keyring:"):
        service_account = value[8:]
        try:
            import keyring  # type: ignore[import]
            service, _, account = service_account.partition("/")
            resolved = keyring.get_password(service, account) or ""
            if not resolved:
                logger.warning("No keyring entry for service=%r account=%r", service, account)
            return resolved
        except ImportError:
            logger.warning(
                "keyring: prefix used but 'keyring' package is not installed. "
                "Run: pip install keyring"
            )
            return ""
    return value

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
