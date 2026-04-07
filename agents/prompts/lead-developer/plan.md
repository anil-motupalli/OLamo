# Plan: Copilot SDK Session Resume Integration (Revised)

## Problem Statement

Currently `CopilotEngine.run()` creates a **new** Copilot SDK session on every agent call and disconnects it in `finally`. This means:
- No conversation context is preserved across calls to the same agent within a pipeline run
- The Copilot SDK's `resume_session()` capability (for cross-process restarts) is unused
- Each call pays session-initialization overhead

We need to wire session reuse (in-process) and session resume (cross-restart) into OLamo's existing run lifecycle.

---

## Architecture Overview

Two **distinct sub-features** with independent logic paths:

| Sub-feature | Scope | Mechanism | DB Needed |
|---|---|---|---|
| **(A) In-Process Session Reuse** | Single pipeline execution, same process | Cache session object in-memory by `(run_id, role)`; skip `disconnect()` | No |
| **(B) Cross-Restart Session Resume** | Server crash/restart, new process | Look up `session_id` from `agent_sessions` table → call `resume_session()` | Yes |

These are implemented independently. (A) is simple and high-value. (B) builds on top of (A)'s persistence layer.

---

## Libraries & Dependencies

- **github-copilot-sdk** (already a dependency) — provides `CopilotClient`, `create_session()`, `resume_session()`, `client_name`
- **sqlite3** (stdlib) — for `agent_sessions` table; OLamo already uses SQLite (`olamo.db`)
- No new external dependencies required

---

## Data Model

### New Table: `agent_sessions`

```sql
CREATE TABLE IF NOT EXISTS agent_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,
    agent_name      TEXT    NOT NULL,       -- e.g. "20260407_1_lead-developer"
    role            TEXT    NOT NULL,       -- e.g. "lead-developer"
    session_id      TEXT    NOT NULL,       -- Copilot SDK opaque session ID string
    settings_snapshot TEXT NOT NULL,        -- JSON: frozen ModelConfig at creation time
    status          TEXT    NOT NULL DEFAULT 'active',  -- 'active' | 'closed' | 'expired'
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at       TEXT,
    UNIQUE(run_id, role)
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_lookup
    ON agent_sessions(run_id, role);
```

**Design decisions:**
- `settings_snapshot` stores **only the per-agent `ModelConfig`** as JSON (not full `AppSettings`). This keeps rows small and avoids deserialization issues if the settings schema evolves.
- `session_id` is the Copilot SDK's opaque string — treated as a blob; we never parse it.
- `UNIQUE(run_id, role)` ensures one row per agent per run.
- **Cleanup policy**: Sessions with `status='closed'` older than 7 days are purged via a maintenance method `_prune_old_sessions()`. Called once at startup in `CopilotEngine.start()`.

### RunRecord Change

Add `run_id` field alongside existing UUID `id`:

```python
@dataclass
class RunRecord:
    id: str                    # UUID — existing primary key, unchanged
    run_id: str = ""           # NEW: YYYYMMDD_N format (e.g. "20260407_1")
    description: str = ""
    status: RunStatus = RunStatus.QUEUED
    # ... all existing fields unchanged ...
```

**ID coexistence**: `id` (UUID) remains the primary key for all internal lookups (`RunManager.resume()`, web UI). `run_id` is a human-readable, time-scoped identifier used for:
- Agent session naming (`{run_id}_{role}`)
- Log directory naming (`logs/{run_id}/`)
- Display in UI/cli output

Lookup mapping: `run_id -> id` via `SELECT id FROM runs WHERE run_id = ?`.

---

## Protocol Changes

### `AgentEngine` Protocol (`app/engines/base.py`)

Add `run_id` as an **optional keyword argument** with default `None`:

```python
class AgentEngine(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def run(
        self,
        role: str,
        prompt: str,
        system_prompt: str,
        tools: list[str],
        model: str,
        model_config: ModelConfig,
        mcp_servers: dict[str, dict],
        on_event: Callable[[dict], Awaitable[None]],
        run_id: str | None = None,       # NEW - optional, defaults to None
    ) -> str: ...
```

