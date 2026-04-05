## Task: Poll CI Checks

**Branch:** {{branch}}

1. `gh run list --branch {{branch}} --limit 10`
2. If any run is `in_progress` or `queued`, wait 30 seconds and retry (up to 6 retries, ~3 minutes).
3. Once all runs have settled:
   - All succeeded (or no runs): reply "CHECKS PASSING"
   - Any failed: reply "CHECKS FAILING: <list each failed check and its error>"
     Get details with: `gh run view <run-id> --log-failed`
