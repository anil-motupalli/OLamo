"""GitHub CLI helpers: _run_gh and _pr_number_from_url."""

from __future__ import annotations

import json
import re
import subprocess


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
