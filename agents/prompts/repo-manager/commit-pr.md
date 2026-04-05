## Task: Commit and Create Pull Request

**Branch:** {{branch}}
**PR Title:** {{pr_title}}
**PR Description:** {{pr_description}}

Steps:
1. `git add -A`
2. `git commit -m "{{pr_title}}"`
3. `git push -u origin {{branch}}`
4. Create the PR using `gh pr create` with the title and description above
5. Report the PR URL and PR number
6. Run `git diff origin/main...{{branch}}` and return the full diff output
