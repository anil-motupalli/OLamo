## Task: Poll PR Comments

**PR URL:** {{pr_url}}
{{exclude_section}}

1. Fetch all open review comments: `gh pr view --comments`
2. Filter for unresolved, actionable comments (exclude bot comments and resolved threads)
3. List each remaining comment with: ID, author, file (if applicable), and body
4. Conclude: "ACTIONABLE COMMENTS FOUND: N" or "NO ACTIONABLE COMMENTS"
