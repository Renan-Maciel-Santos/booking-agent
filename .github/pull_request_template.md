## Summary

<!-- What does this PR change and why? -->

## Type of change

- [ ] feat: new feature
- [ ] fix: bug fix
- [ ] refactor: no behaviour change
- [ ] docs / ci / chore

## Architecture checklist

- [ ] Business rules live in `services/`, not in routers
- [ ] Database access only through `repositories/`, with parameterized queries
- [ ] The LLM only produces a validated `BookingIntent`; it never executes actions
- [ ] No hardcoded configuration or secrets (env vars via `Settings`)
- [ ] ADR added or updated if an architectural decision changed

## Testing

- [ ] Unit tests added/updated
- [ ] Integration tests added/updated (if DB behaviour changed)
- [ ] `ruff check`, `ruff format --check`, `mypy` and `pytest` pass locally

## Notes for reviewers

<!-- Trade-offs, follow-ups, screenshots or logs. -->
