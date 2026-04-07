Plan has been **refined and saved** to `agents/prompts/lead-developer/plan.md`. Here's what changed per QA finding:

### Critical Fixes (3 🔴)

| # | Finding | Resolution |
|---|---------|------------|
| 1 | Protocol signature unspecified | `run_id: str \| None = None` added to `AgentEngine` protocol. All 5 engines updated — 4 ignore it, CopilotEngine uses it. |
| 2 | In-process reuse vs cross-restart resume conflated | **Split into two independent sub-features**: (A) In-Process Session Reuse (caching, no DB) and (B) Cross-Restart Resume (DB lookup + `resume_session()`). Separate logic paths, separate phases. |
| 3 | `agent_sessions` schema underspecified | Full DDL with exact columns, types, `UNIQUE(run_id, role)` constraint, index on `(run_id, role)`, `settings_snapshot` = ModelConfig-only JSON, 7-day TTL cleanup via `_prune_old_sessions()`. |

### Significant Fixes (3 🟡)

| # | Finding | Resolution |
|---|---------|------------|
| 4 | Checkpoint interaction unaddressed | Explicitly defined as **complementary and independent** — checkpoint tracks stage progress, sessions track agent conversation continuity. No schema changes needed. Each `(run_id, role)` looked up independently. |
| 5 | `close_run()` timing unclear | Defined call sites for all scenarios: normal completion → `finally` block; failure → same `finally`; crash → OS reclaims; in-process reuse → NOT called. Behavior table included. |
| 6 | Engine protocol impact understated | Called out explicitly: all 5 files listed with change type. Structural subtyping means optional kwarg doesn't break conformance. |

### Minor Fixes (3 🟢)

| # | Finding | Resolution |
|---|---------|------------|
| 7 | `run_id` vs `id` coexistence | UUID `id` stays as PK for all lookups. `run_id` is display/naming only. Mapping via `SELECT id WHERE run_id=?`. |
| 8 | Auto-increment race condition | `asyncio.Lock`-protected generation with comment noting SQL migration path for future multi-process needs. |
| 9 | No testing strategy | Phase 4 added with 5 test categories: DB ops, run ID gen, session reuse (mock SDK), resume flow (mock SDK), integration pipeline. |

**18 implementation steps across 4 phases**, tracked in SQL todos. Ready when you give the go-ahead.