**Impact on all 5 engines:**

| Engine | Change Required |
|---|---|
| `CopilotEngine` | **Major**: implement session reuse + resume logic using `run_id` |
| `ClaudeEngine` | **None**: add `run_id=None` to signature, ignore |
| `CodexEngine` | **None**: add `run_id=None` to signature, ignore |
| `OpenAIEngine` | **None**: add `run_id=None` to signature, ignore |
| `MockEngine` | **None**: add `run_id=None` to signature, ignore |

All non-Copilot engines simply accept and ignore the parameter. Since Python protocols use structural subtyping, adding an optional kwarg with a default does not break conformance.

---

## Run ID Generation

### Format: `YYYYMMDD_N`

- `YYYYMMDD` = current date in UTC
- `N` = auto-incrementing integer per day, starting at 1

### Atomic Allocation

Use **application-level `asyncio.Lock`** for simplicity (OLamo is single-process):

```python
_run_id_lock = asyncio.Lock()

async def _next_run_id(conn) -> str:
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    async with _run_id_lock:
        row = await conn.execute_fetchall(
            "SELECT MAX(CAST(SUBSTR(run_id, 10) AS INTEGER)) FROM runs WHERE run_id LIKE ?",
            (f"{date_part}%",),
        )
        next_n = (row[0][0] if row and row[0][0] else 0) + 1
        return f"{date_part}_{next_n}"
```

If multi-process enqueue is needed later, migrate to a pure SQL approach using `INSERT ... SELECT MAX()+1` inside a transaction.

---

## CopilotEngine Rewrite

### Internal State

```python
class CopilotEngine:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client = None
        # Sub-feature (A): In-process session cache
        self._sessions: dict[str, object] = {}   # key: "{run_id}_{role}" -> Session object
```

### `run()` Method - Revised Logic

```
def run(self, ..., run_id: str | None = None) -> str:
    if run_id is None:
        # Legacy path: no run_id -> create/disconnect as before (backward compat)
        return await self._run_fresh(...)

    cache_key = f"{run_id}_{role}"

    # --- Sub-feature (A): In-Process Reuse ---
    if cache_key in self._sessions:
        # Reuse existing session - do NOT disconnect
        session = self._sessions[cache_key]
    else:
        # First call for this (run_id, role)
        agent_name = f"{run_id}_{role}"
        kwargs["client_name"] = agent_name   # Name the session in Copilot SDK

        if should_resume(run_id, role):
            # --- Sub-feature (B): Cross-Restart Resume ---
            session_id = lookup_session_id(run_id, role)
            session = await self._client.resume_session(session_id=session_id, **kwargs)
        else:
            session = await self._client.create_session(**kwargs)

        # Persist to DB (for future resume)
        upsert_agent_session(run_id, role, agent_name, session.id, model_config)

        # Cache in-memory for reuse
        self._sessions[cache_key] = session

    # Use session (no disconnect!)
    event = await session.send_and_wait(prompt, timeout=600.0)
    result = extract_content(event)

    await on_event({"type": "agent_message", "role": role, "text": result[:300]})
    return result
```

### Key Behavioral Changes from Current Code

1. **`session.disconnect()` is REMOVED from `finally`** when `run_id` is provided. Sessions stay alive for reuse.
2. **Legacy path preserved**: When `run_id is None`, the old create->use->disconnect cycle still runs. This ensures backward compatibility for any caller that doesn't pass `run_id`.
3. **`stop()` cleans up cached sessions**: When the engine stops, all cached sessions are disconnected.

### `stop()` Method - Updated

```python
async def stop(self) -> None:
    # Disconnect all cached sessions
    for cache_key, session in list(self._sessions.items()):
        try:
            await session.disconnect()
        except Exception:
            pass  # best-effort
        mark_session_closed(cache_key)
    self._sessions.clear()

    if self._client is not None:
        await self._client.stop()
        self._client = None
```

