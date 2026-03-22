# PR Dashboard Design

**Date:** 2026-03-22
**Status:** Approved
**Scope:** Web UI + backend — Runs tab PR sidebar

---

## Overview

Add a PR sidebar panel to the Runs tab that shows all open PRs for the current repository. PRs created by OLamo are marked with a badge. Each PR has two action buttons: a "Quick check" for an inline comment/CI summary, and a "Full run" that kicks off a Stage 4 pipeline run (comment polling → fix loop → CI checks).

---

## Section 1: Backend

### New endpoints

**`GET /api/prs`**

Shells out to `gh pr list --json number,title,url,headRefName,author --state open` via `subprocess.run`. The `author` field returned by `gh` is an object (`{"login": "..."}`) — normalize it to a plain string: `pr["author"]["login"]`.

For each PR, extract the PR number from its URL and cross-reference against PR numbers extracted from all stored `RunRecord.pr_url` values to set an `olamo_created: bool` flag. Compare by PR number (not raw URL string) to avoid fragility from URL format variations (trailing slashes, case differences, etc.).

```python
def _pr_number_from_url(url: str) -> int | None:
    # e.g. "https://github.com/owner/repo/pull/42" → 42
    m = re.search(r'/pull/(\d+)', url)
    return int(m.group(1)) if m else None
```

Response shape:
```json
{
  "prs": [
    {
      "number": 42,
      "title": "Add dark mode",
      "url": "https://github.com/owner/repo/pull/42",
      "headRefName": "feature/dark-mode",
      "author": "anil",
      "olamo_created": true
    }
  ],
  "repo": "owner/repo"
}
```

On failure: returns `{ "prs": [], "repo": null, "error": "<message>" }`. Never raises a 5xx.

**`GET /api/prs/{number}/check`**

Shells out to `gh pr view {number} --json comments,reviews,statusCheckRollup`. Returns the raw JSON response. Used for the inline "Quick check" display — no run is created.

On failure: returns `{ "error": "<message>" }` with HTTP 200 (so the frontend can render an inline retry state without special status-code handling).

### "Full run" path

Reuses the existing `POST /api/runs` with both `pr_url` and `description` set. The pipeline skips Stages 1, 2, and 3 and begins at Stage 3b (CI check polling), then Stage 4 (PR poll loop). The frontend sends:

```json
{ "pr_url": "<pr.url>", "description": "PR #<number>: <title>" }
```

### GitHub auth endpoints

**`GET /api/prs/auth`**

Runs `gh auth status` via `subprocess.run`. Returns `{ "authenticated": true, "user": "<login>" }` on success, or `{ "authenticated": false, "user": null }` if not logged in or `gh` not installed. Never raises a 5xx.

**`POST /api/prs/auth/login`**

Launches `gh auth login --web --git-protocol https` as a non-blocking background process (`asyncio.create_task` wrapping `asyncio.create_subprocess_exec`). Returns immediately with `{ "status": "opening_browser" }`. The browser opens to GitHub's OAuth page. The frontend polls `GET /api/prs/auth` every 2 seconds until `authenticated: true`, then calls `loadPrs()` to populate the sidebar automatically.

If `gh` is not on PATH, returns `{ "status": "error", "error": "gh not installed" }`.

### Helper

A private `_run_gh(args: list[str]) -> dict` function wraps `subprocess.run(['gh'] + args, capture_output=True, text=True)`, parses stdout as JSON, and raises `RuntimeError` on non-zero exit or JSON parse failure. Also catches `FileNotFoundError` (raised when `gh` is not on PATH) and re-raises as `RuntimeError("gh not installed")`. Used by the PR list and check endpoints (not the auth endpoints, which handle their own subprocess calls).

---

## Section 2: Frontend

### Runs tab layout

The existing dashboard/Runs tab (`view === 'dashboard'`) gains a two-column layout:
- **Left column (~65%):** existing runs list + submit form (unchanged)
- **Right column (~35%):** PR sidebar panel

The sidebar is rendered only when the dashboard tab is active. `onViewChange` gains a `'dashboard'` branch that calls `loadPrs()` (alongside the existing `loadTeam` / `loadSettings` branches).

### PR sidebar panel

