from __future__ import annotations
from enum import Enum

class RunStatus(str, Enum):
    QUEUED      = "queued"
    RUNNING     = "running"
    INTERRUPTED = "interrupted"   # server restarted mid-run; can be resumed
    COMPLETED   = "completed"
    FAILED      = "failed"
