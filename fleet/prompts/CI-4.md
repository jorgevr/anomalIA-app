# Fleet task CI-4: ingestion-func: reformat test_adls_store.py and gate CI on ruff format --check

Repo: services/ingestion-func (Python 3.11, Azure Functions v4)
Branch: `ci-4-ruff-format-ci-gate` (base: `main`)
Risk tier: ci | Turn budget: 20
(stop and report if you will not finish within the turn budget -- do not keep going past it)

## Read first
1. `AGENTS.md` (workspace root).
2. `services/ingestion-func/CLAUDE.md` (this repo's own commands).

## What you may touch
Only these paths, as globs relative to this repo's root -- nothing else, no matter
how related it looks:
- `tests/unit/test_adls_store.py`
- `.github/workflows/ci.yml`

Touching anything outside this list gets this task marked `rejected` even if every
check below passes.

## Dependencies
- `R2.9` must be **merged** before you rely on it. If it is not, stop and report -- do not try to work around it.

## Definition of done
Run every one of these yourself, in this exact cwd, before you stop -- the
orchestrator reruns them independently afterward and does not take your word for it:
- **format-check** (cwd `.`): `ruff format --check tests/unit/test_adls_store.py` -- must exit 0
- **ruff** (cwd `.`): `ruff check .` -- must exit 0
- **pytest** (cwd `.`): `pytest` -- must exit 0

## Operational notes
None.

## Hard rules (non-negotiable)
- Local commits only, on `ci-4-ruff-format-ci-gate`. Never push, never open a PR, never touch `main`.
- `.claude/**`, `.mcp.json`, `.agents/**`, `.codex/**` are hard-denied for every fleet task, regardless of what `allowed_paths`
  says -- never touch them, no exceptions.
- `.github/workflows/**` is hard-denied for every fleet task, with exactly one escape hatch: a file named verbatim (not by glob) in that task's own `allowed_paths`. This task's `allowed_paths` names `.github/workflows/ci.yml` verbatim, so that exact file -- and only that file -- may be touched; every other path under `.github/workflows/` stays off-limits.
- Never hand-edit a vendored `schemas/contracts/` copy, or anything under root `contracts/`
  directly -- that only happens via the Contract Owner role re-copying from root
  (AGENTS.md section 3).
- Never commit `.env`, `local.settings.json`, or Azurite runtime data.
- When you are completely done: stop. Do not ask questions, do not self-review, do
  not touch anything this task doesn't name.
