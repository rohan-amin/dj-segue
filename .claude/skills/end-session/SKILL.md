# end-session

Use this skill at the end of every coding session on dj-segue, before the user wraps up. Its job is to capture state, update durable docs, and surface anything that needs the user's review before the session closes.

This is not optional. Sessions that don't end cleanly cause the next session to fly blind.

---

## Steps

### 1. Take stock of what changed

Run:

- `git status` — what's modified, what's new, what's staged?
- `git diff --stat` — scope of changes
- Run the test suite. Note results.

Build a mental list of:

- Features completed
- Tests added / passing / failing
- Schema or interface changes
- Dependency changes
- Files added or removed
- Architectural decisions made (or punted)

### 2. Update `docs/SESSION_LOG.md`

Append a new entry at the bottom (most recent at the bottom). Use this template:

```markdown
## Session: YYYY-MM-DD

**Milestone:** M<N> — <n>
**Duration:** approximately <N> turns
**Worked on:** <one-line summary>

### Completed
- <bullet>
- <bullet>

### Tests
- <added / passing / failing>

### Schema or interface changes
- <bullet, or "none">

### Dependencies added/removed
- <bullet, or "none">

### Open questions
- <bullet, or "none">

### Next session should
- <recommended next task>
- <any blockers>

### Notes for future sessions
- <any "watch out for X" or "don't do Y again" wisdom>
```

If `docs/SESSION_LOG.md` doesn't exist yet, create it with a brief header at the top:

```markdown
# dj-segue Session Log

Append-only log of coding sessions. Most recent at the bottom.

---
```

### 3. Propose doc updates if architecture or schema shifted

If anything settled in this session contradicts or supersedes a decision in `docs/architecture.md` or `docs/schema-v0.1.md`, **do not silently edit those docs.** Instead:

1. Note the discrepancy in your end-of-session summary.
2. Propose a specific edit (quote the existing text and the proposed replacement).
3. Ask the user to confirm before committing the doc change.

This protects the architecture from drifting unintentionally.

### 4. If schema changed, bump the version

If anything in `docs/schema-v0.1.md` was changed:

- Additive change (new optional field, new lane type): bump minor → `0.2`, add a "Migration from 0.1" section.
- Breaking change (removed field, semantic change): bump minor for now (since we're pre-1.0), document the migration prominently.
- Save the old doc as `docs/schema-v0.1.md` (kept as historical reference) and create `docs/schema-v0.2.md` (or whatever the new version is).

Don't do schema bumps without telling the user.

### 5. Make sure tests pass or document why they don't

If tests are failing at the end of the session, the next session needs to know:

- Which tests failed
- Whether the failure is expected (e.g., M1 acceptance test is supposed to fail until M1 is done) or unexpected
- Any partial fixes attempted

Put this in the session log under "Tests."

### 6. Output an end-of-session summary to the user

Format:

```
=== Session wrap-up ===

Completed:
  - <bullet>

In progress / partial:
  - <bullet>

Tests: <summary>

Next session should: <one-line recommendation>

Doc updates needed (require your approval):
  - <bullet, or "none">

Schema version: <unchanged | bumped to X.Y>

Anything else to flag: <bullet, or "none">

OK to wrap?
```

### 7. Wait for user approval before committing changes

Especially for doc updates. If the user says "ship it," commit; otherwise iterate based on their feedback.

---

## Anti-patterns to avoid

- **Don't silently rewrite architecture.md or schema docs.** Propose, then edit on approval.
- **Don't end without updating the session log.** The session log is the only durable bridge between sessions.
- **Don't claim work is "done" if tests are failing.** Mark it accurately; "implemented but X test failing" is honest, "done" is not.
- **Don't bump the schema version without telling the user.** Schema versions are public-facing and need to be intentional.
- **Don't skip the wrap-up summary.** Even a 30-second summary is enormously valuable for the next session.
