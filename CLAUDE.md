# Instructions for Claude

- Be token-efficient in output. Don't narrate tool calls (Write, Edit, Update, Bash, etc.) or restate file contents back to the user. No "I'll update X" / "Updating..." lead-ins before a tool call either — just do it.
- After making changes, just report done/not done concisely — no walkthroughs of what was written.
- Do not run tests or verification commands unless the user explicitly asks for it.
