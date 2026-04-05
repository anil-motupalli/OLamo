## Task: Code Review

**Specification (for context only):**
{{plan}}

{{diff_section}}

Review for:
- Bugs (logic errors, null dereferences, race conditions)
- Security vulnerabilities (injection, insecure deserialization, secrets in code)
- Performance issues (O(n²) where O(n) is possible, unnecessary allocations)
- Code quality (dead code, overly complex logic, unclear variable names)

For every issue found, report: file, line, type, severity, description, suggested fix.
Conclude with APPROVED (no significant issues) or NEEDS IMPROVEMENT.
