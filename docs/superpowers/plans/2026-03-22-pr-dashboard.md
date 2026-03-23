# PR Dashboard Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a PR sidebar panel to the Runs/Dashboard tab showing all open GitHub PRs for the current repo, with Quick check and Full run actions, and a GitHub auth flow for unauthenticated users.

**Architecture:** Four new backend endpoints added to `main.py` inside `create_app()` (plus two module-level helper functions). The frontend adds a two-column layout to the Dashboard view with an Alpine.js-driven PR sidebar — state, data fetching, and helper functions live in `app()` in `static/index.html`.

**Tech Stack:** Python + FastAPI + subprocess (gh CLI), Alpine.js v3, Tailwind CSS

---

## Chunk 1: Backend — helpers, auth endpoints, PR list endpoint, check endpoint

### Task 1: Add `subprocess` import and module-level helpers

**Files:**
- Modify: `main.py:9` (imports block)
- Modify: `main.py:1408-1411` (before `create_app()`)
- Test: `test_main.py` (end of file, new `TestApiPrs` class)

- [ ] **Step 1: Write the failing tests for `_run_gh` behavior**

Add at the end of `test_main.py` (after line 1516):

```python
class TestApiPrs:
    def test_get_prs_gh_not_installed(self, client, monkeypatch):
        """Returns error field and empty prs list when gh binary not found."""
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError()
        monkeypatch.setattr("main.subprocess.run", fake_run)
        resp = client.get("/api/prs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["prs"] == []
        assert "error" in data
        assert data["error"]

    def test_get_prs_not_in_git_repo(self, client, monkeypatch):
        """Returns error field when gh exits non-zero (e.g. not in a git repo)."""
        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "not a git repository"
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["prs"] == []
        assert "error" in data
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest test_main.py::TestApiPrs::test_get_prs_gh_not_installed test_main.py::TestApiPrs::test_get_prs_not_in_git_repo -v
```

Expected: FAIL — `TestApiPrs` or `/api/prs` not found.

- [ ] **Step 3: Add `import subprocess` to `main.py` imports**

In `main.py`, the imports block currently starts at line 3. Add `import subprocess` in alphabetical order after `import re`:

```python
import re
import subprocess
import sys
```

- [ ] **Step 4: Add `_pr_number_from_url` and `_run_gh` before `create_app()`**

Insert before the comment block at line 1408 (`# -----------`):

```python
def _pr_number_from_url(url: str) -> int | None:
    """Extract PR number from a GitHub pull URL, e.g. '.../pull/42' -> 42."""
    m = re.search(r'/pull/(\d+)', url)
    return int(m.group(1)) if m else None


def _run_gh(args: list[str]) -> dict:
    """Run a gh CLI command and return parsed JSON output.

    Raises RuntimeError on non-zero exit, JSON parse failure, or gh not found.
    """
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("gh not installed")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh command failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh output not valid JSON: {exc}") from exc
```

- [ ] **Step 5: Run failing tests — expect pass once endpoint exists in Task 2**

```
pytest test_main.py::TestApiPrs -v
```

Note: tests will pass once the `/api/prs` endpoint is added in Task 3. Continue forward.

