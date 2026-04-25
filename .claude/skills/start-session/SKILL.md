# start-session

Use this skill at the very beginning of every coding session on dj-segue, before writing or modifying any code. Its job is to load the project context and confirm what this session is for.

This is not optional. Sessions that skip orientation drift, duplicate work, or contradict prior decisions.

---

## Steps

### 1. Read the source-of-truth docs in this order

```
docs/architecture.md
docs/schema-v0.1.md     (or the latest schema version)
docs/milestones.md
docs/SESSION_LOG.md
```

If any of those don't exist, the project is in an unusual state — surface this to the user before proceeding.

### 2. Check the latest session entry

The bottom of `docs/SESSION_LOG.md` has the most recent session. Read:

- What was completed in the last session
- What the user committed to or planned for next
- Any open questions or blockers
- Any "do not do X" notes from prior sessions

### 3. Check the current state of the code

Run, in order:

- `git status` — uncommitted changes from before?
- `git log --oneline -10` — what was committed recently?
- If a test runner is set up: run the test suite. Note failures.
- If `pyproject.toml` exists: check for any dependency drift (you don't need to fix it, just note it).

### 4. State the session plan to the user

Output a brief summary, structured like this:

```
=== Session orientation ===

Current milestone: M<N> — <name>
Last session: <one-line summary from SESSION_LOG>
Open items from last session:
  - <item>
  - <item>

Planned for this session: <best inference, or "ask user">

Tests status: <passing | N failing | not yet set up>
Open questions for user: <anything ambiguous>

Ready to proceed when you confirm.
```

### 5. Wait for user confirmation before writing code

If the user says "go," begin. If the user changes scope, update your plan accordingly and confirm again.

---

## Anti-patterns to avoid

- **Don't read the docs and immediately start coding.** Always confirm scope with the user first.
- **Don't assume the milestone.** Read the session log; the user may have pivoted.
- **Don't skip checking tests.** Knowing what's broken before you start prevents new code from sitting on top of old breakage.
- **Don't introduce architectural changes mid-session without flagging them.** If you discover a problem with settled architecture, note it and ask before changing course.
