"""FastAPI application factory — create_app()."""

import asyncio
import subprocess
from dataclasses import asdict
from pathlib import Path

from ..models import (
    AppSettings,
    _settings_from_dict,
    _COPILOT_DEFAULTS,
    _CLAUDE_TIER,
    get_default_engine_config,
)
from ..agents import build_agents
from ..settings import SettingsStore
from .broadcaster import SseBroadcaster
from .run_manager import RunManager
from .github import _run_gh, _pr_number_from_url


def create_app(settings_file: Path | None = None):  # noqa: ANN201
    try:
        from contextlib import asynccontextmanager

        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, JSONResponse
        from sse_starlette.sse import EventSourceResponse
    except ImportError as exc:
        raise SystemExit(
            "Web dependencies missing. Install with:\n"
            "  pip install fastapi uvicorn[standard] sse-starlette aiofiles aiosqlite"
        ) from exc

    broadcaster = SseBroadcaster()
    store = SettingsStore(settings_file=settings_file)
    manager = RunManager(broadcaster, store)
    static_dir = Path(__file__).parent.parent.parent / "static"

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN001
        await manager.setup()
        manager.start()
        yield
        await manager.close()

    app = FastAPI(title="OLamo", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/events")
    async def sse_stream() -> EventSourceResponse:
        cid, q = await broadcaster.connect()

        async def generator():
            try:
                while True:
                    data = await q.get()
                    if data is None:
                        break
                    yield {"data": data}
            finally:
                await broadcaster.disconnect(cid)

        return EventSourceResponse(generator())

    @app.get("/api/runs")
    async def list_runs() -> list[dict]:
        return [asdict(r) for r in manager.all_runs]

    @app.post("/api/runs", status_code=201)
    async def create_run(request: Request) -> dict:
        body = await request.json()
        description = (body.get("description") or "").strip()
        if not description:
            raise HTTPException(status_code=400, detail="description required")
        pr_url = (body.get("pr_url") or "").strip()
        settings_override = body.get("settings_override") or {}
        run = await manager.enqueue(description, pr_url=pr_url, settings_override=settings_override)
        return asdict(run)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        run = manager.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return asdict(run)

    @app.get("/api/runs/{run_id}/approval")
    async def get_approval(run_id: str) -> dict:
        if manager.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        gate = manager.pending_approvals.get(run_id)
        if gate is None or not gate.is_waiting:
            return {"waiting": False, "plan": ""}
        return {"waiting": True, "plan": gate.current_plan}

    @app.post("/api/runs/{run_id}/approval")
    async def resolve_approval(run_id: str, request: Request) -> dict:
        if manager.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        gate = manager.pending_approvals.get(run_id)
        if gate is None or not gate.is_waiting:
            raise HTTPException(status_code=409, detail="run is not awaiting approval")
        body = await request.json()
        gate.resolve(
            bool(body.get("approved", False)),
            (body.get("feedback") or "").strip(),
            body.get("comments") or [],
        )
        return {"ok": True}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> list[dict]:
        if manager.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return await manager.get_run_events(run_id)

    @app.get("/api/runs/{run_id}/state")
    async def get_run_state(run_id: str) -> dict:
        if manager.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        state = await manager.get_run_state(run_id)
        return state or {
            "run_id": run_id,
            "current_stage": None,
            "current_cycle": None,
            "last_agent": None,
            "updated_at": None,
        }

    @app.get("/api/team")
    async def team() -> dict:
        s = store.settings
        agents = build_agents(s)
        agent_list = []
        for role, defn in agents.items():
            cfg = s.agent_configs.get(role) or get_default_engine_config(role, s)
            # Resolve the effective model: explicit config > engine smart default
            if cfg.model_config.model:
                model = cfg.model_config.model
            elif cfg.engine == "copilot":
                model = _COPILOT_DEFAULTS.get(role, "")
            else:
                model = getattr(s, _CLAUDE_TIER.get(role, "sonnet_model"), "")
            agent_list.append({
                "role": role,
                "model": model,
                "description": defn.description,
                "engine": cfg.engine,
                "config_mode": cfg.model_config.mode,
            })
        return {
            "agents": agent_list,
            "pipeline": ["Design Loop", "Implementation Loop", "Commit & PR", "PR Poll"],
            "cycle_limits": {
                "max_design_cycles": s.max_design_cycles,
                "max_build_cycles": s.max_build_cycles,
                "max_impl_cycles": s.max_impl_cycles,
                "max_pr_cycles": s.max_pr_cycles,
            },
        }

    @app.get("/api/settings")
    async def get_settings() -> dict:
        return {"config": asdict(store.settings), "is_locked": store.is_locked}

    @app.put("/api/settings")
    async def update_settings(request: Request) -> dict:
        body = await request.json()
        try:
            current = asdict(store.settings)
            merged = {**current, **{k: v for k, v in body.items()
                                    if k in AppSettings.__dataclass_fields__}}
            new_settings = _settings_from_dict(merged)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=str(e))
        applied = await store.try_update(new_settings)
        return {"applied": applied, "config": asdict(store.settings)}

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

    @app.get("/api/prs/{number}/check")
    async def check_pr(number: int) -> dict:
        try:
            return _run_gh(
                ["pr", "view", str(number), "--json", "comments,reviews,statusCheckRollup"]
            )
        except RuntimeError as e:
            return {"error": str(e)}

    @app.get("/{path:path}")
    async def spa_fallback(path: str) -> FileResponse:
        index = static_dir / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(status_code=404)

    return app
