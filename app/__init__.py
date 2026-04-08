"""OLamo app package — exports all public symbols for backward compatibility."""

# Load .env file (if present) into os.environ before anything else so that
# env:VAR_NAME references in settings resolve correctly without requiring
# shell-profile edits.  python-dotenv is a hard dependency; if somehow missing,
# a clear warning is emitted rather than silently failing.
import logging as _logging
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)   # don't override vars the shell already set
except ImportError:  # pragma: no cover
    _logging.getLogger(__name__).warning(
        "python-dotenv is not installed; .env file will not be loaded. "
        "Run: pip install python-dotenv"
    )

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
    _ENGINE_DEFAULT_MODELS,
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
    _resolve_default_model,
    _agent_engine_config_from_dict,
    _settings_from_dict,
)

# Engine classes and registry
from .engines import AgentEngine, ClaudeEngine, CopilotEngine, CodexEngine, OpenAIEngine, MockEngine, ENGINE_REGISTRY

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
    _parse_stage_announcement,
    _reviewer_prompt,
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
    "_ENGINE_DEFAULT_MODELS",
    "_CLAUDE_TIER",
    "get_default_engine_config",
    "_resolve_default_model",
    "_agent_engine_config_from_dict",
    "_settings_from_dict",
    # Engines
    "AgentEngine",
    "ClaudeEngine",
    "CopilotEngine",
    "CodexEngine",
    "OpenAIEngine",
    "MockEngine",
    "ENGINE_REGISTRY",
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
    "_parse_stage_announcement",
    "_reviewer_prompt",
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
