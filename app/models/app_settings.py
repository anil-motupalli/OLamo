from __future__ import annotations
from dataclasses import dataclass, field
from ..constants import (
    PM_MAIN_MODEL, OPUS_MODEL, SONNET_MODEL, HAIKU_MODEL,
    MAX_DESIGN_CYCLES, MAX_BUILD_CYCLES, MAX_IMPL_CYCLES, MAX_PR_CYCLES,
)
from .agent_engine_config import AgentEngineConfig

@dataclass
class AppSettings:
    pm_model:            str  = PM_MAIN_MODEL
    opus_model:          str  = OPUS_MODEL
    sonnet_model:        str  = SONNET_MODEL
    haiku_model:         str  = HAIKU_MODEL
    max_design_cycles:   int  = MAX_DESIGN_CYCLES
    max_build_cycles:    int  = MAX_BUILD_CYCLES
    max_impl_cycles:     int  = MAX_IMPL_CYCLES
    max_pr_cycles:       int  = MAX_PR_CYCLES
    api_base_url:        str  = ""
    orchestration_mode:  str  = "pm"
    agent_configs: dict[str, AgentEngineConfig] = field(default_factory=dict)
    copilot_github_token: str = ""
    headless:            bool = False

    def __post_init__(self) -> None:
        pass