### New Method: `close_run(run_id: str)`

```python
async def close_run(run_id: str) -> None:
    """Disconnect and close all sessions for a given run_id.

    Call sites:
    - After successful pipeline completion (in the finally block of run_pipeline_orchestrated)
    - On pipeline failure (same finally block)
    - NOT called during normal operation - sessions stay alive between agent calls
    """
    prefix = f"{run_id}_"
    keys_to_close = [k for k in self._sessions if k.startswith(prefix)]
    for key in keys_to_close:
        session = self._sessions.pop(key, None)
        if session:
            try:
                await session.disconnect()
            except Exception:
                pass
        mark_session_closed(key)
```

**When `close_run()` is called**:

| Scenario | Behavior |
|---|---|
| Normal completion | `finally` block in `run_pipeline_orchestrated` calls `close_run(run_id)` after all stages done |
| Pipeline failure / exception | Same `finally` block - ensures cleanup |
| Process crash (unhandled) | OS reclaims resources; sessions expire server-side. Next restart uses resume or falls back to fresh |
| In-process reuse between calls | `close_run()` is NOT called - sessions stay alive in `self._sessions` |

### Resume Fallback Logic

```python
def _should_resume(self, run_id: str, role: str) -> bool:
    """Check if there's a resumable session in DB for this (run_id, role)."""
    row = db_query(
        "SELECT session_id, status FROM agent_sessions WHERE run_id=? AND role=?",
        (run_id, role),
    )
    return row is not None and row["status"] == "active"
```

On resume failure (session expired, network error, etc.):

```python
try:
    session = await self._client.resume_session(session_id=stored_id, **kwargs)
except Exception as log_e:
    logger.warning("Resume failed for %s/%s: %s. Falling back to fresh session.", run_id, role, log_e)
    mark_session_expired(run_id, role)
    session = await self._client.create_session(**kwargs)
    upsert_agent_session(run_id, role, agent_name, session.id, model_config)
```

---

## Threading `run_id` Through the Call Stack

### Call Chain

```
RunManager._execute_run()
  |-- generates run_id (YYYYMMDD_N)
  |-- stores in RunRecord
  |
  +-- run_pipeline(task, settings, ..., run_id=run_id)         <- NEW param
       +-- run_pipeline_orchestrated(task, settings, ..., run_id=run_id)  <- NEW param
            +-- call(role, prompt)   [inner function]
                 +-- eng.run(role, prompt, ..., run_id=run_id)  <- pass through
```

### Specific Changes