- [ ] **Step 6: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: add _run_gh helper and _pr_number_from_url for PR endpoints"
```

---

### Task 2: Add auth endpoints + tests

**Files:**
- Modify: `main.py` (inside `create_app()`, after `update_settings` at line 1554, before `spa_fallback` at line 1556)
- Test: `test_main.py` (add to `TestApiPrs`)

- [ ] **Step 1: Write failing tests for auth endpoints**

Add to `TestApiPrs` in `test_main.py`:

```python
    def test_get_prs_auth_authenticated(self, client, monkeypatch):
        """Returns authenticated:true with username when gh auth status exits 0."""
        class FakeResult:
            returncode = 0
            stdout = "anil"
            stderr = ""
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs/auth")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["user"] == "anil"

    def test_get_prs_auth_not_authenticated(self, client, monkeypatch):
        """Returns authenticated:false when gh auth status exits non-zero."""
        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "not logged in"
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs/auth")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["user"] is None
```

- [ ] **Step 2: Run to verify they fail**

```
pytest test_main.py::TestApiPrs::test_get_prs_auth_authenticated test_main.py::TestApiPrs::test_get_prs_auth_not_authenticated -v
```

Expected: FAIL — `/api/prs/auth` not found.

- [ ] **Step 3: Add auth endpoints inside `create_app()` in `main.py`**

Insert after `update_settings` (after line 1554, before line 1556 `@app.get("/{path:path}")`):

```python
    @app.get("/api/prs/auth")
    async def prs_auth_status() -> dict:
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return {"authenticated": False, "user": None}
            user_result = subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True,
                text=True,
            )
            user = user_result.stdout.strip() if user_result.returncode == 0 else None
            return {"authenticated": True, "user": user}
        except FileNotFoundError:
            return {"authenticated": False, "user": None}

    @app.post("/api/prs/auth/login")
    async def prs_auth_login() -> dict:
        try:
            subprocess.run(["gh", "--version"], capture_output=True, check=False)
        except FileNotFoundError:
            return {"status": "error", "error": "gh not installed"}

        async def _launch() -> None:
            proc = await asyncio.create_subprocess_exec(
                "gh", "auth", "login", "--web", "--git-protocol", "https",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

        asyncio.create_task(_launch())
        return {"status": "opening_browser"}
```

- [ ] **Step 4: Run tests — they should now pass**

```
pytest test_main.py::TestApiPrs::test_get_prs_auth_authenticated test_main.py::TestApiPrs::test_get_prs_auth_not_authenticated -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: add GET /api/prs/auth and POST /api/prs/auth/login endpoints"
```

---

### Task 3: Add `GET /api/prs` endpoint + test

**Files:**
- Modify: `main.py` (inside `create_app()`, after auth endpoints)
- Test: `test_main.py` (add to `TestApiPrs`)

- [ ] **Step 1: Write failing test**

Add to `TestApiPrs`:

```python
    def test_get_prs_returns_list(self, client, monkeypatch):
        """Lists PRs, sets olamo_created for runs with matching PR number, normalizes author."""
        import json

        gh_prs = json.dumps([
            {
                "number": 42,
                "title": "Add dark mode",
                "url": "https://github.com/owner/repo/pull/42",
                "headRefName": "feature/dark-mode",
                "author": {"login": "anil"},
            },
            {
                "number": 41,
                "title": "Fix nav bug",
                "url": "https://github.com/owner/repo/pull/41",
                "headRefName": "fix/nav",
                "author": {"login": "bob"},
            },
        ])
        gh_repo = json.dumps({"nameWithOwner": "owner/repo"})

        def fake_run(cmd, **kwargs):
            class R:
                returncode = 0
                stderr = ""
            r = R()
            r.stdout = gh_repo if "repo" in cmd else gh_prs
            return r

        monkeypatch.setattr("main.subprocess.run", fake_run)

        # Create a run with PR #42 so it becomes olamo_created
        client.post("/api/runs", json={
            "description": "fix PR",
            "pr_url": "https://github.com/owner/repo/pull/42",
        })

        resp = client.get("/api/prs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo"] == "owner/repo"
        assert len(data["prs"]) == 2

        pr42 = next(p for p in data["prs"] if p["number"] == 42)
        pr41 = next(p for p in data["prs"] if p["number"] == 41)
        assert pr42["olamo_created"] is True
        assert pr41["olamo_created"] is False
        assert pr42["author"] == "anil"   # normalized from {"login": "anil"}
        assert pr41["author"] == "bob"
```

- [ ] **Step 2: Run to verify it fails**

```
pytest test_main.py::TestApiPrs::test_get_prs_returns_list -v
```

Expected: FAIL — `/api/prs` not found.

- [ ] **Step 3: Add `GET /api/prs` inside `create_app()` in `main.py`**

Insert after the auth endpoints (before `@app.get("/{path:path}")`):

```python
    @app.get("/api/prs")
    async def list_prs() -> dict:
        try:
            raw = _run_gh(
                ["pr", "list", "--json", "number,title,url,headRefName,author", "--state", "open"]
            )
            prs_raw: list[dict] = raw if isinstance(raw, list) else []

            olamo_pr_numbers: set[int] = set()
            for run in manager.all_runs:
                if run.pr_url:
                    n = _pr_number_from_url(run.pr_url)
                    if n is not None:
                        olamo_pr_numbers.add(n)

            prs = []
            for pr in prs_raw:
                author = pr.get("author") or {}
                if isinstance(author, dict):
                    author = author.get("login", "")
                prs.append({
                    "number": pr["number"],
                    "title": pr["title"],
                    "url": pr["url"],
                    "headRefName": pr["headRefName"],
                    "author": author,
                    "olamo_created": pr["number"] in olamo_pr_numbers,
                })

            repo_info = _run_gh(["repo", "view", "--json", "nameWithOwner"])
            repo = repo_info.get("nameWithOwner")
            return {"prs": prs, "repo": repo}
        except RuntimeError as e:
            return {"prs": [], "repo": None, "error": str(e)}
```

- [ ] **Step 4: Run all `TestApiPrs` tests so far**

```
pytest test_main.py::TestApiPrs -v
```

Expected: All 5 tests PASS (the 2 from Task 1 + 2 from Task 2 + this one).

- [ ] **Step 5: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: add GET /api/prs endpoint with olamo_created detection"
```

---

### Task 4: Add `GET /api/prs/{number}/check` endpoint + test

**Files:**
- Modify: `main.py` (inside `create_app()`, after `list_prs`)
- Test: `test_main.py` (add to `TestApiPrs`)

- [ ] **Step 1: Write failing tests**

Add to `TestApiPrs`:

```python
    def test_get_pr_check_returns_data(self, client, monkeypatch):
        """Passes gh pr view JSON through to caller."""
        import json
        gh_data = {
            "comments": [],
            "reviews": [],
            "statusCheckRollup": [{"name": "CI", "conclusion": "SUCCESS"}],
        }

        class FakeResult:
            returncode = 0
            stderr = ""
        r = FakeResult()
        r.stdout = json.dumps(gh_data)
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: r)

        resp = client.get("/api/prs/42/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "statusCheckRollup" in data
        assert data["statusCheckRollup"][0]["conclusion"] == "SUCCESS"

    def test_get_pr_check_error(self, client, monkeypatch):
        """Returns error field with HTTP 200 when gh fails."""
        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "PR not found"
        monkeypatch.setattr("main.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs/99/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert data["error"]
```

- [ ] **Step 2: Run to verify they fail**

```
pytest test_main.py::TestApiPrs::test_get_pr_check_returns_data test_main.py::TestApiPrs::test_get_pr_check_error -v
```

Expected: FAIL — endpoint not found.

- [ ] **Step 3: Add the endpoint inside `create_app()` in `main.py`**

Insert after `list_prs` (before `@app.get("/{path:path}")`):

```python
    @app.get("/api/prs/{number}/check")
    async def check_pr(number: int) -> dict:
        try:
            return _run_gh(
                ["pr", "view", str(number), "--json", "comments,reviews,statusCheckRollup"]
            )
        except RuntimeError as e:
            return {"error": str(e)}
```

- [ ] **Step 4: Run all `TestApiPrs` tests**

```
pytest test_main.py::TestApiPrs -v
```

Expected: All 7 tests PASS.

- [ ] **Step 5: Run full test suite**

```
pytest test_main.py -x -q
```

Expected: All tests pass, no regressions.

- [ ] **Step 6: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: add GET /api/prs/{number}/check endpoint"
```

---

## Chunk 2: Frontend — PR sidebar panel

### Task 5: Two-column Dashboard layout + PR sidebar HTML

**Files:**
- Modify: `static/index.html:43-103` (dashboard section)

- [ ] **Step 1: Replace the dashboard section with a two-column layout**

In `static/index.html`, the dashboard `<div x-show="view === 'dashboard'">` block spans lines 43–103. Replace the entire block with the following (the existing runs table content is preserved inside the left column):

```html
    <!-- ── DASHBOARD ─────────────────────────────────────────── -->
    <div x-show="view === 'dashboard'">
      <div class="flex items-center justify-between mb-4">
        <h1 class="text-xl font-bold">Dashboard</h1>
        <button @click="loadRuns()" class="text-xs text-gray-400 hover:text-white">↻ Refresh</button>
      </div>

      <div class="flex gap-6 items-start">

        <!-- Left column: runs content -->
        <div class="flex-1 min-w-0">

          <!-- Active run banner -->
          <template x-if="activeRun && activeRun.status === 'running'">
            <div class="mb-4 bg-indigo-900/40 border border-indigo-700 rounded-lg p-4">
              <div class="flex items-center gap-3">
                <div class="w-3 h-3 rounded-full bg-indigo-400 animate-pulse"></div>
                <div class="flex-1">
                  <p class="text-sm font-semibold text-indigo-300">Run in progress</p>
                  <p class="text-xs text-gray-400 mt-0.5" x-text="activeRun.description"></p>
                </div>
                <span class="text-xs text-indigo-400" x-text="currentStage"></span>
              </div>
              <div class="mt-3 bg-gray-900 rounded p-2 h-32 overflow-y-auto scrollbar-thin text-xs space-y-0.5" id="activityLog">
                <template x-for="(evt, i) in liveEvents" :key="i">
                  <div :class="eventColor(evt.type)" x-text="formatEvent(evt)"></div>
                </template>
              </div>
            </div>
          </template>

          <!-- Runs table -->
          <div class="bg-gray-900 rounded-lg border border-gray-800 overflow-hidden">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b border-gray-800 text-gray-400 text-xs uppercase tracking-wide">
                  <th class="text-left px-4 py-3">Status</th>
                  <th class="text-left px-4 py-3">Description</th>
                  <th class="text-left px-4 py-3 hidden md:table-cell">Queued</th>
                  <th class="text-left px-4 py-3 hidden md:table-cell">Completed</th>
                  <th class="px-4 py-3"></th>
                </tr>
              </thead>
              <tbody>
                <template x-if="runs.length === 0">
                  <tr>
                    <td colspan="5" class="text-center py-12 text-gray-600">No runs yet. Submit a task to get started.</td>
                  </tr>
                </template>
                <template x-for="run in runs" :key="run.id">
                  <tr class="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors">
                    <td class="px-4 py-3">
                      <span :class="statusBadge(run.status)" class="px-2 py-0.5 rounded text-xs font-semibold" x-text="run.status"></span>
                    </td>
                    <td class="px-4 py-3 text-gray-200 max-w-xs truncate" x-text="run.description"></td>
                    <td class="px-4 py-3 text-gray-500 text-xs hidden md:table-cell" x-text="fmtDate(run.queued_at)"></td>
                    <td class="px-4 py-3 text-gray-500 text-xs hidden md:table-cell" x-text="run.completed_at ? fmtDate(run.completed_at) : '—'"></td>
                    <td class="px-4 py-3">
                      <button @click="openRun(run.id)" class="text-xs text-indigo-400 hover:text-indigo-300">Detail →</button>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>

        </div><!-- end left column -->

        <!-- Right column: PR sidebar -->
        <div class="w-80 flex-shrink-0">
          <div class="bg-gray-900 rounded-lg border border-gray-800 p-4">

            <div class="flex items-center justify-between mb-3">
              <h2 class="text-sm font-semibold text-gray-200">
                Open PRs<span x-show="prs.length > 0" class="text-gray-500 ml-1" x-text="'(' + prs.length + ')'"></span>
              </h2>
              <button @click="loadPrs()" class="text-xs text-gray-400 hover:text-white" title="Refresh">↻</button>
            </div>

            <!-- Loading -->
            <p x-show="prsLoading" class="text-xs text-gray-500">Loading…</p>

            <!-- Not authenticated -->
            <template x-if="!prsLoading && prsAuthNeeded">
              <div class="space-y-2">
                <p class="text-xs text-gray-400">Not logged in to GitHub</p>
                <button @click="loginGitHub()"
                  class="text-xs bg-gray-700 hover:bg-gray-600 text-white px-3 py-1.5 rounded w-full text-left">
                  Login with GitHub
                </button>
                <p x-show="prsLoginPending" class="text-xs text-gray-500">Opening browser…</p>
              </div>
            </template>

            <!-- gh not installed -->
            <template x-if="!prsLoading && !prsAuthNeeded && prsError && prsError.includes('not installed')">
              <p class="text-xs text-gray-400">GitHub CLI not available — install from cli.github.com</p>
            </template>

            <!-- Non-auth error -->
            <template x-if="!prsLoading && !prsAuthNeeded && prsError && !prsError.includes('not installed')">
              <p class="text-xs text-gray-400">GitHub repo not detected</p>
            </template>

            <!-- No open PRs -->
            <template x-if="!prsLoading && !prsError && !prsAuthNeeded && prs.length === 0">
              <p class="text-xs text-gray-500">No open PRs</p>
            </template>

            <!-- PR list -->
            <template x-if="!prsLoading && !prsError && !prsAuthNeeded && prs.length > 0">
              <div class="space-y-3">
                <template x-for="pr in prs" :key="pr.number">
                  <div class="border border-gray-800 rounded p-3">
                    <div class="flex items-start gap-2 mb-2">
                      <span class="text-xs text-gray-200 font-medium flex-1 leading-snug"
                        x-text="'#' + pr.number + ' ' + pr.title"></span>
                      <span x-show="pr.olamo_created"
                        class="text-xs bg-green-900 text-green-300 px-1.5 py-0.5 rounded flex-shrink-0">OLamo</span>
                    </div>
                    <div class="flex gap-2">
                      <button @click="quickCheckPr(pr.number)"
                        class="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-2 py-1 rounded">
                        Quick check
                      </button>
                      <button @click="fullRunPr(pr)"
                        :disabled="activeRun !== null"
                        :title="activeRun !== null ? 'A run is already in progress' : ''"
                        class="text-xs bg-indigo-700 hover:bg-indigo-600 disabled:opacity-40 disabled:cursor-not-allowed text-white px-2 py-1 rounded">
                        Full run ▶
                      </button>
                    </div>
                    <!-- Quick check result -->
                    <template x-if="prCheckResults[pr.number]">
                      <div class="mt-2 text-xs text-gray-400 border-t border-gray-800 pt-2 space-y-0.5">
                        <template x-if="prCheckResults[pr.number].error">
                          <span class="text-red-400">
                            Could not load —
                            <a @click="quickCheckPr(pr.number)" class="underline cursor-pointer">retry</a>
                          </span>
                        </template>
                        <template x-if="!prCheckResults[pr.number].error">
                          <div>
                            <p x-text="prCiStatus(pr.number)"></p>
                            <p x-text="'Comments: ' + prUnresolvedCount(pr.number)"></p>
                          </div>
                        </template>
                      </div>
                    </template>
                  </div>
                </template>
              </div>
            </template>

          </div>
        </div><!-- end right column -->

      </div><!-- end flex row -->
    </div><!-- end dashboard -->
```

- [ ] **Step 2: Verify the page loads without JS errors**

Start the server (`python main.py web`) and open `http://localhost:8000`. The Dashboard tab should show the two-column layout. The PR sidebar shows "Loading…" briefly.

- [ ] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "feat: add two-column dashboard layout with PR sidebar HTML"
```

---

### Task 6: Alpine.js state + PR functions + onViewChange

**Files:**
- Modify: `static/index.html` — `app()` state block and JS function sections

- [ ] **Step 1: Add PR sidebar state to `app()` return object**

In `static/index.html`, find the `// Stages reached` comment and the `_stagesReached` line (~line 519). Add the PR state **after** `_stagesReached` and **before** `async init()`:

```javascript
    // PR sidebar
    prs: [],
    prsLoading: false,
    prsError: '',
    prsAuthNeeded: false,
    prsLoginPending: false,
    _prsPollInterval: null,
    prCheckResults: {},
```

- [ ] **Step 2: Update `onViewChange` to call `loadPrs()` on the dashboard tab**

Find `onViewChange` (~line 526):

```javascript
    onViewChange(id) {
      if (id === 'team') this.loadTeam();
      if (id === 'settings') this.loadSettings();
    },
```

Replace with:

```javascript
    onViewChange(id) {
      if (id === 'dashboard') this.loadPrs();
      if (id === 'team') this.loadTeam();
      if (id === 'settings') this.loadSettings();
    },
```

- [ ] **Step 3: Update `init()` to call `loadPrs()` on startup**

Find `init()` (~line 521):

```javascript
    async init() {
      await this.loadRuns();
      this.connectSSE();
    },
```

Replace with:

```javascript
    async init() {
      await this.loadRuns();
      this.connectSSE();
      this.loadPrs();
    },
```

- [ ] **Step 4: Add PR sidebar functions before `// ── Agent config helpers`**

Find the `// ── Agent config helpers` comment (~line 672). Insert the following block immediately before it:

```javascript
    // ── PR sidebar ───────────────────────────────────────────
    async loadPrs() {
      this.prsLoading = true;
      this.prsError = '';
      this.prsAuthNeeded = false;
      try {
        const res = await fetch('/api/prs');
        const data = await res.json();
        if (data.error) {
          const authRes = await fetch('/api/prs/auth');
          const authData = await authRes.json();
          if (!authData.authenticated) {
            this.prsAuthNeeded = true;
          } else {
            this.prsError = data.error;
          }
        } else {
          this.prs = data.prs;
        }
      } catch {
        this.prsError = 'Failed to load PRs';
      } finally {
        this.prsLoading = false;
      }
    },

    async loginGitHub() {
      this.prsLoginPending = true;
      try {
        const res = await fetch('/api/prs/auth/login', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'error') {
          this.prsError = data.error || 'gh not installed';
          this.prsLoginPending = false;
          return;
        }
        // Poll every 2 s until authenticated, then reload PRs
        this._prsPollInterval = setInterval(async () => {
          const authRes = await fetch('/api/prs/auth');
          const authData = await authRes.json();
          if (authData.authenticated) {
            clearInterval(this._prsPollInterval);
            this._prsPollInterval = null;
            this.prsLoginPending = false;
            this.prsAuthNeeded = false;
            await this.loadPrs();
          }
        }, 2000);
      } catch {
        this.prsLoginPending = false;
        this.prsError = 'Login failed';
      }
    },

    async quickCheckPr(number) {
      // Second click collapses
      if (this.prCheckResults[number]) {
        this.prCheckResults = { ...this.prCheckResults, [number]: null };
        return;
      }
      try {
        const res = await fetch(`/api/prs/${number}/check`);
        const data = await res.json();
        this.prCheckResults = { ...this.prCheckResults, [number]: data };
      } catch {
        this.prCheckResults = { ...this.prCheckResults, [number]: { error: 'Network error' } };
      }
    },

    async fullRunPr(pr) {
      if (this.activeRun) return;
      try {
        const res = await fetch('/api/runs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            pr_url: pr.url,
            description: `PR #${pr.number}: ${pr.title}`,
          }),
        });
        if (res.ok) {
          await this.loadRuns();
        }
      } catch {}
    },

    prCiStatus(number) {
      const checks = (this.prCheckResults[number] || {}).statusCheckRollup;
      if (!checks || checks.length === 0) return 'CI: no checks configured';
      if (checks.some(c => c.conclusion === 'FAILURE')) {
        const names = checks.filter(c => c.conclusion === 'FAILURE').map(c => c.name).join(', ');
        return `CI: failing (${names})`;
      }
      return 'CI: passing';
    },

    prUnresolvedCount(number) {
      const data = this.prCheckResults[number] || {};
      return (data.comments || []).filter(c => !c.isResolved).length;
    },
```

- [ ] **Step 5: End-to-end manual verification**

1. Start the server in a git repo with `gh` authenticated
2. Open `http://localhost:8000` — PR list appears in the sidebar on the right
3. Click "Quick check" on a PR — inline result appears; click again — collapses
4. Click "↻" — refreshes
5. Navigate to Team tab and back — `loadPrs()` fires again
6. In a non-gh-authed environment: sidebar shows "Not logged in to GitHub" + Login button

- [ ] **Step 6: Run full test suite for regressions**

```
pytest test_main.py -x -q
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add static/index.html
git commit -m "feat: add PR sidebar Alpine.js state and functions (loadPrs, quickCheckPr, fullRunPr, auth flow)"
```
