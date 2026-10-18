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

`needs` entries reference another task's id with a required `state` (`pushed` or `merged`) — that
`state` just records what this task's author *believes* it needs. Every `needs[].task` MUST
resolve to either another id in `fleet/tasks/` or a key in `fleet/external.json`; anything else is
a validation failure (there is no implicit "unknown id, trust it" fallback).

Two more optional fields: `notes` (free-text operational cautions a human wants attached, e.g. "must
not run concurrently with X" that isn't mechanically enforced — `fleet.py` never acts on these) and
`agent` (which herdr pane/CLI this task is meant to run under — `{pane, cli, fresh_context?}`; the
fuller per-pane CLI/fencing design lives in the historical `.fleet/panes.md` note referenced from
`docs/agent-fleet.md`'s "herdr pane" column).

## External work (`fleet/external.json`, `fleet/schemas/external.v1.json`)

The fleet's own hand-maintained registry of work that a `needs` can point to without that work
having its own file in `fleet/tasks/` — historical work that predates the fleet (e.g. `R2.9`), or
work tracked elsewhere entirely. Each entry has a `repo`, a `state` (`open` or `merged`), and
optionally `branch`, `pr`, `paths` and a free-text `note`.

**`state` is edited by hand, by a human, as real-world work actually lands.** `fleet.py plan`
reads it — never a need's own claim — to decide whether a dependent task is ready:

- A need pointing at an external item with `state: "merged"` is satisfied.
- A need pointing at an external item with `state: "open"` keeps the task **blocked on** that id
  until a human flips it.
- A need pointing at another task in `fleet/tasks/` is always reported as blocking for now —
  `fleet.py` has no `fleet/results/` tracking yet to know whether that sibling task has actually
  run, so it never claims one is done on another's behalf.

If an open external item declares `paths`, `fleet.py plan` also flags any fleet task in the same
`repo` whose `allowed_paths` overlaps them — work-in-flight outside the fleet that could collide
with a task the orchestrator is about to start, even though that task doesn't `need` it.

## Result files (`fleet/results/*.json`, `fleet/schemas/result.v1.json`)

Written by the orchestrator after a task finishes, never by the agent. Every fact — SHAs,
files changed, diff stats, acceptance exit codes, PR number — is something the orchestrator
observed directly (`git`, the acceptance commands it ran, the PR API), never copied from the
agent's own narration. `agent_report` is the one field that *is* the agent's words, and it is
documented as untrusted free text for exactly that reason.

Optional `herdr` field: which pane/CLI/worktree actually ran the task (`{pane, cli, worktree,
session_id?}`) — the executed-reality counterpart to the task file's optional `agent` field, which
only states intent. Omitted when the orchestrator doesn't track pane-level detail for a run.

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
2. Orchestrator runs `python tools/fleet/fleet.py plan` to get topological layers plus, per task,
   `ready` or `blocked on: <ids>`. Tasks in the same layer with the same `repo` whose
   `allowed_paths` overlap are printed as pairs that **must be serialized** — never run two such
   tasks as parallel agents even though the layer says they're both ready. Tasks whose
   `allowed_paths` overlap an *open* `fleet/external.json` item's declared `paths` in the same
   repo are flagged too, even with no `needs` edge between them.
3. For each task reported `ready`: orchestrator creates the worktree/branch, launches
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
python tools/fleet/fleet.py validate   # schema + ids + needs (incl. external.json) + acyclic +
                                        # path safety + prompt files exist
python tools/fleet/fleet.py plan       # topological layers + per-task ready/blocked-on +
                                        # required serializations + external path overlaps
```

Both default to the workspace root; pass `--base <dir>` to point at a different task set (used
by `tools/fleet/tests` to run the same checks against fixtures without touching
`fleet/tasks/`).
