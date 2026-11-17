#!/usr/bin/env python3
"""Generate a fleet/prompts/<id>.md kickoff prompt from its fleet/tasks/<id>.json
task file (FLEET-1/FLEET-2, docs/fleet.md).

tools/fleet/runner.py hands the agent exactly one message: "Read <prompt_path>
and follow it exactly from start to finish." (send_kickoff_prompt) -- the prompt
file is the agent's entire brief, so this fills a task's "paste prompt here"
placeholder deterministically from its own schema-validated fields
(allowed_paths, acceptance, needs, notes, risk, model, max_turns) instead of a
human retyping them by hand and risking drift from the task file fleet.py and
the runner actually enforce.

Supersedes the old scripts/gen-coordinator-prompt.sh, which generated a prompt
for a hand-rolled "Orca Coordinator" issuing `orca orchestration ...` CLI calls
that nothing else in this repo calls -- the real, running orchestrator is
tools/fleet/fleet.py + tools/fleet/runner.py (driving `herdr` + `claude` + `gh`
directly). This generator feeds that system's own prompt_file slot instead of a
parallel one.

Exit status: 0 on success, 1 on any failure (missing/invalid task file, a
refused overwrite, or `fleet.py validate` failing).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = ROOT / "fleet" / "tasks"
FLEET_PY = ROOT / "tools" / "fleet" / "fleet.py"
# Same default and env var as tools/fleet/runner.py's DEFAULT_FLEET_HOME, so the
# mirror below lands exactly where write_outbox_prompt() will look -- and will
# overwrite -- the next time runner.py actually starts or resumes this task.
FLEET_HOME = Path(os.environ.get("FLEET_HOME", r"C:\code\_fleet"))

PLACEHOLDER = "paste prompt here\n"

REPO_LABELS = {
    "root": "workspace root",
    "ingestion-func": "services/ingestion-func (Python 3.11, Azure Functions v4)",
    "processing-func": "services/processing-func (.NET 10 Isolated Worker)",
    "sandbox": "jorgevr/fleet-sandbox (disposable -- never root/ingestion-func/processing-func)",
}

REQUIRED_FIELDS = (
    "id",
    "title",
    "repo",
    "branch",
    "needs",
    "allowed_paths",
    "acceptance",
    "prompt_file",
    "model",
    "max_turns",
    "risk",
)


def load_task(task_id: str) -> dict:
    path = TASKS_DIR / f"{task_id}.json"
    if not path.is_file():
        sys.exit(f"no such task file: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path}: not valid JSON: {exc}")
    missing = [f for f in REQUIRED_FIELDS if f not in doc]
    if missing:
        sys.exit(f"{path}: missing required field(s) {missing} -- run fleet.py validate")
    if doc["id"] != task_id:
        sys.exit(f"{path} declares id {doc['id']!r}, expected {task_id!r}")
    return doc


def validate_registry() -> None:
    result = subprocess.run([sys.executable, str(FLEET_PY), "validate"], cwd=ROOT)
    if result.returncode != 0:
        sys.exit(
            "tools/fleet/fleet.py validate failed (see above) -- fix the task "
            "registry before generating a prompt from it."
        )


def render(task: dict) -> str:
    tid = task["id"]
    repo = task["repo"]

    read_items = ["`AGENTS.md` (workspace root)."]
    if repo in ("ingestion-func", "processing-func"):
        read_items.append(f"`services/{repo}/CLAUDE.md` (this repo's own commands).")
    read_items.append(
        f"`fleet/tasks/{tid}.json` (this task's full, schema-validated definition -- "
        "the lists below are copied from it verbatim; if anything here looks "
        "inconsistent with that file, trust the file and stop)."
    )
    read_block = "\n".join(f"{i}. {item}" for i, item in enumerate(read_items, start=1))

    needs_block = (
        "\n".join(
            f"- `{n['task']}` must be **{n['state']}** before you rely on it. "
            "If it is not, stop and report -- do not try to work around it."
            for n in task["needs"]
        )
        or "None."
    )
    notes_block = "\n".join(f"- {n}" for n in task.get("notes", [])) or "None."
    allowed_block = "\n".join(f"- `{p}`" for p in task["allowed_paths"])
    acceptance_block = "\n".join(
        f"- **{c['name']}** (cwd `{c['cwd']}`): `{c['command']}` -- must exit {c['expect_exit']}"
        for c in task["acceptance"]
    )

    return f"""# Fleet task {tid}: {task['title']}

Repo: {REPO_LABELS.get(repo, repo)}
Branch: `{task['branch']}` (base: `{task.get('base', 'main')}`)
Risk tier: {task['risk']} | Model: {task['model']} | Turn budget: {task['max_turns']}
(stop and report if you will not finish within the turn budget -- do not keep going past it)

## Read first
{read_block}

## What you may touch
Only these paths, as globs relative to this repo's root -- nothing else, no matter
how related it looks:
{allowed_block}

Touching anything outside this list gets this task marked `rejected` even if every
check below passes.

## Dependencies
{needs_block}

## Definition of done
Run every one of these yourself, in this exact cwd, before you stop -- the
orchestrator reruns them independently afterward and does not take your word for it:
{acceptance_block}

## Operational notes
{notes_block}

## Hard rules (non-negotiable)
- Local commits only, on `{task['branch']}`. Never push, never open a PR, never touch `main`.
- Never touch `.claude/**`, `.mcp.json`, or `.github/workflows/**` -- hard-denied for every
  fleet task regardless of what `allowed_paths` says (docs/fleet.md "Operational rules").
- Never hand-edit a vendored `schemas/contracts/` copy, or anything under root `contracts/`
  directly -- that only happens via the Contract Owner role re-copying from root
  (AGENTS.md section 3).
- Never commit `.env`, `local.settings.json`, or Azurite runtime data.
- When you are completely done: stop. Do not ask questions, do not self-review, do
  not touch anything this task doesn't name.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task_id", help="Task id, e.g. R2.11 (reads fleet/tasks/<id>.json)")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing non-placeholder prompt file")
    parser.add_argument("--stdout", action="store_true", help="Print the prompt instead of writing it to disk")
    parser.add_argument("--skip-validate", action="store_true", help="Skip running `fleet.py validate` first")
    args = parser.parse_args()

    task = load_task(args.task_id)
    if not args.skip_validate:
        validate_registry()

    prompt_text = render(task)

    if args.stdout:
        print(prompt_text)
        return 0

    out_path = ROOT / task["prompt_file"]
    if out_path.is_file() and out_path.read_text(encoding="utf-8") != PLACEHOLDER and not args.force:
        sys.exit(
            f"{out_path} already has content and is not the placeholder -- "
            "pass --force to overwrite, or --stdout to review first."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(prompt_text, encoding="utf-8")
    print(f"Wrote {out_path}")

    # Mirror into the fleet-home outbox so a manually-started herdr agent can be
    # pointed at it right now. This copy is always overwritten unconditionally --
    # it is exactly what runner.py's write_outbox_prompt() would (re)produce from
    # fleet/prompts/<id>.md the next time this task actually starts or resumes,
    # so there is nothing here for --force to protect.
    outbox_path = FLEET_HOME / "outbox" / task["id"] / "prompt.md"
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    outbox_path.write_text(prompt_text, encoding="utf-8")
    print(f"Wrote {outbox_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
