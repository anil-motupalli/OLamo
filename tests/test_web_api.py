"""Tests for FastAPI web endpoints and orchestration mode."""

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import (
    AgentEngineConfig,
    AppSettings,
    MAX_BUILD_CYCLES,
    MAX_DESIGN_CYCLES,
    MAX_IMPL_CYCLES,
    MAX_PR_CYCLES,
    ModelConfig,
    OPUS_MODEL,
    PM_MAIN_MODEL,
    SONNET_MODEL,
)
from app.pipeline.orchestrated import run_pipeline_orchestrated

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

from starlette.testclient import TestClient  # noqa: E402
from app.web.app import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<html>OLamo</html>")
    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


class TestOrchestrationMode:
    def test_default_is_pm(self):
        assert AppSettings().orchestration_mode == "pm"

    def test_can_set_orchestrated(self):
        s = AppSettings(orchestration_mode="orchestrated")
        assert s.orchestration_mode == "orchestrated"

    def test_settings_endpoint_includes_mode(self):
        d = asdict(AppSettings())
        assert "orchestration_mode" in d


class TestApiSettings:
    def test_get_settings_returns_defaults(self, client):
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data
        assert "is_locked" in data
        assert data["is_locked"] is False
        assert data["config"]["pm_model"] == PM_MAIN_MODEL

    def test_put_settings_updates_config(self, client):
        resp = client.put("/api/settings", json={"pm_model": "opus", "max_pr_cycles": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is True
        assert data["config"]["pm_model"] == "opus"
        assert data["config"]["max_pr_cycles"] == 5

    def test_put_settings_ignores_unknown_keys(self, client):
        resp = client.put("/api/settings", json={"unknown_field": "value", "pm_model": "haiku"})
        assert resp.status_code == 200
        assert resp.json()["config"]["pm_model"] == "haiku"

    def test_get_settings_includes_agent_configs(self, client):
        assert "agent_configs" in client.get("/api/settings").json()["config"]

    def test_get_settings_includes_copilot_github_token(self, client):
        assert "copilot_github_token" in client.get("/api/settings").json()["config"]

    def test_put_settings_accepts_agent_configs(self, client):
        payload = {"agent_configs": {"developer": {
            "engine": "copilot",
            "model_config": {"mode": "simple", "model": "gpt-5",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {}},
            "mcp_servers": {}
        }}}
        resp = client.put("/api/settings", json=payload)
        assert resp.status_code == 200
        assert resp.json()["config"]["agent_configs"]["developer"]["engine"] == "copilot"

    def test_put_settings_advanced_missing_base_url_returns_200(self, client):
        payload = {"agent_configs": {"developer": {
            "engine": "claude",
            "model_config": {"mode": "advanced", "model": "gpt-4",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {}},
            "mcp_servers": {}
        }}}
        resp = client.put("/api/settings", json=payload)
        assert resp.status_code == 200


class TestApiRuns:
    def test_list_runs_initially_empty(self, client):
        resp = client.get("/api/runs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_run_returns_201(self, client):
        resp = client.post("/api/runs", json={"description": "build a feature"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["description"] == "build a feature"
        assert data["status"] == "queued"
        assert "id" in data

    def test_create_run_appears_in_list(self, client):
        client.post("/api/runs", json={"description": "listed task"})
        runs = client.get("/api/runs").json()
        assert len(runs) == 1
        assert runs[0]["description"] == "listed task"

    def test_create_run_missing_description_returns_400(self, client):
        resp = client.post("/api/runs", json={})
        assert resp.status_code == 400

    def test_get_run_by_id(self, client):
        created = client.post("/api/runs", json={"description": "get by id"}).json()
        resp = client.get(f"/api/runs/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]

    def test_get_run_unknown_id_returns_404(self, client):
        resp = client.get("/api/runs/nonexistent-id")
        assert resp.status_code == 404

    def test_run_events_includes_queued_event(self, client):
        created = client.post("/api/runs", json={"description": "no events yet"}).json()
        resp = client.get(f"/api/runs/{created['id']}/events")
        assert resp.status_code == 200
        events = resp.json()
        # A run_queued event is emitted when the run is enqueued
        assert any(e["type"] == "run_queued" for e in events)

    def test_run_events_unknown_run_returns_404(self, client):
        resp = client.get("/api/runs/bad-id/events")
        assert resp.status_code == 404

    def test_create_run_with_pr_url(self, client):
        resp = client.post("/api/runs", json={"description": "fix PR", "pr_url": "https://github.com/org/repo/pull/5"})
        assert resp.status_code == 201
        assert resp.json()["pr_url"] == "https://github.com/org/repo/pull/5"

    def test_create_run_with_settings_override(self, client):
        resp = client.post("/api/runs", json={"description": "fast run", "settings_override": {"max_impl_cycles": 1}})
        assert resp.status_code == 201
        assert resp.json()["settings_override"] == {"max_impl_cycles": 1}

    def test_create_run_without_pr_url_defaults_to_empty(self, client):
        resp = client.post("/api/runs", json={"description": "normal run"})
        assert resp.status_code == 201
        assert resp.json()["pr_url"] == ""

    def test_create_run_with_agent_configs_override(self, client):
        """settings_override.agent_configs is stored in the run record."""
        payload = {
            "description": "custom agent run",
            "settings_override": {
                "agent_configs": {
                    "developer": {
                        "engine": "copilot",
                        "model_config": {
                            "mode": "simple",
                            "model": "gpt-5",
                            "provider_type": "openai",
                            "base_url": "",
                            "api_key": "",
                            "extra_params": {},
                        },
                        "mcp_servers": {},
                    }
                }
            },
        }
        resp = client.post("/api/runs", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["settings_override"]["agent_configs"]["developer"]["engine"] == "copilot"

    def test_existing_scalar_overrides_still_work_with_agent_configs_excluded(self, client):
        """max_design_cycles override still applies when agent_configs is also present."""
        payload = {
            "description": "scalar + agent override",
            "settings_override": {
                "max_design_cycles": 7,
                "agent_configs": {
                    "developer": {
                        "engine": "copilot",
                        "model_config": {
                            "mode": "simple",
                            "model": "gpt-5",
                            "provider_type": "openai",
                            "base_url": "",
                            "api_key": "",
                            "extra_params": {},
                        },
                        "mcp_servers": {},
                    }
                },
            },
        }
        resp = client.post("/api/runs", json=payload)
        assert resp.status_code == 201
        assert resp.json()["settings_override"]["max_design_cycles"] == 7


class TestApiApproval:
    def test_get_approval_returns_404_for_unknown_run(self, client):
        resp = client.get("/api/runs/no-such-id/approval")
        assert resp.status_code == 404

    def test_get_approval_not_waiting_for_queued_run(self, client):
        created = client.post("/api/runs", json={"description": "task"}).json()
        resp = client.get(f"/api/runs/{created['id']}/approval")
        assert resp.status_code == 200
        assert resp.json()["waiting"] is False
        assert resp.json()["plan"] == ""

    def test_post_approval_returns_409_when_not_waiting(self, client):
        created = client.post("/api/runs", json={"description": "task"}).json()
        resp = client.post(f"/api/runs/{created['id']}/approval", json={"approved": True})
        assert resp.status_code == 409

    def test_post_approval_returns_404_for_unknown_run(self, client):
        resp = client.post("/api/runs/no-such-id/approval", json={"approved": True})
        assert resp.status_code == 404


class TestApiTeam:
    def test_team_returns_all_six_agents(self, client):
        resp = client.get("/api/team")
        assert resp.status_code == 200
        data = resp.json()
        roles = {a["role"] for a in data["agents"]}
        expected = {"lead-developer", "developer", "code-reviewer", "qa-engineer", "build-agent", "repo-manager"}
        assert roles == expected

    def test_team_returns_pipeline_stages(self, client):
        data = client.get("/api/team").json()
        assert len(data["pipeline"]) == 4

    def test_team_returns_cycle_limits(self, client):
        data = client.get("/api/team").json()
        limits = data["cycle_limits"]
        assert limits["max_design_cycles"] == MAX_DESIGN_CYCLES
        assert limits["max_build_cycles"] == MAX_BUILD_CYCLES
        assert limits["max_impl_cycles"] == MAX_IMPL_CYCLES
        assert limits["max_pr_cycles"] == MAX_PR_CYCLES

    def test_each_agent_has_model_and_description(self, client):
        agents = client.get("/api/team").json()["agents"]
        for agent in agents:
            assert agent["model"], f"{agent['role']} missing model"
            assert agent["description"], f"{agent['role']} missing description"

    def test_each_agent_has_engine_field(self, client):
        for agent in client.get("/api/team").json()["agents"]:
            assert agent["engine"] in ("claude", "copilot"), f"{agent['role']} bad engine"

    def test_each_agent_has_config_mode_field(self, client):
        for agent in client.get("/api/team").json()["agents"]:
            assert agent["config_mode"] in ("simple", "advanced"), f"{agent['role']} bad config_mode"

    def test_default_engines_match_smart_defaults(self, client):
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        assert agents["lead-developer"]["engine"] == "claude"
        assert agents["developer"]["engine"] == "claude"
        assert agents["code-reviewer"]["engine"] == "copilot"
        assert agents["qa-engineer"]["engine"] == "copilot"
        assert agents["build-agent"]["engine"] == "copilot"
        assert agents["repo-manager"]["engine"] == "copilot"

    def test_copilot_default_agents_report_copilot_model(self, client):
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        assert agents["code-reviewer"]["model"] == "codex"
        assert agents["qa-engineer"]["model"] == "gpt-5.4"
        assert agents["build-agent"]["model"] == "gpt-5-mini"
        assert agents["repo-manager"]["model"] == "gpt-5-mini"

    def test_claude_default_agents_report_claude_tier_model(self, client):
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        assert agents["lead-developer"]["model"] == OPUS_MODEL
        assert agents["developer"]["model"] == SONNET_MODEL

    def test_agent_config_override_reflected_in_team(self, client):
        client.put("/api/settings", json={"agent_configs": {"developer": {
            "engine": "copilot",
            "model_config": {"mode": "simple", "model": "gpt-5",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {}},
            "mcp_servers": {}
        }}})
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        assert agents["developer"]["engine"] == "copilot"
        assert agents["developer"]["model"] == "gpt-5"
        assert agents["developer"]["config_mode"] == "simple"

    def test_put_settings_unknown_model_config_key_is_silently_dropped(self, client):
        """Unknown model_config keys are filtered out rather than rejected (forward-compat)."""
        payload = {"agent_configs": {"developer": {
            "engine": "claude",
            "model_config": {"mode": "simple", "model": "claude-sonnet-4-6",
                             "provider_type": "openai", "base_url": "",
                             "api_key": "", "extra_params": {},
                             "unknown_key": "surprise"},
            "mcp_servers": {}
        }}}
        resp = client.put("/api/settings", json=payload)
        assert resp.status_code == 200
        agents = {a["role"]: a for a in client.get("/api/team").json()["agents"]}
        assert agents["developer"]["model"] == "claude-sonnet-4-6"


class TestSpaFallback:
    def test_root_serves_index_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_unknown_path_serves_index_html(self, client):
        resp = client.get("/some/spa/route")
        assert resp.status_code == 200


class TestApiPrs:
    def test_get_prs_gh_not_installed(self, client, monkeypatch):
        """Returns error field and empty prs list when gh binary not found."""
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError()
        monkeypatch.setattr("app.web.github.subprocess.run", fake_run)
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
        monkeypatch.setattr("app.web.github.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["prs"] == []
        assert "error" in data
        assert data["error"]

    def test_get_prs_auth_authenticated(self, client, monkeypatch):
        """Returns authenticated:true with username when gh auth status exits 0."""
        class FakeResult:
            returncode = 0
            stdout = "anil"
            stderr = ""
        monkeypatch.setattr("app.web.app.subprocess.run", lambda cmd, **kw: FakeResult())
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
        monkeypatch.setattr("app.web.app.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs/auth")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["user"] is None

    def test_post_prs_auth_login_gh_not_installed(self, client, monkeypatch):
        """Returns error when gh binary is not found."""
        def fake_run_missing(cmd, **kwargs):
            raise FileNotFoundError()
        monkeypatch.setattr("app.web.app.subprocess.run", fake_run_missing)
        resp = client.post("/api/prs/auth/login")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "not installed" in data["error"]

    def test_post_prs_auth_login_returns_opening_browser(self, client, monkeypatch):
        """Returns opening_browser status when gh is available."""
        import unittest.mock as mock

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr("app.web.app.subprocess.run", lambda cmd, **kw: FakeResult())
        monkeypatch.setattr("app.web.app.asyncio.create_task", mock.Mock(side_effect=lambda coro: coro.close()))
        resp = client.post("/api/prs/auth/login")
        assert resp.status_code == 200
        assert resp.json()["status"] == "opening_browser"

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

        monkeypatch.setattr("app.web.github.subprocess.run", fake_run)

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
        assert pr42["author"] == "anil"
        assert pr41["author"] == "bob"

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
        monkeypatch.setattr("app.web.github.subprocess.run", lambda cmd, **kw: r)

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
        monkeypatch.setattr("app.web.github.subprocess.run", lambda cmd, **kw: FakeResult())
        resp = client.get("/api/prs/99/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert data["error"]


class TestOrchestrationEngineRouting:
    @pytest.mark.asyncio
    async def test_claude_engine_agents_invoke_query(self):
        """Agents configured for claude engine go through ClaudeEngine (query())."""
        from claude_agent_sdk import ResultMessage

        _CANNED = {
            "build":    '{"status": "BUILD SUCCESS", "output": "ok", "build_errors": [], "test_failures": []}',
            "review":   '{"decision": "Approved", "findings": []}',
            "repo":     '{"mode": "commit_pr", "pr_url": "https://github.com/mock/repo/pull/1", "pr_number": 1, "diff": "diff"}\n{"mode": "poll_comments", "status": "NO ACTIONABLE COMMENTS", "count": 0, "comments": []}',
            "default":  "done",
        }
        query_calls = []

        async def fake_query(**kwargs):
            query_calls.append(kwargs)
            p = kwargs.get("prompt", "").upper()
            if "BUILD AND TEST" in p:
                result = _CANNED["build"]
            elif any(k in p for k in ("COMMIT ALL CHANGES", "POLL CI", "POLL PR", "PUSH CHANGES", "MARK COMMENTS")):
                result = _CANNED["repo"]
            elif "REVIEW" in p:
                result = _CANNED["review"]
            else:
                result = _CANNED["default"]
            mock = MagicMock(spec=ResultMessage)
            mock.result = result
            yield mock

        settings = AppSettings(
            orchestration_mode="orchestrated",
            max_design_cycles=1,
            max_impl_cycles=1,
            max_build_cycles=1,
            max_pr_cycles=1,
            agent_configs={role: AgentEngineConfig(engine="claude")
                           for role in ["lead-developer", "developer", "code-reviewer",
                                        "qa-engineer", "build-agent", "repo-manager"]},
        )

        with patch("app.engines.claude.query", fake_query):
            events = []
            on_event = AsyncMock(side_effect=lambda e: events.append(e))
            await run_pipeline_orchestrated(
                task="add hello world",
                settings=settings,
                on_event=on_event,
            )

        assert len(query_calls) >= 8

    @pytest.mark.asyncio
    async def test_copilot_engine_agents_invoke_copilot_client(self):
        """Agents configured for copilot engine go through CopilotEngine."""
        # Canned responses per role to advance the pipeline
        _CANNED = {
            "lead-developer": '{"decision": "Approved", "findings": []}',
            "developer": "implemented",
            "code-reviewer": '{"decision": "Approved", "findings": []}',
            "qa-engineer": '{"decision": "Approved", "findings": []}',
            "build-agent": '{"status": "BUILD SUCCESS", "output": "all tests passed", "build_errors": [], "test_failures": []}',
            "repo-manager": '{"mode": "commit_pr", "pr_url": "https://github.com/mock/repo/pull/1", "pr_number": 1, "diff": "diff --git a/f b/f"}\n{"mode": "poll_comments", "status": "NO ACTIONABLE COMMENTS", "count": 0, "comments": []}',
        }

        def _make_session(content):
            session = MagicMock()
            session.disconnect = AsyncMock()
            session.send = AsyncMock()

            idle_et = MagicMock()
            idle_et.value = "session.idle"
            idle_evt = MagicMock()
            idle_evt.type = idle_et
            idle_evt.data = MagicMock()

            msg_et = MagicMock()
            msg_et.value = "assistant.message"
            msg_evt = MagicMock()
            msg_evt.type = msg_et
            msg_evt.data = MagicMock()
            msg_evt.data.content = content

            def fake_on(handler):
                handler(msg_evt)
                handler(idle_evt)
                return lambda: None

            session.on = MagicMock(side_effect=fake_on)
            return session

        call_count = 0

        async def _create_session(**kwargs):
            nonlocal call_count
            call_count += 1
            # Extract role from client_name if present
            cn = kwargs.get("client_name", "")
            role = cn.split("_", 1)[-1] if "_" in cn else "lead-developer"
            content = _CANNED.get(role, "OK")
            return _make_session(content)

        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.create_session = AsyncMock(side_effect=_create_session)
        mock_client.resume_session = AsyncMock(side_effect=Exception("no resume"))

        settings = AppSettings(
            orchestration_mode="orchestrated",
            copilot_github_token="gh-tok",
            max_design_cycles=1,
            max_impl_cycles=1,
            max_build_cycles=1,
            max_pr_cycles=1,
            agent_configs={role: AgentEngineConfig(engine="copilot",
                                                    model_config=ModelConfig(model="gpt-5"))
                           for role in ["lead-developer", "developer", "code-reviewer",
                                        "qa-engineer", "build-agent", "repo-manager"]},
        )

        with patch("app.engines.copilot.CopilotClient", return_value=mock_client), \
             patch("app.engines.copilot.SubprocessConfig"):
            events = []
            on_event = AsyncMock(side_effect=lambda e: events.append(e))
            await run_pipeline_orchestrated(
                task="add hello world",
                settings=settings,
                on_event=on_event,
            )

        assert mock_client.start.call_count == 1
        assert mock_client.stop.call_count == 1
        assert call_count >= 8
