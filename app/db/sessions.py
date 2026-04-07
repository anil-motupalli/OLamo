"""agent_sessions — CRUD helpers for the agent_sessions table.

Each row maps a (run_id, role) pair to a Copilot SDK session ID and a frozen
snapshot of the ModelConfig used when the session was created.  This enables
cross-restart session resume via ``resume_session()``.

All methods accept an ``aiosqlite.Connection`` so they can share the same
connection used by ``OLamoDb``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,
    agent_name      TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    settings_snapshot TEXT NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'active',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at       TEXT,
    UNIQUE(run_id, role)
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_lookup
    ON agent_sessions(run_id, role);
"""


async def ensure_schema(conn: "aiosqlite.Connection") -> None:  # type: ignore
    """Create the agent_sessions table if it does not exist."""
    await conn.executescript(_SCHEMA)
    await conn.commit()


async def upsert_session(
    conn: "aiosqlite.Connection",  # type: ignore
    run_id: str,
    role: str,
    agent_name: str,
    session_id: str,
    settings_snapshot: dict,
) -> None:
    """Insert or update a session row for (run_id, role)."""
    await conn.execute(
        """
        INSERT INTO agent_sessions (run_id, agent_name, role, session_id, settings_snapshot)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(run_id, role) DO UPDATE SET
            session_id       = excluded.session_id,
            agent_name       = excluded.agent_name,
            settings_snapshot = excluded.settings_snapshot,
            status           = 'active',
            closed_at        = NULL
        """,
        (run_id, agent_name, role, session_id, json.dumps(settings_snapshot)),
    )
    await conn.commit()


async def lookup_session(
    conn: "aiosqlite.Connection",  # type: ignore
    run_id: str,
    role: str,
) -> dict | None:
    """Return the active session row for (run_id, role), or None."""
    async with conn.execute(
        "SELECT session_id, status, settings_snapshot FROM agent_sessions WHERE run_id=? AND role=?",
        (run_id, role),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "session_id": row["session_id"],
        "status": row["status"],
        "settings_snapshot": json.loads(row["settings_snapshot"]),
    }


async def mark_closed(
    conn: "aiosqlite.Connection",  # type: ignore
    run_id: str,
    role: str,
) -> None:
    """Mark a session as closed."""
    ts = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "UPDATE agent_sessions SET status='closed', closed_at=? WHERE run_id=? AND role=?",
        (ts, run_id, role),
    )
    await conn.commit()


async def mark_expired(
    conn: "aiosqlite.Connection",  # type: ignore
    run_id: str,
    role: str,
) -> None:
    """Mark a session as expired (resume failed)."""
    ts = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "UPDATE agent_sessions SET status='expired', closed_at=? WHERE run_id=? AND role=?",
        (ts, run_id, role),
    )
    await conn.commit()


async def mark_all_closed_for_run(
    conn: "aiosqlite.Connection",  # type: ignore
    run_id: str,
) -> None:
    """Mark all active sessions for a run_id as closed."""
    ts = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "UPDATE agent_sessions SET status='closed', closed_at=? WHERE run_id=? AND status='active'",
        (ts, run_id),
    )
    await conn.commit()


async def prune_old_sessions(
    conn: "aiosqlite.Connection",  # type: ignore
    max_age_days: int = 7,
) -> int:
    """Delete closed/expired sessions older than max_age_days. Returns count deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    cur = await conn.execute(
        "DELETE FROM agent_sessions WHERE status IN ('closed', 'expired') AND closed_at < ?",
        (cutoff,),
    )
    await conn.commit()
    return cur.rowcount
