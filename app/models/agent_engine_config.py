from __future__ import annotations
from dataclasses import dataclass, field
from .model_config import ModelConfig

@dataclass
class AgentEngineConfig:
    engine: str            = "claude"
    model_config: ModelConfig = field(default_factory=ModelConfig)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
