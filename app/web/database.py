"""OLamoDb — thin async SQLite wrapper using aiosqlite."""

from __future__ import annotations

import json
from datetime import datetime, timezone

try:
    import aiosqlite
except ImportError:
    aiosqlite = None  # type: ignore  — only required for web/RunManager mode

from ..models import RunRecord, RunStatus


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
                queued_at         TEXT NOT NULL,
                started_at        TEXT,
                completed_at      TEXT,
                error             TEXT,
                log_dir           TEXT,
                pr_url            TEXT NOT NULL DEFAULT '',
                settings_override TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS events (
                seq    INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts     TEXT NOT NULL,
                data   TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);

            CREATE TABLE IF NOT EXISTS run_state (
                run_id          TEXT PRIMARY KEY,
                current_stage   TEXT,
                current_cycle   TEXT,
                last_agent      TEXT,
                checkpoint_data TEXT,
                updated_at      TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
        """)
        # Migrate existing run_state tables that may be missing the new columns
        try:
            await self._conn.execute("ALTER TABLE run_state ADD COLUMN current_cycle TEXT")
        except Exception:
            pass  # column already exists
        try:
            await self._conn.execute("ALTER TABLE run_state ADD COLUMN last_agent TEXT")
        except Exception:
            pass  # column already exists
        try:
            await self._conn.execute("ALTER TABLE run_state ADD COLUMN checkpoint_data TEXT")
        except Exception:
            pass  # column already exists
        await self._conn.commit()

    def _row_to_run(self, row: "aiosqlite.Row") -> "RunRecord":  # type: ignore
        return RunRecord(
            id=row["id"],
            description=row["description"],
            status=RunStatus(row["status"]),
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
              (id, description, status, queued_at, started_at, completed_at, error, log_dir, pr_url, settings_override)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status            = excluded.status,
                started_at        = excluded.started_at,
                completed_at      = excluded.completed_at,
                error             = excluded.error,
                log_dir           = excluded.log_dir
            """,
            (
                run.id, run.description, run.status.value, run.queued_at,
                run.started_at, run.completed_at, run.error, run.log_dir,
                run.pr_url, json.dumps(run.settings_override),
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
        cur = await self._conn.execute(
            "INSERT INTO events (run_id, ts, data) VALUES (?, ?, ?)",
            (run_id, ts, json.dumps(data)),
        )
        await self._conn.commit()
        return cur.lastrowid  # equivalent to OLaCo's last_insert_rowid()

    async def get_events(self, run_id: str) -> list[dict]:
        async with self._conn.execute(
            "SELECT data FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ) as cur:
            return [json.loads(row["data"]) async for row in cur]

    async def upsert_run_state(
        self,
        run_id: str,
        current_stage: str | None = None,
        current_cycle: str | None = None,
        last_agent: str | None = None,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO run_state (run_id, current_stage, current_cycle, last_agent, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                current_stage = COALESCE(excluded.current_stage, current_stage),
                current_cycle = COALESCE(excluded.current_cycle, current_cycle),
                last_agent    = COALESCE(excluded.last_agent, last_agent),
                updated_at    = excluded.updated_at
            """,
            (run_id, current_stage, current_cycle, last_agent, ts),
        )
        await self._conn.commit()

    async def get_run_state(self, run_id: str) -> dict | None:
        async with self._conn.execute(
            "SELECT run_id, current_stage, current_cycle, last_agent, checkpoint_data, updated_at FROM run_state WHERE run_id=?",
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
