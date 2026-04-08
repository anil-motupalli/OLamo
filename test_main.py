"""Compatibility shim — all tests have moved to tests/.

Run with:
    pytest tests/ -v
    pytest tests/ -v -k "TestSettingsStore"

This file re-exports every test class so that ``pytest test_main.py`` still
discovers and runs the full suite.
"""

# ruff: noqa: F401, F403
from tests.test_models import (
    TestAppSettings,
    TestModelConfig,
    TestAgentEngineConfig,
    TestRunRecord,
)
from tests.test_agents import (
    TestBuildAgents,
    TestAgentConfigs,
    TestAgentConfigMerge,
    TestGetDefaultEngineConfig,
    TestSettingsFromDict,
)
from tests.test_helpers import (
    TestParseStageAnnouncement,
    TestReviewerPrompt,
    TestBuildPmPrompt,
    TestApprovalGate,
    TestExtractCommentIds,
    TestParseBuildOutput,
    TestParseFindingResponses,
    TestParseRepoOutput,
    TestParseReviewJson,
)
from tests.test_settings import TestSettingsStore
from tests.test_broadcaster import TestSseBroadcaster
from tests.test_database import TestOLamoDb
from tests.test_run_manager import TestRunManager, run_manager  # noqa: F401 (fixture)
from tests.test_web_api import (
    TestOrchestrationMode,
    TestApiSettings,
    TestApiRuns,
    TestApiApproval,
    TestApiTeam,
    TestSpaFallback,
    TestApiPrs,
    TestOrchestrationEngineRouting,
    client,  # noqa: F401 (fixture)
)
from tests.test_engines import (
    TestClaudeEngine,
    TestCopilotEngine,
    TestOpenAIEngine,
    TestHeadlessMode,
    TestEngineRegistry,
)

