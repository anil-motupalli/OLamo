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

Shells out to `gh pr list --json number,title,url,headRefName,author --state open` via `subprocess.run`. Cross-references each PR's `url` against all stored `RunRecord.pr_url` values to set an `olamo_created: bool` flag.

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

On failure (not in a git repo, `gh` not installed, no auth): returns `{ "prs": [], "repo": null, "error": "<message>" }`. Never raises a 5xx.

**`GET /api/prs/{number}/check`**

Shells out to `gh pr view {number} --json comments,reviews,statusCheckRollup`. Returns the raw JSON response. Used for the inline "Quick check" display — no run is created.

### "Full run" path

Reuses the existing `POST /api/runs` with `pr_url` set to the PR's URL. The pipeline already skips Stages 1–3 when `pr_url` is provided.

### Helper

A private `_run_gh(args: list[str]) -> dict` function wraps `subprocess.run(['gh'] + args, capture_output=True, text=True)`, parses stdout as JSON, and raises `RuntimeError` on non-zero exit or JSON parse failure. Used by both new endpoints.

---

## Section 2: Frontend

### Runs tab layout

The Runs tab gains a two-column layout:
- **Left column (~65%):** existing runs list + submit form (unchanged)
- **Right column (~35%):** PR sidebar panel

The sidebar is only rendered when the tab is active. On tab navigation to Runs, Alpine.js calls `loadPrs()`.

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
- **Full run ▶:** calls `POST /api/runs` with `{ pr_url: pr.url }`. The new run appears in the runs list immediately.
- **Refresh button:** re-fetches `GET /api/prs`

### Error / empty states

| Condition | Display |
|---|---|
| `repo: null` or `error` set | "GitHub repo not detected" one-liner, no list |
| `gh` not installed | "GitHub CLI not available" |
| No open PRs | "No open PRs" |
| Quick check fetch fails | Inline "Could not load — retry" link |

---

## Section 3: Error Handling

| Scenario | Behaviour |
|---|---|
| `gh` not on PATH | `_run_gh` raises `RuntimeError`; endpoint returns `error` field |
| Not in a git repo | `gh pr list` exits non-zero; `_run_gh` raises; endpoint returns `error` field |
| No GitHub auth | Same as above |
| `POST /api/runs` fails for full run | Existing error handling in submit flow |
| Quick check returns no comments/reviews | Display "No unresolved comments. CI: passing." |

---

## Section 4: Testing

- `test_get_prs_returns_list` — mock `subprocess.run` returning valid `gh` JSON; assert `olamo_created` flag set correctly for PRs matching stored `RunRecord.pr_url` values
- `test_get_prs_gh_not_installed` — mock `subprocess.run` raising `FileNotFoundError`; assert response has `error` field and empty `prs` list
- `test_get_prs_not_in_git_repo` — mock `subprocess.run` returning non-zero exit code; assert same
- `test_get_pr_check_returns_data` — mock `subprocess.run` returning valid `gh pr view` JSON; assert pass-through

---

## Out of Scope

- Filtering/searching PRs
- Showing closed or merged PRs
- Inline comment resolution from the sidebar (use Full run for that)
- GitHub App or OAuth authentication (relies on `gh` CLI ambient auth)
