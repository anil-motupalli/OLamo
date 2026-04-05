"""Tests for app.pipeline.helpers and app.pipeline.pm."""

import asyncio

import pytest

from app.agents import build_pm_prompt
from app.models import AppSettings, _ALL_REVIEWERS
from app.pipeline.helpers import (
    ApprovalGate,
    _extract_comment_ids,
    _make_env,
    _parse_stage_announcement,
    _reviewer_prompt,
)


class TestParseStageAnnouncement:
    def test_parses_stage_1(self):
        assert _parse_stage_announcement("Moving to Stage 1 now") == "Stage 1"

    def test_parses_stage_2(self):
        assert _parse_stage_announcement("Advancing to Stage 2 — implementation") == "Stage 2"

    def test_parses_stage_3(self):
        assert _parse_stage_announcement("Beginning Stage 3") == "Stage 3"

    def test_parses_stage_4(self):
        assert _parse_stage_announcement("Entering Stage 4 — PR poll") == "Stage 4"

    def test_parses_design_cycle(self):
        assert _parse_stage_announcement("Design cycle 1/2 complete") == "Design cycle 1/2"

    def test_parses_design_cycle_second(self):
        assert _parse_stage_announcement("Design cycle 2/2") == "Design cycle 2/2"

    def test_parses_implementation_cycle(self):
        assert _parse_stage_announcement("Implementation cycle 2/3") == "Implementation cycle 2/3"

    def test_parses_pr_cycle(self):
        assert _parse_stage_announcement("PR cycle 1/2") == "PR cycle 1/2"

    def test_parses_ci_check_cycle(self):
        assert _parse_stage_announcement("CI check cycle 1/2") == "CI check cycle 1/2"

    def test_parses_ci_check_cycle_second(self):
        assert _parse_stage_announcement("CI check cycle 2/2") == "CI check cycle 2/2"

    def test_case_insensitive_stage(self):
        result = _parse_stage_announcement("STAGE 2 begins")
        assert result is not None
        assert "2" in result

    def test_returns_none_for_unrelated_text(self):
        assert _parse_stage_announcement("Nothing special here") is None

    def test_returns_none_for_empty_string(self):
        assert _parse_stage_announcement("") is None

    def test_returns_none_for_partial_match(self):
        assert _parse_stage_announcement("The staging environment is ready") is None


class TestReviewerPrompt:
    def test_code_reviewer_prompt(self):
        prompt = _reviewer_prompt("code-reviewer", "the plan", "")
        assert "Review the implementation" in prompt

    def test_qa_engineer_prompt_includes_plan(self):
        prompt = _reviewer_prompt("qa-engineer", "my plan", "")
        assert "REVIEW CODE" in prompt
        assert "my plan" in prompt

    def test_lead_developer_prompt_includes_plan(self):
        prompt = _reviewer_prompt("lead-developer", "my plan", "")
        assert "REVIEW IMPLEMENTATION" in prompt
        assert "my plan" in prompt

    def test_diff_ctx_appended(self):
        prompt = _reviewer_prompt("code-reviewer", "p", "\ndiff --git a/f b/f")
        assert "diff --git" in prompt

    def test_all_reviewers_constant_has_three_entries(self):
        assert len(_ALL_REVIEWERS) == 3

    def test_all_reviewers_contains_expected_roles(self):
        assert "code-reviewer" in _ALL_REVIEWERS
        assert "qa-engineer" in _ALL_REVIEWERS
        assert "lead-developer" in _ALL_REVIEWERS


