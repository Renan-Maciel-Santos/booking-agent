---
name: git-workflow
description: Branching, commit, pull request, merge and cleanup rules for repositories with a protected main branch and CI. Use this skill whenever you create a branch, commit, amend, rebase, push, open or merge a pull request, delete branches, or fix history — and before running any git command that rewrites or deletes something.
---

# Git workflow

The history is part of the portfolio. It should read as small, reviewed, green steps.

## Branches and commits

- Never commit directly to `main`; `main` is protected and requires a PR and green
  CI.
- Start every branch from an up-to-date `main` (`git checkout main && git pull`).
- One branch per unit of work; names like `feat/...`, `fix/...`, `docs/...`,
  `chore/...`, `refactor/...`, `test/...`.
- Conventional Commits in English (`feat(scope): ...`). One logical change per
  commit, and every commit passes pre-commit and unit tests on its own.
- Author e-mail must be the GitHub noreply address. Check `git config user.email`
  before the first commit on a new machine or repository.

## Rewriting history

- You may rewrite only commits that were never pushed. Use `git commit --fixup <hash>`
  and `git rebase -i --autosquash main` to fold corrections into the right commit.
- Before pushing, confirm the remote branch is an ancestor of your branch:
  `git merge-base --is-ancestor origin/<branch> HEAD` must succeed.
- Never use `--force`. If a push is rejected, stop and report the message.
- Never rewrite `main` or published history.

## Pull requests

- Open with `gh pr create --base main --assignee @me`, a Conventional Commit style
  title and a body with summary, testing and security considerations. Reference
  issues with `Closes #N` when appropriate.
- Wait for CI with `gh pr checks --watch`. Do not merge unless explicitly told to.
- Merge strategy:
  - Single branch: "Rebase and merge" to keep a linear history.
  - Stacked branches (each built on the previous one): "Create a merge commit", in
    order, because rebase merges change hashes and break the next PR in the stack.
- Never use bypass or admin options to get past branch protection.

## After merge

- `git checkout main && git pull && git fetch --prune`.
- Before deleting a local branch that was rebase-merged, prove its changes are in
  `main`: `git cherry main <branch>` must show only `-` lines (or
  `git diff main <branch> --stat` must be empty). Only then use
  `git branch -D <branch>`.
- Delete leftover remote branches that are fully merged.

## Working on two machines

- `git pull` before starting, push the branch when stopping.
- Never work on the same branch on two machines without passing through the remote.
