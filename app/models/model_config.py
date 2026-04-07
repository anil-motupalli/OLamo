from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class ModelConfig:
    mode: str          = "simple"
    model: str         = ""
    provider_type: str = "openai"
    base_url: str      = ""
    api_key: str       = ""
    extra_params: dict = field(default_factory=dict)
