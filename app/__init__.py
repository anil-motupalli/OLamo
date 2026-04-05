"""OLamo app package — exports all public symbols for backward compatibility."""

# Constants
from .constants import (
    OPUS_MODEL,
    SONNET_MODEL,
    HAIKU_MODEL,
    PM_MAIN_MODEL,
    MAX_DESIGN_CYCLES,
    MAX_BUILD_CYCLES,
    MAX_IMPL_CYCLES,
    MAX_PR_CYCLES,
    _ALL_REVIEWERS,
    _DEFAULT_ENGINES,
    _CLAUDE_TIER,
    _COPILOT_DEFAULTS,
    AGENT_TOOLS,
)

# Models
from .models import (
    RunStatus,
    RunRecord,
    ModelConfig,
    AgentEngineConfig,
    AppSettings,
    get_default_engine_config,
    _agent_engine_config_from_dict,
    _settings_from_dict,
)

# Engine classes
from .engines import AgentEngine, ClaudeEngine, CopilotEngine, CodexEngine, OpenAIEngine, MockEngine

# Agents and prompts
from .agents import (
    AGENT_CONFIGS,
    build_agents,
    build_pm_prompt,
    LEAD_DEV_SYSTEM_PROMPT,
    DEVELOPER_SYSTEM_PROMPT,
    QA_SYSTEM_PROMPT,
    CODE_REVIEWER_SYSTEM_PROMPT,
    BUILD_SYSTEM_PROMPT,
    REPO_MANAGER_SYSTEM_PROMPT,
)

# Settings store
from .settings import SettingsStore

# Pipeline helpers and runners
from .pipeline import (
    ApprovalGate,
    _extract_comment_ids,
    _make_env,
    _parse_stage_announcement,
    _reviewer_prompt,
    reverse_string,
    run_pipeline_pm,
    run_pipeline_orchestrated,
    run_pipeline,
    run_pipeline_cli,
)

# Web infrastructure
from .web.broadcaster import SseBroadcaster
from .web.database import OLamoDb
from .web.run_manager import RunManager
from .web.github import _run_gh, _pr_number_from_url
from .web.app import create_app

__all__ = [
    # Constants
    "OPUS_MODEL",
    "SONNET_MODEL",
    "HAIKU_MODEL",
    "PM_MAIN_MODEL",
    "MAX_DESIGN_CYCLES",
    "MAX_BUILD_CYCLES",
    "MAX_IMPL_CYCLES",
    "MAX_PR_CYCLES",
    "_ALL_REVIEWERS",
    # Models
    "RunStatus",
    "RunRecord",
    "ModelConfig",
    "AgentEngineConfig",
    "AppSettings",
    "_COPILOT_DEFAULTS",
    "_DEFAULT_ENGINES",
    "_CLAUDE_TIER",
    "get_default_engine_config",
    "_agent_engine_config_from_dict",
    "_settings_from_dict",
    # Engines
    "AgentEngine",
    "ClaudeEngine",
    "CopilotEngine",
    "CodexEngine",
    "OpenAIEngine",
    "MockEngine",
    # Agents
    "AGENT_CONFIGS",
    "build_agents",
    "build_pm_prompt",
    "LEAD_DEV_SYSTEM_PROMPT",
    "DEVELOPER_SYSTEM_PROMPT",
    "QA_SYSTEM_PROMPT",
    "CODE_REVIEWER_SYSTEM_PROMPT",
    "BUILD_SYSTEM_PROMPT",
    "REPO_MANAGER_SYSTEM_PROMPT",
    # Settings
    "SettingsStore",
    # Pipeline
    "ApprovalGate",
    "_extract_comment_ids",
    "_make_env",
    "_parse_stage_announcement",
    "_reviewer_prompt",
    "reverse_string",
    "run_pipeline_pm",
    "run_pipeline_orchestrated",
    "run_pipeline",
    "run_pipeline_cli",
    # Web
    "SseBroadcaster",
    "OLamoDb",
    "RunManager",
    "_run_gh",
    "_pr_number_from_url",
    "create_app",
]
