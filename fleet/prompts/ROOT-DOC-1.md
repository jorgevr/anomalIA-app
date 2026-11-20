# Fleet task ROOT-DOC-1: root: document the R2.x contract-validation rollout

Repo: workspace root
Branch: `root-doc-1-contract-migration-update` (base: `main`)
Risk tier: docs | Model: A (Tier A -- Frontier Reasoning (docs/agent-fleet.md section 2): premium cloud models (Claude 3.5 Sonnet / Claude Pro / GPT-4o). For architecture, task decomposition, contract ownership, complex cross-repo analysis.) | Turn budget: 15
(stop and report if you will not finish within the turn budget -- do not keep going past it)

## Read first
1. `AGENTS.md` (workspace root).

## What you may touch
Only these paths, as globs relative to this repo's root -- nothing else, no matter
how related it looks:
- `docs/adr/*`
- `docs/contracts.md`
- `docs/contract-migration.md`
- `contracts/examples/**`

Touching anything outside this list gets this task marked `rejected` even if every
check below passes.

## Dependencies
- `R2.9` must be **merged** before you rely on it. If it is not, stop and report -- do not try to work around it.

## Definition of done
Run every one of these yourself, in this exact cwd, before you stop -- the
orchestrator reruns them independently afterward and does not take your word for it:
- **contracts-check** (cwd `.`): `python scripts/check-contracts.py` -- must exit 0

## Operational notes
None.

## Hard rules (non-negotiable)
- Local commits only, on `root-doc-1-contract-migration-update`. Never push, never open a PR, never touch `main`.
- `.claude/**`, `.mcp.json`, `.agents/**`, `.codex/**` are hard-denied for every fleet task, regardless of what `allowed_paths`
  says -- never touch them, no exceptions.
- `.github/workflows/**` is hard-denied for this task -- its `allowed_paths` does not name any workflow file verbatim, so nothing under `.github/workflows/` may be touched, no matter how related it looks.
- Never hand-edit a vendored `schemas/contracts/` copy, or anything under root `contracts/`
  directly -- that only happens via the Contract Owner role re-copying from root
  (AGENTS.md section 3).
- Never commit `.env`, `local.settings.json`, or Azurite runtime data.
- When you are completely done: stop. Do not ask questions, do not self-review, do
  not touch anything this task doesn't name.
