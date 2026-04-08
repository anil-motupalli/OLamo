"""run_pipeline_orchestrated — Python-driven deterministic orchestration mode."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ..models import (
    AppSettings,
    _ALL_REVIEWERS,
    get_default_engine_config,
    _resolve_default_model,
)
from ..agents import AGENT_CONFIGS
from ..engines import AgentEngine, ENGINE_REGISTRY
from .helpers import _reviewer_prompt, _extract_comment_ids, parse_review_json, parse_finding_responses, parse_build_output, parse_repo_output, _build_failed, _build_failure_summary, FINDING_RESPONSES_SEP


def _write_agent_log(log_dir: str, role: str, prompt: str, lines: list[str], result: str, elapsed_ms: int) -> None:
    """Append one agent call's I/O to logs/{run_id}/{role}.log — full content, no truncation."""
    try:
        log_path = Path(log_dir) / f"{role}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{'─' * 60}\n")
            fh.write(f"[{ts}] PROMPT ({elapsed_ms}ms):\n{prompt}\n\n")
            if lines:
                fh.write("ACTIVITY:\n" + "\n".join(lines) + "\n\n")
            fh.write(f"RESULT:\n{result}\n")
    except Exception:
        pass  # never crash the pipeline over logging


async def run_pipeline_orchestrated(
    task: str,
    settings: AppSettings,
    on_event: Callable[[dict], Awaitable[None]],
    pr_url: str = "",
    on_approval_required: Callable[[str, str], Awaitable[dict]] | None = None,
    checkpoint: dict | None = None,
    save_checkpoint: Callable[[dict], Awaitable[None]] | None = None,
    log_dir: str | None = None,
    run_id: str | None = None,
    db_conn=None,
) -> str:
    """Orchestration driven entirely by Python — no PM LLM, deterministic loops."""

    engines_to_stop: list[AgentEngine] = []

    if settings.headless:
        # Headless / dry-run: one MockEngine handles every role; no API calls made.
        mock_engine: AgentEngine = ENGINE_REGISTRY["mock"](settings)
        await mock_engine.start()
        engines_to_stop.append(mock_engine)

        def _resolve(role: str) -> tuple[AgentEngine, str, object, dict]:
            from ..models import ModelConfig
            return mock_engine, "mock-model", ModelConfig(), {}

    else:
        # Build only the engine instances that are actually needed.
        def _engine_type(role: str) -> str:
            return (settings.agent_configs.get(role) or get_default_engine_config(role, settings)).engine

        used_engine_names = {_engine_type(r) for r in AGENT_CONFIGS}
        engine_instances: dict[str, AgentEngine] = {}

        for name in used_engine_names:
            cls = ENGINE_REGISTRY.get(name)
            if cls is None:
                raise ValueError(f"Unknown engine '{name}'. Available: {list(ENGINE_REGISTRY.keys())}")
            instance = cls(settings)
            # Wire DB connection for engines that support it
            if name == "copilot" and db_conn is not None:
                instance.set_db_conn(db_conn)
            await instance.start()
            engine_instances[name] = instance
            engines_to_stop.append(instance)

        def _resolve(role: str) -> tuple[AgentEngine, str, object, dict]:
            cfg = settings.agent_configs.get(role) or get_default_engine_config(role, settings)
            eng = engine_instances.get(cfg.engine)
            if eng is None:
                raise ValueError(f"Engine '{cfg.engine}' required for role '{role}' but not started")
            model = cfg.model_config.model or _resolve_default_model(role, cfg.engine, settings)
            return eng, model, cfg.model_config, cfg.mcp_servers

    async def call(role: str, prompt: str) -> str:
        action = prompt[:120].strip().replace("\n", " ")
        t0 = time.monotonic()
        await on_event({"type": "agent_started", "role": role, "action": action})
        system_prompt, tools, _ = AGENT_CONFIGS[role]
        eng, model, model_config, mcp_servers = _resolve(role)

        # Intercept messages to write per-agent log file (captures messages AND tool calls)
        log_lines: list[str] = []
        async def _forwarding_on_event(evt: dict) -> None:
            etype = evt.get("type", "")
            if etype == "agent_message":
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log_lines.append(f"[{ts}] MESSAGE: {evt.get('text', '')}")
            elif etype == "agent_tool_call":
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log_lines.append(f"[{ts}] TOOL CALL: {evt.get('tool_name')}({evt.get('args_preview', '')})")
            elif etype == "agent_tool_result":
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log_lines.append(f"[{ts}] TOOL RESULT ({evt.get('tool_name')}): {evt.get('result_preview', '')}")
            await on_event(evt)

        try:
            result = await eng.run(
                role=role,
                prompt=prompt,
                system_prompt=system_prompt,
                tools=tools,
                model=model,
                model_config=model_config,
                mcp_servers=mcp_servers,
                on_event=_forwarding_on_event,
                run_id=run_id,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            summary = result.strip() if result else ""
            await on_event({"type": "agent_completed", "role": role, "success": True, "elapsed_ms": elapsed_ms, "summary": summary})
            # Write per-agent log
            if log_dir:
                _write_agent_log(log_dir, role, prompt, log_lines, result, elapsed_ms)
            return result
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            err_msg = str(e)[:300]
            await on_event({"type": "agent_completed", "role": role, "success": False, "elapsed_ms": elapsed_ms, "summary": err_msg})
            if log_dir:
                _write_agent_log(log_dir, role, prompt, log_lines, f"ERROR: {e}", elapsed_ms)
            raise RuntimeError(f"Agent '{role}' failed: {e}") from e

    async def stage(label: str, cycle: int | None = None) -> None:
        await on_event({"type": "stage_changed", "stage": label, "cycle": cycle})

    try:
        completed_stage = (checkpoint or {}).get("completed_stage", 0)

        plan = (checkpoint or {}).get("plan", task)
        last_diff = (checkpoint or {}).get("last_diff", "")
        pr_result = (checkpoint or {}).get("pr_result", pr_url)
        addressed_ids = list((checkpoint or {}).get("addressed_ids", []))

        if not pr_url and completed_stage < 1:
            # ── Stage 1: Design Loop ──────────────────────────────────────────────────
            await stage("Stage 1: Design", cycle=0)
            plan = await call("lead-developer", task)

            # Emit initial plan as revision 0
            await on_event({"type": "design_plan_created", "revision": 0, "plan": plan})

            qa_approved = False
            pending_findings: list[dict] = []

            for i in range(settings.max_design_cycles):
                await stage(f"Design cycle {i + 1}/{settings.max_design_cycles}", cycle=i + 1)
                qa_prompt = f"REVIEW DESIGN:\n\n{plan}"
                if pending_findings:
                    # Include previous finding responses to help QA decide on pushbacks
                    qa_prompt += f"\n\n---\n\nLead developer's responses to previous findings are embedded in the plan below the {FINDING_RESPONSES_SEP!r} marker."
                qa_result = await call("qa-engineer", qa_prompt)
                review = parse_review_json(qa_result)

                # Emit findings for this cycle
                await on_event({
                    "type": "design_review_findings",
                    "revision": i,
                    "findings": review["findings"],
                    "decision": review["decision"],
                })

                if review["decision"] == "Approved" or not review["findings"]:
                    qa_approved = True
                    break
                if i < settings.max_design_cycles - 1:
                    findings_json = __import__("json").dumps(review["findings"])
                    lead_result = await call(
                        "lead-developer",
                        f"Task:\n{task}\n\nCurrent Plan:\n{plan}\n\nQA Findings (JSON):\n{findings_json}",
                    )
                    revised_plan, finding_responses = parse_finding_responses(lead_result)
                    plan = revised_plan if revised_plan else lead_result
                    pending_findings = finding_responses

                    # Emit revised plan with per-finding responses
                    await on_event({
                        "type": "design_plan_revised",
                        "revision": i + 1,
                        "plan": plan,
                        "responses": finding_responses,
                    })

            # Emit final approved event
            await on_event({"type": "design_approved", "plan": plan})

            # Build the human-review spec: full design plan + QA's final assessment
            spec_for_review = plan

            # Optional human approval gate after design loop — only when QA has approved, skipped in headless mode
            if on_approval_required is not None and not settings.headless and qa_approved:
                dev_response = ""  # developer's response to show on subsequent rounds
                while True:
                    gate_result = await on_approval_required(spec_for_review, dev_response)
                    if gate_result.get("approved"):
                        break
                    feedback = gate_result.get("feedback", "")
                    comments = gate_result.get("comments", [])
                    if feedback or comments:
                        comment_text = ""
                        if comments:
                            comment_lines = "\n".join(
                                f"- [{c.get('selectedText', '')}]: {c.get('commentText', '')}"
                                for c in comments
                            )
                            comment_text = f"\n\nInline comments:\n{comment_lines}"
                        plan = await call(
                            "lead-developer",
                            f"REFINE the following plan based on human feedback.\n\n"
                            f"Plan:\n{plan}\n\n"
                            f"Feedback:\n{feedback}{comment_text}",
                        )
                        # Strip any finding-responses separator from human-feedback-driven refinement
                        plan, _ = parse_finding_responses(plan)
                        # Rebuild the spec for the next review round with updated plan + fresh QA run
                        qa_result = await call("qa-engineer", f"REVIEW DESIGN:\n{plan}")
                        qa_review = parse_review_json(qa_result)
                        qa_approved = qa_review["decision"] == "Approved"
                        await on_event({
                            "type": "design_review_findings",
                            "revision": -1,
                            "findings": qa_review["findings"],
                            "decision": qa_review["decision"],
                        })
                        await on_event({"type": "design_approved", "plan": plan})
                        spec_for_review = plan
                        dev_response = plan[:300].strip()

            if save_checkpoint:
                await save_checkpoint({
                    "completed_stage": 1,
                    "plan": plan,
                    "last_diff": last_diff,
                    "pr_result": pr_result,
                    "addressed_ids": addressed_ids,
                    "already_approved": [],
                })

        if not pr_url and completed_stage < 3:
            # ── Stage 2: Implementation Loop ─────────────────────────────────────────
            await stage("Stage 2: Implementation", cycle=0)
            impl_findings: list[dict] = []
            already_approved: set[str] = set()

            for impl_cycle in range(settings.max_impl_cycles):
                await stage(f"Implementation cycle {impl_cycle + 1}/{settings.max_impl_cycles}", cycle=impl_cycle + 1)

                if not impl_findings:
                    impl_prompt = plan
                else:
                    import json as _json
                    impl_prompt = f"{plan}\n\nReview findings to address (JSON):\n{_json.dumps(impl_findings)}"
                dev_result = await call("developer", impl_prompt)
                # Parse developer's per-finding responses
                dev_summary, dev_responses = parse_finding_responses(dev_result)

                # Build loop
                build_ok = False
                build_parsed: dict = {}
                for build_cycle in range(settings.max_build_cycles):
                    raw_build = await call("build-agent", "Build and test the project.")
                    build_parsed = parse_build_output(raw_build)
                    if not _build_failed(build_parsed):
                        build_ok = True
                        break
                    if build_cycle < settings.max_build_cycles - 1:
                        dev_result = await call("developer", f"FIX BUILD FAILURE:\n{_build_failure_summary(build_parsed)}")
                        dev_summary, dev_responses = parse_finding_responses(dev_result)

                if not build_ok:
                    impl_findings = [{"id": "build-fail", "type": "Bug", "severity": "Critical",
                                      "file": None, "line": 0,
                                      "description": f"Build failed after {settings.max_build_cycles} retries",
                                      "suggestion": _build_failure_summary(build_parsed)[:500]}]
                    break  # exit impl loop; guard below will raise a build-failure error

                # Code review — pass developer's per-finding JSON responses so reviewers can weigh pushbacks
                diff_ctx = f"\nGit diff for context:\n{last_diff}" if last_diff else ""
                dev_resp_ctx = ""
                if dev_responses:
                    import json as _json2
                    dev_resp_ctx = f"\n\n---FINDING_RESPONSES---\n{_json2.dumps(dev_responses)}"
                pending = [r for r in _ALL_REVIEWERS if r not in already_approved]

                reviewer_results: dict[str, dict] = {}
                if pending:
                    raw_results = await asyncio.gather(
                        *[call(r, _reviewer_prompt(r, plan, diff_ctx) + dev_resp_ctx) for r in pending]
                    )
                    for role, raw in zip(pending, raw_results):
                        review = parse_review_json(raw)
                        reviewer_results[role] = review
                        if review["decision"] == "Approved":
                            already_approved.add(role)

                # Re-invite approved reviewers only if pending reviewers found critical issues AND
                # the developer is actually addressing them (not purely pushing back).
                combined_findings = [f for rv in reviewer_results.values() for f in rv.get("findings", [])]
                has_critical = any(f.get("severity") in ("Critical", "MustHave") for f in combined_findings)
                developer_addressing = not dev_responses or any(
                    r.get("action", "ADDRESSED").upper() != "PUSHBACK" for r in dev_responses
                )
                if has_critical and developer_addressing and already_approved:
                    reinvite = list(already_approved)
                    re_results = await asyncio.gather(
                        *[call(r, _reviewer_prompt(r, plan, diff_ctx) + dev_resp_ctx) for r in reinvite]
                    )
                    for role, raw in zip(reinvite, re_results):
                        review = parse_review_json(raw)
                        reviewer_results[role] = review
                        if review["decision"] == "NeedsImprovement":
                            already_approved.discard(role)
                elif has_critical and not developer_addressing:
                    await on_event({
                        "type": "agent_message", "role": "orchestrator",
                        "text": f"Developer is pushing back on all critical findings — skipping re-invite of approved reviewer(s): {', '.join(sorted(already_approved))}",
                    })
                elif already_approved:
                    await on_event({
                        "type": "agent_message", "role": "orchestrator",
                        "text": f"Skipping approved reviewer(s): {', '.join(sorted(already_approved))}",
                    })

                impl_findings = [
                    f for rv in reviewer_results.values()
                    if rv.get("decision") == "NeedsImprovement"
                    for f in rv.get("findings", [])
                ]

                if save_checkpoint:
                    await save_checkpoint({
                        "completed_stage": 1,
                        "plan": plan,
                        "last_diff": last_diff,
                        "pr_result": pr_result,
                        "addressed_ids": addressed_ids,
                        "already_approved": list(already_approved),
                    })

                if not impl_findings:
                    break

            # Guard: only proceed to commit if every reviewer has approved.
            if any(f.get("id") == "build-fail" for f in impl_findings):
                raise RuntimeError(
                    f"Build/test failures persisted after {settings.max_build_cycles} retries: "
                    f"{impl_findings[0].get('suggestion', '')[:300]}"
                )
            unapproved = set(_ALL_REVIEWERS) - already_approved
            if unapproved:
                raise RuntimeError(
                    f"Implementation cycle exhausted ({settings.max_impl_cycles} cycles) "
                    f"without approval from: {', '.join(sorted(unapproved))}"
                )

            # ── Stage 3: Commit & PR ──────────────────────────────────────────────────
            await stage("Stage 3: Commit & PR", cycle=0)
            raw_pr = await call(
                "repo-manager",
                f"Commit all changes and create a Pull Request.\n"
                f"Branch: feature/{re.sub(r'[^a-z0-9]+', '-', task[:50].lower()).strip('-')}\n"
                f"Title: {task[:72]}\nDescription: Implemented via OLamo orchestrated pipeline.",
            )
            pr_parsed = parse_repo_output(raw_pr)
            pr_result = pr_parsed.get("pr_url", raw_pr)
            last_diff = pr_parsed.get("diff", raw_pr)

            if save_checkpoint:
                await save_checkpoint({
                    "completed_stage": 3,
                    "plan": plan,
                    "last_diff": last_diff,
                    "pr_result": pr_result,
                    "addressed_ids": addressed_ids,
                    "already_approved": [],
                })

        elif not pr_url and completed_stage >= 3:
            await on_event({"type": "agent_message", "role": "orchestrator",
                            "text": f"Resuming from Stage 3b (Stage 1-3 already completed). PR: {pr_result[:100]}"})

        # ── Stage 3b: CI Check Polling ────────────────────────────────────────────
        for ci_cycle in range(settings.max_pr_cycles):
            await stage(f"CI check cycle {ci_cycle + 1}/{settings.max_pr_cycles}", cycle=ci_cycle + 1)
            raw_ci = await call("repo-manager", "POLL CI CHECKS")
            ci_parsed = parse_repo_output(raw_ci)
            if ci_parsed.get("status", "").upper() == "CHECKS PASSING":
                break

            await call("developer", f"Fix the following CI check failures:\n{ci_parsed.get('details', raw_ci)}")

            raw_build = await call("build-agent", "Build and test the project.")
            build_parsed = parse_build_output(raw_build)
            if _build_failed(build_parsed):
                await call("developer", f"FIX BUILD FAILURE:\n{_build_failure_summary(build_parsed)}")
                await call("build-agent", "Build and test the project.")

            raw_push = await call("repo-manager", "PUSH CHANGES")
            last_diff = parse_repo_output(raw_push).get("diff", raw_push)

        # ── Stage 4: PR Poll Loop ─────────────────────────────────────────────────
        await stage("Stage 4: PR Poll", cycle=0)

        for pr_cycle in range(settings.max_pr_cycles):
            await stage(f"PR cycle {pr_cycle + 1}/{settings.max_pr_cycles}", cycle=pr_cycle + 1)

            exclude = f" Exclude these IDs: {', '.join(addressed_ids)}" if addressed_ids else ""
            raw_poll = await call("repo-manager", f"POLL PR COMMENTS.{exclude}")
            poll_parsed = parse_repo_output(raw_poll)

            if poll_parsed.get("status", "").upper() == "NO ACTIONABLE COMMENTS":
                break

            comments = poll_parsed.get("comments", [])
            new_ids = [c["id"] for c in comments if c.get("id")] or _extract_comment_ids(raw_poll)
            if new_ids:
                addressed_ids.extend(new_ids)
                await call("repo-manager", f"MARK COMMENTS ADDRESSED: {', '.join(new_ids)}")

            import json as _json3
            comment_text = _json3.dumps(comments) if comments else raw_poll
            await call("developer", f"Address the following PR review comments:\n{comment_text}")

            raw_build = await call("build-agent", "Build and test the project.")
            build_parsed = parse_build_output(raw_build)
            if _build_failed(build_parsed):
                await call("developer", f"FIX BUILD FAILURE:\n{_build_failure_summary(build_parsed)}")
                await call("build-agent", "Build and test the project.")

            # One reviewer pass after PR comment fix
            diff_ctx = f"\nGit diff for context:\n{last_diff}" if last_diff else ""
            raw_reviews = await asyncio.gather(
                *[call(r, _reviewer_prompt(r, plan, diff_ctx)) for r in _ALL_REVIEWERS]
            )
            pr_review_findings = [
                f for raw in raw_reviews
                for f in parse_review_json(raw).get("findings", [])
                if parse_review_json(raw).get("decision") == "NeedsImprovement"
            ]
            if pr_review_findings:
                import json as _json4
                await call("developer", f"Address review findings before pushing:\n{_json4.dumps(pr_review_findings)}")
                raw_build = await call("build-agent", "Build and test the project.")
                build_parsed = parse_build_output(raw_build)
                if _build_failed(build_parsed):
                    await call("developer", f"FIX BUILD FAILURE:\n{_build_failure_summary(build_parsed)}")
                    await call("build-agent", "Build and test the project.")

            raw_push = await call("repo-manager", "PUSH CHANGES")
            last_diff = parse_repo_output(raw_push).get("diff", raw_push)

            if save_checkpoint:
                await save_checkpoint({
                    "completed_stage": 3,
                    "plan": plan,
                    "last_diff": last_diff,
                    "pr_result": pr_result,
                    "addressed_ids": addressed_ids,
                    "already_approved": [],
                })

        return f"Pipeline complete. PR: {pr_result[:200]}"
    finally:
        # Close per-run sessions for CopilotEngine (disconnects, marks DB closed)
        CopilotEngine = ENGINE_REGISTRY.get("copilot")
        for eng in engines_to_stop:
            if CopilotEngine and isinstance(eng, CopilotEngine) and run_id:
                try:
                    await eng.close_run(run_id)
                except Exception:
                    pass
        for eng in engines_to_stop:
            await eng.stop()
