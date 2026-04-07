"""OLamoDb — thin async SQLite wrapper using aiosqlite."""

from __future__ import annotations

import json
from datetime import datetime, timezone

try:
    import aiosqlite
except ImportError:
    aiosqlite = None  # type: ignore  — only required for web/RunManager mode

from ..models import RunRecord, RunStatus
from ..db.sessions import ensure_schema as _ensure_agent_sessions


class OLamoDb:
    """
    Thin async SQLite wrapper using aiosqlite.

    Three tables mirror OLaCo's schema:
      - runs       — one row per run; upserted on every status change
      - events     — append-only stream (seq AUTOINCREMENT) of pipeline events
      - run_state  — live single-row projection of the current stage per run

    aiosqlite serialises all writes through a background thread queue, which is
    semantically equivalent to OLaCo's SemaphoreSlim(1,1) write lock.
    WAL mode allows concurrent readers alongside the single writer.
    """

    def __init__(self, path: str = "olamo.db") -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None  # type: ignore

    async def open(self) -> None:
        if aiosqlite is None:
            raise SystemExit("aiosqlite not installed. Run: pip install aiosqlite")
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._ensure_schema()
        await _ensure_agent_sessions(self._conn)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _ensure_schema(self) -> None:
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id                TEXT PRIMARY KEY,
                description       TEXT NOT NULL,
                status            TEXT NOT NULL,
                run_id            TEXT NOT NULL DEFAULT '',
                queued_at         TEXT NOT NULL,
                started_at        TEXT,
                completed_at      TEXT,
                error             TEXT,
                log_dir           TEXT,
                pr_url            TEXT NOT NULL DEFAULT '',
                settings_override TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS events (
                seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT NOT NULL,
                ts           TEXT NOT NULL,
                data         TEXT NOT NULL,
                type         TEXT,
                stage        TEXT,
                cycle        INTEGER,
                role         TEXT,
                action       TEXT,
                success      INTEGER,
                elapsed_ms   INTEGER,
                summary      TEXT,
                content_path TEXT,
                pr_url       TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);

            CREATE TABLE IF NOT EXISTS run_state (
                run_id          TEXT PRIMARY KEY,
                current_stage   TEXT,
                current_cycle   TEXT,
                last_agent      TEXT,
                last_agent_ok   INTEGER,
                last_summary    TEXT,
                checkpoint_data TEXT,
                updated_at      TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
        """)
        # Migrate existing tables that may be missing newer columns
        for col in [
            "ALTER TABLE run_state ADD COLUMN current_cycle TEXT",
            "ALTER TABLE run_state ADD COLUMN last_agent TEXT",
            "ALTER TABLE run_state ADD COLUMN last_agent_ok INTEGER",
            "ALTER TABLE run_state ADD COLUMN last_summary TEXT",
            "ALTER TABLE run_state ADD COLUMN checkpoint_data TEXT",
            "ALTER TABLE events ADD COLUMN type TEXT",
            "ALTER TABLE events ADD COLUMN stage TEXT",
            "ALTER TABLE events ADD COLUMN cycle INTEGER",
            "ALTER TABLE events ADD COLUMN role TEXT",
            "ALTER TABLE events ADD COLUMN action TEXT",
            "ALTER TABLE events ADD COLUMN success INTEGER",
            "ALTER TABLE events ADD COLUMN elapsed_ms INTEGER",
            "ALTER TABLE events ADD COLUMN summary TEXT",
            "ALTER TABLE events ADD COLUMN content_path TEXT",
            "ALTER TABLE events ADD COLUMN pr_url TEXT",
        ]:
            try:
                await self._conn.execute(col)
            except Exception:
                pass
        await self._conn.commit()

    def _row_to_run(self, row: "aiosqlite.Row") -> "RunRecord":  # type: ignore
        return RunRecord(
            id=row["id"],
            description=row["description"],
            status=RunStatus(row["status"]),
            run_id=row["run_id"],
            queued_at=row["queued_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
            log_dir=row["log_dir"],
            pr_url=row["pr_url"] or "",
            settings_override=json.loads(row["settings_override"] or "{}"),
        )

    async def upsert_run(self, run: "RunRecord") -> None:
        await self._conn.execute(
            """
            INSERT INTO runs
              (id, description, status, run_id, queued_at, started_at, completed_at, error, log_dir, pr_url, settings_override)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status            = excluded.status,
                run_id            = excluded.run_id,
                started_at        = excluded.started_at,
                completed_at      = excluded.completed_at,
                error             = excluded.error,
                log_dir           = excluded.log_dir,
                pr_url            = excluded.pr_url,
                settings_override = excluded.settings_override
            """,
            (
                run.id, run.description, run.status.value, run.run_id,
                run.queued_at, run.started_at, run.completed_at, run.error,
                run.log_dir, run.pr_url, json.dumps(run.settings_override),
            ),
        )
        await self._conn.commit()

    async def get_all_runs(self) -> list["RunRecord"]:
        async with self._conn.execute(
            "SELECT * FROM runs ORDER BY queued_at DESC"
        ) as cur:
            return [self._row_to_run(row) async for row in cur]

    async def insert_event(self, run_id: str, data: dict) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        success_val = None
        if "success" in data:
            success_val = 1 if data["success"] else 0
        cur = await self._conn.execute(
            """INSERT INTO events
               (run_id, ts, data, type, stage, cycle, role, action, success, elapsed_ms, summary, content_path, pr_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, ts, json.dumps(data),
                data.get("type"), data.get("stage"), data.get("cycle"),
                data.get("role"), data.get("action"),
                success_val,
                data.get("elapsed_ms"), data.get("summary"),
                data.get("content_path"), data.get("pr_url"),
            ),
        )
        await self._conn.commit()
        return cur.lastrowid  # seq assigned by DB

    async def get_events(self, run_id: str) -> list[dict]:
        """Return all events for a run, each augmented with its seq and ts."""
        async with self._conn.execute(
            "SELECT seq, ts, data FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ) as cur:
            results = []
            async for row in cur:
                event = json.loads(row["data"])
                event["seq"] = row["seq"]
                event["ts"] = row["ts"]
                results.append(event)
            return results

    async def get_events_since(self, run_id: str, after_seq: int) -> list[dict]:
        """Return events with seq > after_seq (for SSE gap-fill on reconnect)."""
        async with self._conn.execute(
            "SELECT seq, ts, data FROM events WHERE run_id=? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        ) as cur:
            results = []
            async for row in cur:
                event = json.loads(row["data"])
                event["seq"] = row["seq"]
                event["ts"] = row["ts"]
                results.append(event)
            return results

    async def get_events_since_global(self, after_seq: int) -> list[dict]:
        """Return all events across all runs with seq > after_seq (for SSE gap-fill)."""
        async with self._conn.execute(
            "SELECT seq, ts, data FROM events WHERE seq > ? ORDER BY seq",
            (after_seq,),
        ) as cur:
            results = []
            async for row in cur:
                event = json.loads(row["data"])
                event["seq"] = row["seq"]
                event["ts"] = row["ts"]
                results.append(event)
            return results

    async def get_event_content_path(self, run_id: str, seq: int) -> str | None:
        """Return the content_path for a specific event (e.g. spec markdown file)."""
        async with self._conn.execute(
            "SELECT content_path FROM events WHERE run_id=? AND seq=?", (run_id, seq)
        ) as cur:
            row = await cur.fetchone()
        return row["content_path"] if row else None

    async def update_event_content_path(self, run_id: str, seq: int, content_path: str) -> None:
        """Update the content_path for an already-inserted event."""
        await self._conn.execute(
            "UPDATE events SET content_path=? WHERE run_id=? AND seq=?",
            (content_path, run_id, seq),
        )
        await self._conn.commit()

    async def upsert_run_state(
        self,
        run_id: str,
        current_stage: str | None = None,
        current_cycle: str | None = None,
        last_agent: str | None = None,
        last_agent_ok: bool | None = None,
        last_summary: str | None = None,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        ok_val = None if last_agent_ok is None else (1 if last_agent_ok else 0)
        await self._conn.execute(
            """
            INSERT INTO run_state (run_id, current_stage, current_cycle, last_agent, last_agent_ok, last_summary, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                current_stage = COALESCE(excluded.current_stage, current_stage),
                current_cycle = COALESCE(excluded.current_cycle, current_cycle),
                last_agent    = COALESCE(excluded.last_agent, last_agent),
                last_agent_ok = COALESCE(excluded.last_agent_ok, last_agent_ok),
                last_summary  = COALESCE(excluded.last_summary, last_summary),
                updated_at    = excluded.updated_at
            """,
            (run_id, current_stage, current_cycle, last_agent, ok_val, last_summary, ts),
        )
        await self._conn.commit()

    async def get_run_state(self, run_id: str) -> dict | None:
        async with self._conn.execute(
            "SELECT run_id, current_stage, current_cycle, last_agent, last_agent_ok, last_summary, checkpoint_data, updated_at FROM run_state WHERE run_id=?",
            (run_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "current_stage": row["current_stage"],
            "current_cycle": row["current_cycle"],
            "last_agent": row["last_agent"],
            "last_agent_ok": bool(row["last_agent_ok"]) if row["last_agent_ok"] is not None else None,
            "last_summary": row["last_summary"],
            "checkpoint_data": row["checkpoint_data"],
            "updated_at": row["updated_at"],
        }

    async def save_checkpoint(self, run_id: str, data: dict) -> None:
        """Persist pipeline checkpoint data (plan, pr_result, stage, etc.)."""
        ts = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO run_state (run_id, checkpoint_data, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                checkpoint_data = excluded.checkpoint_data,
                updated_at      = excluded.updated_at
            """,
            (run_id, json.dumps(data), ts),
        )
        await self._conn.commit()

    async def load_checkpoint(self, run_id: str) -> dict | None:
        """Load persisted checkpoint data, or None if not present."""
        async with self._conn.execute(
            "SELECT checkpoint_data FROM run_state WHERE run_id=?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None or not row["checkpoint_data"]:
            return None
        return json.loads(row["checkpoint_data"])
