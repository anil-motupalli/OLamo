"""Tests for app.pipeline.helpers and app.pipeline.pm."""

import asyncio

import pytest

from app.agents import build_pm_prompt
from app.models import AppSettings, _ALL_REVIEWERS
from app.pipeline.helpers import (
    ApprovalGate,
    _extract_comment_ids,
    _parse_stage_announcement,
    _reviewer_prompt,
    parse_review_json,
    parse_finding_responses,
    parse_build_output,
    parse_repo_output,
    _build_failed,
    _build_failure_summary,
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
    def test_qa_engineer_prompt_includes_plan(self):
        prompt = _reviewer_prompt("qa-engineer", "my plan", "")
        assert "REVIEW CODE" in prompt
        assert "my plan" in prompt

    def test_lead_developer_prompt_includes_plan(self):
        prompt = _reviewer_prompt("lead-developer", "my plan", "")
        assert "REVIEW IMPLEMENTATION" in prompt
        assert "my plan" in prompt

    def test_diff_ctx_appended(self):
        prompt = _reviewer_prompt("qa-engineer", "p", "\ndiff --git a/f b/f")
        assert "diff --git" in prompt

    def test_all_reviewers_constant_has_two_entries(self):
        assert len(_ALL_REVIEWERS) == 2

    def test_all_reviewers_contains_expected_roles(self):
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


class TestParseReviewJson:
    def test_valid_approved_json(self):
        text = '{"decision": "Approved", "findings": []}'
        result = parse_review_json(text)
        assert result["decision"] == "Approved"
        assert result["findings"] == []

    def test_valid_needs_improvement_json(self):
        text = '{"decision": "NeedsImprovement", "findings": [{"id": "f1", "type": "Bug", "severity": "Critical", "file": "x.py", "line": 1, "description": "d", "suggestion": "s"}]}'
        result = parse_review_json(text)
        assert result["decision"] == "NeedsImprovement"
        assert len(result["findings"]) == 1
        assert result["findings"][0]["id"] == "f1"

    def test_markdown_fenced_json(self):
        text = '```json\n{"decision": "Approved", "findings": []}\n```'
        result = parse_review_json(text)
        assert result["decision"] == "Approved"

    def test_assigns_ids_when_missing(self):
        text = '{"decision": "NeedsImprovement", "findings": [{"type": "Bug", "severity": "MustHave", "file": null, "line": 0, "description": "d", "suggestion": "s"}]}'
        result = parse_review_json(text)
        assert result["findings"][0]["id"] == "f1"

    def test_fallback_approved_keyword(self):
        result = parse_review_json("Everything looks good. APPROVED.")
        assert result["decision"] == "Approved"
        assert result["findings"] == []

    def test_fallback_needs_improvement_keyword(self):
        result = parse_review_json("NEEDS IMPROVEMENT: missing tests")
        assert result["decision"] == "NeedsImprovement"


class TestParseFindingResponses:
    def test_no_separator_returns_full_text(self):
        text, responses = parse_finding_responses("just some output")
        assert text == "just some output"
        assert responses == []

    def test_splits_on_separator(self):
        responses_json = '[{"id": "f1", "action": "FIXED", "explanation": "done"}]'
        text, responses = parse_finding_responses(f"my plan\n---FINDING_RESPONSES---\n{responses_json}")
        assert text == "my plan"
        assert len(responses) == 1
        assert responses[0]["id"] == "f1"
        assert responses[0]["action"] == "FIXED"

    def test_invalid_json_returns_empty_responses(self):
        text, responses = parse_finding_responses("plan\n---FINDING_RESPONSES---\nnot json")
        assert text == "plan"
        assert responses == []


class TestParseBuildOutput:
    def test_success_json(self):
        text = '{"status": "BUILD SUCCESS", "output": "all good", "build_errors": [], "test_failures": []}'
        result = parse_build_output(text)
        assert result["status"] == "BUILD SUCCESS"
        assert result["build_errors"] == []
        assert result["test_failures"] == []

    def test_build_failure_json_with_errors(self):
        text = '{"status": "BUILD FAILURE", "output": "boom", "build_errors": [{"file": "src/foo.py", "line": 5, "message": "SyntaxError"}], "test_failures": []}'
        result = parse_build_output(text)
        assert result["status"] == "BUILD FAILURE"
        assert len(result["build_errors"]) == 1
        assert result["build_errors"][0]["file"] == "src/foo.py"

    def test_test_failure_json(self):
        text = '{"status": "TEST FAILURE", "output": "3 failed", "build_errors": [], "test_failures": [{"test": "test_add", "file": "tests/test_math.py", "line": 10, "error": "AssertionError"}]}'
        result = parse_build_output(text)
        assert result["status"] == "TEST FAILURE"
        assert len(result["test_failures"]) == 1
        assert result["test_failures"][0]["test"] == "test_add"

    def test_fallback_success_keyword(self):
        result = parse_build_output("All tests passed. BUILD SUCCESS.")
        assert result["status"] == "BUILD SUCCESS"
        assert not _build_failed(result)

    def test_fallback_failure_keyword(self):
        result = parse_build_output("Compilation error. BUILD FAILURE.")
        assert _build_failed(result)

    def test_defaults_missing_fields(self):
        result = parse_build_output('{"status": "BUILD FAILURE", "output": "err"}')
        assert result["build_errors"] == []
        assert result["test_failures"] == []

    def test_back_compat_errors_string(self):
        result = parse_build_output('{"status": "BUILD FAILURE", "output": "x", "errors": "old style error"}')
        assert result["build_errors"] == [{"file": None, "line": 0, "message": "old style error"}]

    def test_build_failure_summary_includes_errors(self):
        result = parse_build_output('{"status": "BUILD FAILURE", "output": "x", "build_errors": [{"file": "f.py", "line": 1, "message": "E1"}], "test_failures": []}')
        summary = _build_failure_summary(result)
        assert "E1" in summary
        assert "BUILD FAILURE" in summary

    def test_build_failure_summary_includes_test_failures(self):
        result = parse_build_output('{"status": "TEST FAILURE", "output": "x", "build_errors": [], "test_failures": [{"test": "test_x", "file": "t.py", "line": 3, "error": "AssertionError"}]}')
        summary = _build_failure_summary(result)
        assert "test_x" in summary


class TestParseRepoOutput:
    def test_commit_pr_json(self):
        text = '{"mode": "commit_pr", "pr_url": "https://github.com/o/r/pull/1", "pr_number": 1, "diff": "diff --git a/f"}'
        result = parse_repo_output(text)
        assert result["mode"] == "commit_pr"
        assert result["pr_url"] == "https://github.com/o/r/pull/1"
        assert result["diff"].startswith("diff --git")

    def test_poll_comments_no_actionable(self):
        text = '{"mode": "poll_comments", "status": "NO ACTIONABLE COMMENTS", "count": 0, "comments": []}'
        result = parse_repo_output(text)
        assert result["status"] == "NO ACTIONABLE COMMENTS"
        assert result["comments"] == []

    def test_poll_comments_with_comments(self):
        text = '{"mode": "poll_comments", "status": "ACTIONABLE COMMENTS FOUND: 2", "count": 2, "comments": [{"id": "c1", "author": "alice", "file": "foo.py", "body": "fix this"}]}'
        result = parse_repo_output(text)
        assert result["count"] == 2
        assert result["comments"][0]["id"] == "c1"

    def test_poll_ci_passing_json(self):
        text = '{"mode": "poll_ci", "status": "CHECKS PASSING", "details": ""}'
        result = parse_repo_output(text)
        assert result["status"] == "CHECKS PASSING"

    def test_poll_ci_failing_json(self):
        text = '{"mode": "poll_ci", "status": "CHECKS FAILING", "details": "CI/lint failed"}'
        result = parse_repo_output(text)
        assert result["status"] == "CHECKS FAILING"
        assert "lint" in result["details"]

    def test_fallback_checks_passing_text(self):
        result = parse_repo_output("All CI runs passed. CHECKS PASSING.")
        assert result["status"] == "CHECKS PASSING"

    def test_fallback_checks_failing_text(self):
        result = parse_repo_output("CHECKS FAILING: lint failed")
        assert result["status"] == "CHECKS FAILING"

    def test_fallback_no_actionable_comments(self):
        result = parse_repo_output("NO ACTIONABLE COMMENTS")
        assert result["status"] == "NO ACTIONABLE COMMENTS"
        assert result["comments"] == []

    def test_fallback_plain_diff(self):
        result = parse_repo_output("diff --git a/foo b/foo\n+new line")
        assert "diff" in result
