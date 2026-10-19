---
name: delivery-report
description: Pre-push checklist and evidence-based report format for finishing any task. Use this skill at the end of every task that changed files, before any commit, push or pull request, and whenever you are asked for a report, status, summary or "relatório" — so every claim is backed by evidence from the code or literal command output.
---

# Delivery report

The reviewer reads the report and then checks the code. Every mismatch between the
two costs a review cycle and trust. The report must therefore contain only claims
that are backed by evidence.

## Rules for claims

- Separate **done** (you changed it) from **verified** (you ran something that proves
  it). Never present an unverified change as verified.
- Every claim about code cites the file and the relevant snippet or line range.
- Command output is pasted literally. Do not paraphrase, summarize or "clean up"
  output that is presented as literal. If output is long, paste the relevant part and
  say what was omitted.
- When something was asked but not done, or done differently, say so explicitly at
  the top of the report with the reason.
- When you find a bug outside the scope of the task, report it and propose a fix;
  do not fix it silently.
- Before claiming something was removed or moved, search the whole repository for it
  (e.g. `git grep`) and show the result.

## Pre-push checklist

Run and report each item:

1. `git status` — working tree clean, correct branch.
2. `git log --format="%h %ae | %s" main..HEAD` — Conventional Commits, author e-mail
   is the GitHub noreply address.
3. `uv run pre-commit run --all-files` — all hooks pass.
4. Full test suite with the database available, run twice; report totals per level
   and how many integration tests ran.
5. If the README or Quick Start changed: run the documented commands in order on a
   clean environment and report each result.
6. README and docs contain no claim the code and tests do not support (no
   "production-grade", "100%", "guarantees" without proof).
7. No agent logs, status files, `.env` or secrets are tracked (`git ls-files`).
8. Push without `--force`. Never use bypass or admin flags on protected branches.
   Never merge unless explicitly told to.

## Report template

```markdown
## Summary
One paragraph: what changed and why.

## Deviations
Anything requested that was not done or was done differently, with the reason.
(Write "None" if there are none.)

## Changes
- `<commit hash> <message>` — what it does, with file references.

## Verification
- pre-commit: <literal summary>
- tests: <unit N passed, integration N passed (N ran against PostgreSQL), skipped N>
- other checks: <literal output>

## Evidence for key claims
- Claim → `path/to/file.py:L10-L25` (snippet)

## Open issues and proposals
- Bugs found outside scope, risks, follow-ups.
```