```
Open PRs (3)          [↻ refresh]
─────────────────────────────────
● #42 Add dark mode  [OLamo]
  [Quick check]  [Full run ▶]

● #41 Fix nav bug
  [Quick check]  [Full run ▶]
```

- Each PR row shows: number, title, `[OLamo]` badge (green) if `olamo_created`
- **Quick check:** calls `GET /api/prs/{number}/check`, displays an inline summary beneath the row (unresolved comment count, CI status). Collapses on second click.
- **Full run ▶:** disabled when `activeRun` is not null (a run is already in progress), consistent with the existing submit form behavior. When enabled, calls `POST /api/runs` with `{ pr_url, description }`. The new run appears in the runs list.
- **Refresh button:** re-fetches `GET /api/prs`

### Quick check CI display

`statusCheckRollup` from `gh pr view` can be `null` or an empty array when no CI is configured. Handle all cases:

| `statusCheckRollup` value | Display |
|---|---|
| Array with all `"SUCCESS"` | "CI: passing" |
| Array with any `"FAILURE"` | "CI: failing" + list of failing check names |
| `null` or empty array | "CI: no checks configured" |

### Auth flow in sidebar

When `GET /api/prs` returns an auth-related error (detected by `authenticated: false` from `GET /api/prs/auth` or an error message containing "authentication"), the sidebar shows:

```
Not logged in to GitHub
[Login with GitHub]
```

Clicking "Login with GitHub":
1. Calls `POST /api/prs/auth/login` — browser opens to GitHub OAuth
2. Sidebar shows "Opening browser…" with a spinner
3. Frontend polls `GET /api/prs/auth` every 2 seconds
4. When `authenticated: true` is returned, calls `loadPrs()` and restores the normal PR list view

### Error / empty states

| Condition | Display |
|---|---|
| `repo: null` or non-auth error | "GitHub repo not detected" one-liner, no list |
| `gh` not installed | "GitHub CLI not available — install from cli.github.com" |
| Not authenticated | "Not logged in to GitHub" + "Login with GitHub" button |
| No open PRs | "No open PRs" |
| Quick check returns `error` field | Inline "Could not load — retry" link |
| `activeRun` not null | "Full run" button disabled with tooltip "A run is already in progress" |

---

## Section 3: Error Handling

| Scenario | Behaviour |
|---|---|
| `gh` not on PATH | `_run_gh` catches `FileNotFoundError`, raises `RuntimeError`; endpoint returns `error` field |
| Not in a git repo | `gh pr list` exits non-zero; `_run_gh` raises `RuntimeError`; endpoint returns `error` field |
| No GitHub auth | `GET /api/prs/auth` returns `authenticated: false`; sidebar shows "Login with GitHub" button |
| `POST /api/prs/auth/login` called, `gh` missing | Returns `{ "status": "error" }`; sidebar shows install instructions |
| `POST /api/runs` fails for full run | Existing error handling in submit flow |
| Quick check `statusCheckRollup` is null | Display "CI: no checks configured" |

---

## Section 4: Testing

- `test_get_prs_returns_list` — mock `subprocess.run` returning valid `gh` JSON with two PRs; assert `olamo_created: true` for the PR whose number matches a stored `RunRecord.pr_url`, `olamo_created: false` for the other; assert `author` is normalized to a plain string
- `test_get_prs_gh_not_installed` — mock `subprocess.run` raising `FileNotFoundError`; assert response has `error` field and empty `prs` list
- `test_get_prs_not_in_git_repo` — mock `subprocess.run` returning non-zero exit code; assert same
- `test_get_pr_check_returns_data` — mock `subprocess.run` returning valid `gh pr view` JSON; assert pass-through
- `test_get_pr_check_error` — mock `subprocess.run` raising `RuntimeError`; assert response has `error` field
- `test_get_prs_auth_authenticated` — mock `gh auth status` returning exit code 0; assert `{ "authenticated": true }` with username
- `test_get_prs_auth_not_authenticated` — mock `gh auth status` returning non-zero exit; assert `{ "authenticated": false }`

---

## Out of Scope

- Filtering/searching PRs
- Showing closed or merged PRs
- Inline comment resolution from the sidebar (use Full run for that)
- GitHub App or OAuth authentication beyond `gh auth login --web` (relies on `gh` CLI)