class TestBuildPmPrompt:
    def test_contains_all_four_stage_headers(self):
        prompt = build_pm_prompt(AppSettings())
        for stage in ("STAGE 1", "STAGE 2", "STAGE 3", "STAGE 4"):
            assert stage in prompt, f"Missing {stage}"

    def test_embeds_design_cycle_limit(self):
        s = AppSettings(max_design_cycles=7)
        prompt = build_pm_prompt(s)
        assert "7" in prompt

    def test_embeds_impl_cycle_limit(self):
        s = AppSettings(max_impl_cycles=9)
        prompt = build_pm_prompt(s)
        assert "9" in prompt

    def test_embeds_pr_cycle_limit(self):
        s = AppSettings(max_pr_cycles=4)
        prompt = build_pm_prompt(s)
        assert "4" in prompt

    def test_has_explicit_build_failure_gate(self):
        prompt = build_pm_prompt(AppSettings())
        assert "Only proceed to 2c if build-agent reports SUCCESS" in prompt

    def test_has_addressed_id_tracking(self):
        prompt = build_pm_prompt(AppSettings())
        assert "Exclude these IDs" in prompt

    def test_has_diff_handoff_for_reviewers(self):
        prompt = build_pm_prompt(AppSettings())
        assert "git diff" in prompt

    def test_has_mark_comments_addressed(self):
        prompt = build_pm_prompt(AppSettings())
        assert "MARK COMMENTS ADDRESSED" in prompt

    def test_has_ci_check_polling(self):
        prompt = build_pm_prompt(AppSettings())
        assert "POLL CI CHECKS" in prompt

    def test_has_checks_passing_gate(self):
        prompt = build_pm_prompt(AppSettings())
        assert "CHECKS PASSING" in prompt

    def test_has_ci_check_cycle_announcement(self):
        prompt = build_pm_prompt(AppSettings())
        assert "CI check cycle" in prompt

    def test_different_settings_produce_different_prompts(self):
        p1 = build_pm_prompt(AppSettings(max_design_cycles=2))
        p2 = build_pm_prompt(AppSettings(max_design_cycles=5))
        assert p1 != p2


class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_is_waiting_false_before_wait(self):
        gate = ApprovalGate()
        assert not gate.is_waiting

    @pytest.mark.asyncio
    async def test_wait_stores_current_plan(self):
        gate = ApprovalGate()
        task = asyncio.create_task(gate.wait("my plan"))
        await asyncio.sleep(0)
        assert gate.current_plan == "my plan"
        assert gate.is_waiting
        gate.resolve(approved=True)
        await task

    @pytest.mark.asyncio
    async def test_resolve_approved_returns_true(self):
        gate = ApprovalGate()
        task = asyncio.create_task(gate.wait("plan"))
        await asyncio.sleep(0)
        gate.resolve(approved=True)
        result = await task
        assert result == {"approved": True, "feedback": "", "comments": []}

    @pytest.mark.asyncio
    async def test_resolve_with_feedback(self):
        gate = ApprovalGate()
        task = asyncio.create_task(gate.wait("plan"))
        await asyncio.sleep(0)
        gate.resolve(approved=False, feedback="needs more detail")
        result = await task
        assert result == {"approved": False, "feedback": "needs more detail", "comments": []}

    @pytest.mark.asyncio
    async def test_is_waiting_false_after_resolve(self):
        gate = ApprovalGate()
        task = asyncio.create_task(gate.wait("plan"))
        await asyncio.sleep(0)
        gate.resolve(approved=True)
        await task
        assert not gate.is_waiting

    def test_resolve_without_wait_does_not_raise(self):
        gate = ApprovalGate()
        gate.resolve(approved=True)  # should not raise


class TestMakeEnv:
    def test_always_unsets_claudecode(self):
        env = _make_env(AppSettings())
        assert env.get("CLAUDECODE") == ""

    def test_no_base_url_when_empty(self):
        env = _make_env(AppSettings(api_base_url=""))
        assert "ANTHROPIC_BASE_URL" not in env

    def test_sets_base_url_when_provided(self):
        env = _make_env(AppSettings(api_base_url="https://proxy.example.com"))
        assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example.com"


class TestExtractCommentIds:
    def test_extracts_single_id(self):
        text = "ID: 42\nauthor: alice\nbody: fix this"
        assert _extract_comment_ids(text) == ["42"]

    def test_extracts_multiple_ids(self):
        text = "ID: 101\n...\nID: 202\n...\nID: 303"
        assert _extract_comment_ids(text) == ["101", "202", "303"]

    def test_case_insensitive(self):
        assert _extract_comment_ids("id: abc123") == ["abc123"]

    def test_returns_empty_for_no_ids(self):
        assert _extract_comment_ids("NO ACTIONABLE COMMENTS") == []

    def test_returns_empty_for_empty_string(self):
        assert _extract_comment_ids("") == []

    def test_handles_alphanumeric_ids(self):
        assert _extract_comment_ids("ID: PR-456") == ["PR-456"]
