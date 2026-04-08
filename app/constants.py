"""Constants loaded from config/defaults.json — single source of truth for all defaults."""
from __future__ import annotations
import json
from pathlib import Path

_DEFAULTS = json.loads((Path(__file__).parent.parent / "config" / "defaults.json").read_text())

# Model aliases
OPUS_MODEL:    str = _DEFAULTS["models"]["opus"]
SONNET_MODEL:  str = _DEFAULTS["models"]["sonnet"]
HAIKU_MODEL:   str = _DEFAULTS["models"]["haiku"]
PM_MAIN_MODEL: str = _DEFAULTS["models"]["pm"]

# Pipeline limits
MAX_DESIGN_CYCLES: int = _DEFAULTS["pipeline"]["max_design_cycles"]
MAX_BUILD_CYCLES:  int = _DEFAULTS["pipeline"]["max_build_cycles"]
MAX_IMPL_CYCLES:   int = _DEFAULTS["pipeline"]["max_impl_cycles"]
MAX_PR_CYCLES:     int = _DEFAULTS["pipeline"]["max_pr_cycles"]

# Agent defaults (tuple for immutability)
_ALL_REVIEWERS:          tuple[str, ...] = tuple(_DEFAULTS["agents"]["reviewers"])
_DEFAULT_ENGINES:        dict[str, str]  = _DEFAULTS["agents"]["default_engines"]
_ENGINE_DEFAULT_MODELS:  dict[str, dict[str, str]] = _DEFAULTS["agents"]["engine_default_models"]
AGENT_TOOLS:             dict[str, list[str]] = _DEFAULTS["agents"]["tools"]

# Legacy aliases for backward compatibility — deprecated, prefer _ENGINE_DEFAULT_MODELS
_CLAUDE_TIER:      dict[str, str] = {role: models["claude"] for role, models in _ENGINE_DEFAULT_MODELS.items()}
_COPILOT_DEFAULTS: dict[str, str] = {role: models["copilot"] for role, models in _ENGINE_DEFAULT_MODELS.items()}
