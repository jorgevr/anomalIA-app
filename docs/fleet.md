# Fleet orchestration — contract layer (FLEET-1)

This describes the files that let an orchestrator hand out tasks to agents and record what
actually happened, and the tool (`tools/fleet/fleet.py`) that validates them before any task
runs. It assumes `docs/agent-fleet.md` (roles, model tiers) and `AGENTS.md` (repo map,
boundaries); this page is the operational layer underneath both — task/result file shapes and
the orchestrator loop, not role design.

## Roles

- **Orchestrator** — not an LLM. A script/process that reads `fleet/tasks/*.json`, starts
  agents in their own worktree/branch, runs the acceptance commands itself, writes
  `fleet/results/<task-id>.json`, pushes branches and opens PRs. It is the only thing that
  writes a result file, and the only thing that pushes.
- **Agent** — runs one task, inside one worktree, on one branch (`AGENTS.md` §5). It commits
  **locally only**; it never pushes and never opens a PR. Its own account of what it did
  (`agent_report` in the result file) is recorded but never trusted as fact — see "Untrusted
  narrators" below.
- **Human** — merges. No result's `status` field, however it reads, merges anything by itself.

## Task files (`fleet/tasks/*.json`, `fleet/schemas/task.v1.json`)

One task = one repo (`root`, `ingestion-func`, or `processing-func`) + one branch + one agent.
A task file is the complete brief: which repo and base branch, what it depends on (`needs`),
exactly which paths it may touch (`allowed_paths`, globs relative to its repo, never `..` or
absolute), which commands the orchestrator will run to accept it (`acceptance`), where its
prompt lives (`prompt_file`, a placeholder until a human fills it in), and the model/turn
budget/risk tier it runs under.

`needs` entries reference another task's id with a required `state` (`pushed` or `merged`). A
`task` id that is not in `fleet/tasks/` is treated as an **explicit external marker** — work that
predates the fleet or was merged outside it (e.g. `R2.9`) — and is trusted only when its state is
`merged`; an unresolvable `pushed` claim has nothing behind it and is a validation failure.

## Result files (`fleet/results/*.json`, `fleet/schemas/result.v1.json`)

Written by the orchestrator after a task finishes, never by the agent. Every fact — SHAs,
files changed, diff stats, acceptance exit codes, PR number — is something the orchestrator
observed directly (`git`, the acceptance commands it ran, the PR API), never copied from the
agent's own narration. `agent_report` is the one field that *is* the agent's words, and it is
documented as untrusted free text for exactly that reason.

`status` is one of:

| Status | Meaning |
| :--- | :--- |
| `succeeded` | Acceptance passed, no `allowed_paths` violation, review (if run) passed. |
| `failed` | The agent ran, but acceptance or review did not pass. |
| `rejected` | The agent touched paths outside `allowed_paths`, or another untrusted-narrator check failed. |
| `blocked` | A `needs` dependency was not satisfied, or `fleet/STOP` was present, before the task could start. |

## The loop

1. Orchestrator runs `python tools/fleet/fleet.py validate`. Any `FAIL` stops everything —
   nothing starts on a task set that doesn't check out.
2. Orchestrator runs `python tools/fleet/fleet.py plan` to get topological layers. Tasks in the
   same layer with the same `repo` whose `allowed_paths` overlap are printed as pairs that
   **must be serialized** — never run two such tasks as parallel agents even though the layer
   says they're both ready.
3. For each task whose `needs` are satisfied: orchestrator creates the worktree/branch, launches
   the agent with its `prompt_file`, caps it at `max_turns`.
4. Agent commits locally and hands back. It does not push, does not open a PR, does not decide
   its own status.
5. Orchestrator runs every command in `acceptance` itself, diffs the branch against
   `allowed_paths`, writes `fleet/results/<id>.json`, and only then pushes the branch and opens
   the PR (if the result warrants one).
6. A human reviews and merges. Merging is never automatic from a result file alone.

## Operational rules

- **One docker stack at a time** (`AGENTS.md` §6): the orchestrator must not run acceptance
  commands for two tasks that both bring up `docker-compose.yml` concurrently on this machine.
  Serialize those acceptance runs regardless of what `plan` says about the task graph itself.
- **Kill switch:** if `fleet/STOP` exists, the orchestrator starts no new tasks. Tasks already
  running are left to finish or be cancelled by a human; nothing new picks up until the file is
  removed.
- **Agents are untrusted narrators.** An agent's `agent_report`, its claimed test results, its
  claimed file list — none of it is a result until the orchestrator has independently observed
  the same thing via git and the acceptance commands. This is why `result.v1.json` has no field
  where the agent's own verdict could stand in for `status`.

## Validating and planning

```
python tools/fleet/fleet.py validate   # schema + ids + needs + acyclic + prompt files exist
python tools/fleet/fleet.py plan       # topological layers + required serializations
```

Both default to the workspace root; pass `--base <dir>` to point at a different task set (used
by `tools/fleet/tests` to run the same checks against fixtures without touching
`fleet/tasks/`).
