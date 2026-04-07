from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from .run_status import RunStatus

@dataclass
class RunRecord:
    id: str
    description: str
    status: RunStatus = RunStatus.QUEUED
    run_id: str = ""  # Human-readable YYYYMMDD_N format (e.g. "20260407_1")
    queued_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    log_dir: str | None = None
    pr_url: str = ""
    settings_override: dict = field(default_factory=dict)
