# Pipeline

## Two modes

`orchestration_mode` in settings selects the mode:

- **`orchestrated`** (default for new work) — `app/pipeline/orchestrated.py`. Python-driven deterministic loops. Supports mixed engines per agent. All active development happens here.
- **`pm`** — `app/pipeline/pm.py`. Uses Claude's Task tool as the orchestrator. Claude-only. Kept for compatibility.

## Orchestrated pipeline stages

### Stage 1: Design loop

1. `lead-developer` receives the raw task → produces a full implementation plan
2. Loop up to `max_design_cycles` (default 5):
   - `qa-engineer` reviews the plan (`REVIEW DESIGN`)
   - If `APPROVED` → break
   - Otherwise `lead-developer` refines the plan, addressing each finding explicitly with `ADDRESSED` or `PUSHBACK` per finding. The revised plan + response is passed back to QA next cycle so QA can reconsider pushed-back items
3. Human approval gate (web mode only) — suspends pipeline via `ApprovalGate` (`app/pipeline/approval_gate.py`), sends `awaiting_approval` SSE. Human can approve or request revision. On revision: lead-developer refines → QA re-runs → gate re-presents
4. Checkpoint saved with `completed_stage: 1`

### Stage 2: Implementation loop

Loop up to `max_impl_cycles` (default 5):

1. `developer` receives the plan (+ any prior review findings) — implements changes, responds per-finding with `FIXED` or `PUSHBACK`
2. Build loop (up to `max_build_cycles`, default 3): `build-agent` builds and tests; on failure `developer` fixes
3. Parallel review: `code-reviewer`, `qa-engineer`, `lead-developer` review concurrently
   - Developer's per-finding response from step 1 is included as context so reviewers can accept reasonable pushbacks
   - Reviewers that return `APPROVED` are added to `already_approved` and skipped in subsequent cycles unless a `CRITICAL` finding appears
4. If any reviewer returns `NEEDS IMPROVEMENT` → next cycle with those findings
5. Checkpoint saved after each cycle

### Stage 3: Commit & PR

`repo-manager` commits, pushes a feature branch (`feature/<slug>`), opens a PR.

### Stage 3b: CI check polling

Up to `max_pr_cycles` (default 3): `repo-manager` polls CI; `developer` fixes failures.

### Stage 4: PR poll loop

Up to `max_pr_cycles`: `repo-manager` polls PR comments; `developer` addresses them; one final review pass; push.

## Checkpointing & resumability

`save_checkpoint` is called after each stage with a dict containing `completed_stage`, `plan`, `last_diff`, `pr_result`, `addressed_ids`, `already_approved`. On resume, the pipeline reads `checkpoint` and skips completed stages. Checkpoint data is stored in the `run_state` table.

## Approval gate

`ApprovalGate` (`app/pipeline/approval_gate.py`) is an `asyncio.Future`-based gate. The pipeline `await`s `gate.wait(plan)`, which suspends the coroutine. The web layer calls `gate.resolve(approved, feedback, comments)` when the human responds. Inline comments from the UI are passed as `[{selectedText, commentText}]` and formatted into the lead-developer prompt.

## Headless / dry-run mode

`--headless` flag or `settings.headless = True`: all agents route to `MockEngine`, no real API calls. Useful for testing pipeline logic and frontend.