1. **`app/pipeline/runner.py`** - `run_pipeline()` gains `run_id: str | None = None` parameter, passes to `run_pipeline_orchestrated()`. `run_pipeline_cli()` also gains it.
2. **`app/pipeline/orchestrated.py`** - `run_pipeline_orchestrated()` gains `run_id: str | None = None` parameter; inner `call()` function passes it to `eng.run(...)`.
3. **`app/pipeline/pm.py`** - `run_pipeline_pm()` gains `run_id: str | None = None` parameter (passthrough only; PM mode doesn't call engine `run()` directly).
4. **`RunManager`** - generates `run_id` before calling `run_pipeline()`, stores on `RunRecord`.

---

## Checkpoint Interaction

The existing checkpoint system (`save_checkpoint` callback) tracks **pipeline stage progress** (completed_stage, plan, last_diff, etc.). Session resume operates at a **different layer**:

| System | What it tracks | Scope |
|---|---|---|
| Checkpoint | Pipeline stage state (design done? impl done? PR created?) | Pipeline orchestration |
| Agent Sessions | Copilot SDK session IDs per agent | Per-agent conversation continuity |

**Relationship: Complementary, independent.**

- When resuming from a checkpoint at Stage 3, the checkpoint system handles "skip Stages 1-2".
- Session resume handles "the lead-developer agent may still have its Copilot conversation context".
- If a session for a previously-completed stage agent is expired/missing, that's **fine** - the pipeline doesn't need that agent again. Only agents that will be **called in future stages** need active sessions.
- **No change needed to checkpoint schema or save/load logic.**

**Edge case**: Resuming at Stage 2 (implementation) after a crash. The lead-developer session from Stage 1 may be stale. That's acceptable - Stage 2 calls the `developer` role, not `lead-developer`. Each `(run_id, role)` pair is looked up independently.

---

## Implementation Steps (Ordered)

### Phase 1: Foundation (no behavior change)

1. **Add `run_id` field to `RunRecord`** (`app/models/run_record.py`)
   - Add `run_id: str = ""` field with default empty string
   - Existing code paths unaffected (empty string = no run_id)

2. **Update `AgentEngine` protocol** (`app/engines/base.py`)
   - Add `run_id: str | None = None` to `run()` signature

3. **Update all 5 engine `run()` signatures** to accept `run_id: str | None = None`
   - `CopilotEngine`: accept but don't use yet (next phase)
   - `ClaudeEngine`, `CodexEngine`, `OpenAIEngine`, `MockEngine`: accept and ignore

4. **Thread `run_id` through pipeline layer**
   - `app/pipeline/runner.py`: add `run_id` param to `run_pipeline()` and `run_pipeline_cli()`
   - `app/pipeline/orchestrated.py`: add `run_id` param to `run_pipeline_orchestrated()`, pass to `eng.run()`
   - `app/pipeline/pm.py`: add `run_id` param to `run_pipeline_pm()` (passthrough only)

5. **Create `agent_sessions` DB table** and helper module
   - New file: `app/db/sessions.py` (or similar location)
   - Contains: table creation, upsert, lookup, mark-closed, mark-expired, prune methods
   - Uses existing SQLite connection pattern (check how `olamo.db` is accessed currently)

6. **Implement run ID generation** in `RunManager`
   - `asyncio.Lock`-protected counter per day
   - Format: `YYYYMMDD_N`
   - Store on `RunRecord` at enqueue time

### Phase 2: In-Process Session Reuse (Sub-feature A)

7. **Rewrite `CopilotEngine.run()` with session caching**
   - Add `self._sessions: dict[str, object]` cache
   - When `run_id` provided: check cache -> create if missing -> skip `disconnect()`
   - Legacy path (`run_id is None`): keep current create/disconnect behavior
   - Set `client_name=f"{run_id}_{role}"` on session creation

8. **Update `CopilotEngine.stop()`** to drain and disconnect cached sessions

9. **Add `close_run(run_id)` method** to `CopilotEngine`

10. **Wire `close_run()` into `run_pipeline_orchestrated` finally block**
    - After the existing `for eng in engines_to_stop: await eng.stop()` loop
    - Call `copilot_engine.close_run(run_id)` if copilot_engine exists and run_id is set

### Phase 3: Cross-Restart Session Resume (Sub-feature B)

11. **Persist session info to `agent_sessions` table** on creation
    - In `CopilotEngine.run()`, after `create_session()` or successful `resume_session()`
    - Upsert: `run_id, role, agent_name, session.id, model_config_json`

12. **Implement resume lookup** in `CopilotEngine.run()`
    - Before creating fresh session, query DB for existing `active` session
    - If found: attempt `resume_session()` with fallback to fresh
    - If not found or expired: create fresh

13. **Session lifecycle management**
    - Mark `status='closed'` in DB on `stop()` / `close_run()`
    - Mark `status='expired'` on resume failure
    - Implement `_prune_old_sessions()` (delete `closed` rows > 7 days)
    - Call prune in `CopilotEngine.start()`

### Phase 4: Testing

14. **Unit tests for `agent_sessions` DB operations**
    - Test upsert, lookup, mark-closed, mark-expired, prune
    - Test UNIQUE constraint on (run_id, role)

15. **Unit test for run ID generation**
    - Test auto-increment within same day
    - Test reset on new day
    - Test thread-safety with concurrent generation

16. **Test for CopilotEngine session reuse (with mock SDK client)**
    - Mock `CopilotClient` to verify `create_session` called once, not N times
    - Verify `disconnect()` NOT called between reused calls
    - Verify `disconnect()` called on `stop()`
    - Verify fallback to legacy path when `run_id=None`

17. **Test for resume flow (with mock SDK client)**
    - Mock `resume_session` success path
    - Mock `resume_session` failure -> fallback to `create_session`
    - Verify DB upserted on both paths

18. **Integration test: end-to-end pipeline with `run_id`**
    - Run orchestrated pipeline with MockEngine (verifies passthrough)
    - If possible, run with real Copilot SDK in CI-like environment

---

## Edge Cases & Pitfalls

| Edge Case | Handling |
|---|---|
| **Session expired on server** | Catch exception in `resume_session()`, fall back to `create_session()`, mark old row as `expired` |
| **Concurrent runs** | Unique names guaranteed by `run_id` prefix (each run gets unique `YYYYMMDD_N`) |
| **Non-Copilot engines receive `run_id`** | Ignored via optional kwarg with `None` default - zero impact |
| **Settings changed mid-run** | `settings_snapshot` frozen at session creation time; resumed sessions use original config |
| **DB unavailable** | Session reuse still works in-memory (cache); persistence is best-effort. Wrap DB ops in try/except |
| **Process killed (-9)** | OS reclaims resources. Old sessions timeout server-side. Prune job cleans DB rows |
| **`run_id` not passed (legacy caller)** | Falls back to current create/disconnect-per-call behavior - fully backward compatible |
| **Same role called multiple times in one stage** | e.g., developer called in impl cycle 1, then cycle 2 -> same cached session reused, preserving context across cycles (desirable!) |
| **Empty `run_id` string vs `None`** | Treat both as "no session management" - only truthy non-empty strings activate the feature |

---

## Files Changed Summary

| File | Type of Change |
|---|---|
| `app/models/run_record.py` | Add `run_id` field |
| `app/engines/base.py` | Update protocol signature |
| `app/engines/copilot.py` | **Major rewrite**: session cache, resume, persistence |
| `app/engines/claude.py` | Signature-only: add `run_id=None` |
| `app/engines/codex.py` | Signature-only: add `run_id=None` |
| `app/engines/openai_compat.py` | Signature-only: add `run_id=None` |
| `app/engines/mock.py` | Signature-only: add `run_id=None` |
| `app/pipeline/runner.py` | Thread `run_id` through |
| `app/pipeline/orchestrated.py` | Thread `run_id` through, call `close_run()` |
| `app/pipeline/pm.py` | Passthrough `run_id` param |
| `app/db/sessions.py` | **New file**: `agent_sessions` table + CRUD |
| `tests/test_copilot_sessions.py` | **New file**: unit + integration tests |

---

## Testing Criteria

QA engineer should verify:

1. **Protocol conformance**: All 5 engines satisfy updated `AgentEngine` protocol (mypy or manual check)
2. **Backward compatibility**: Pipeline runs identically when `run_id` is not passed (no regressions)
3. **In-process reuse**: With `run_id` set, Copilot SDK `create_session` is called exactly **once** per unique `(run_id, role)` pair, even if the agent is called 10 times
4. **No premature disconnect**: `session.disconnect()` is NOT called between reused calls within the same run
5. **Clean shutdown**: All cached sessions are disconnected when `stop()` or `close_run()` is called
6. **Resume success**: After process restart, `resume_session()` is attempted with the stored `session_id`
7. **Resume fallback**: If `resume_session()` fails, a fresh session is created seamlessly (no crash, no data loss)
8. **DB consistency**: `agent_sessions` rows have correct status transitions (active -> closed/expired)
9. **Pruning**: Old closed sessions (>7 days) are removed from DB
10. **Concurrency**: Two simultaneous runs get different `run_id` values (no collision)
11. **Checkpoint independence**: Checkpoint resume works correctly regardless of session resume state
12. **Legacy path**: Setting `headless=True` (MockEngine) works identically with or without `run_id`
