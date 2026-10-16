#!/usr/bin/env python3
"""Validate fleet task files and plan their execution order (FLEET-1).

Two subcommands:

  validate  — every fleet/tasks/*.json file is schema-valid against
              fleet/schemas/task.v1.json; task ids are unique; every `needs` entry
              either names another task in the set or is an explicit external marker
              (a task id not in the set, accepted only with state "merged" — the
              orchestrator has no record to verify an external "pushed" claim
              against); the needs graph (over known tasks) is acyclic; every
              prompt_file exists.
  plan      — topological layers over the needs graph, then for tasks that land in
              the same layer and target the same repo, whether their allowed_paths
              globs overlap. Overlapping pairs in the same repo/layer cannot run as
              parallel agents and must be serialized.

Reuses the Draft 2020-12 approach of scripts/check-contracts.py: one Report table,
every row printed before deciding pass/fail, non-zero exit on FAIL.
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

ROOT = Path(__file__).resolve().parent.parent.parent
TASK_SCHEMA_PATH = ROOT / "fleet" / "schemas" / "task.v1.json"

META_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


class Report:
    """Collects one row per check so the run always prints a full table."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, bool, str]] = []

    def add(self, check: str, subject: str, passed: bool, note: str = "") -> None:
        self.rows.append((check, subject, passed, note))

    @property
    def ok(self) -> bool:
        return all(passed for _, _, passed, _ in self.rows)

    def print(self) -> None:
        check_w = max([len("CHECK")] + [len(r[0]) for r in self.rows])
        subj_w = max([len("SUBJECT")] + [len(r[1]) for r in self.rows])
        print(f"{'CHECK':<{check_w}}  {'SUBJECT':<{subj_w}}  RESULT  NOTE")
        print(f"{'-' * check_w}  {'-' * subj_w}  ------  ----")
        for check, subject, passed, note in self.rows:
            result = "PASS" if passed else "FAIL"
            print(f"{check:<{check_w}}  {subject:<{subj_w}}  {result:<6}  {note}")
        failed = sum(1 for _, _, passed, _ in self.rows if not passed)
        print()
        print(f"{len(self.rows)} checks, {len(self.rows) - failed} passed, {failed} failed")


def load_json(path: Path) -> tuple[dict | list | None, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except OSError as exc:
        return None, f"unreadable: {exc.strerror}"
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"


def load_task_schema() -> Draft202012Validator:
    doc, err = load_json(TASK_SCHEMA_PATH)
    if doc is None:
        raise SystemExit(f"cannot load {TASK_SCHEMA_PATH}: {err}")
    if doc.get("$schema") != META_SCHEMA:
        raise SystemExit(f"{TASK_SCHEMA_PATH} does not declare {META_SCHEMA!r}")
    try:
        Draft202012Validator.check_schema(doc)
    except SchemaError as exc:
        raise SystemExit(f"{TASK_SCHEMA_PATH} is not a valid Draft 2020-12 schema: {exc.message}")
    return Draft202012Validator(doc)


def task_files(tasks_dir: Path) -> list[Path]:
    return sorted(tasks_dir.glob("*.json"))


class Task:
    def __init__(self, path: Path, doc: dict) -> None:
        self.path = path
        self.doc = doc
        self.id: str = doc["id"]
        self.repo: str = doc["repo"]
        self.base: str = doc.get("base", "main")
        self.needs: list[dict] = doc.get("needs", [])
        self.allowed_paths: list[str] = doc.get("allowed_paths", [])
        self.prompt_file: str = doc.get("prompt_file", "")
        self.branch: str = doc.get("branch", "")


def load_tasks(report: Report, tasks_dir: Path, validator: Draft202012Validator) -> dict[str, Task]:
    """Schema-validate every file; return id -> Task for files that at least declare an id.

    Duplicate ids are reported here (not folded into a single entry) so a later file
    that reuses an earlier id does not silently shadow it in the needs graph.
    """
    tasks: dict[str, Task] = {}
    seen_at: dict[str, Path] = {}
    had_duplicate = False

    files = task_files(tasks_dir)
    if not files:
        report.add("schema", str(tasks_dir), False, "no task files found")
        return tasks

    for path in files:
        subject = path.name
        doc, err = load_json(path)
        if doc is None:
            report.add("schema", subject, False, err)
            continue
        if not isinstance(doc, dict):
            report.add("schema", subject, False, "task file must be a JSON object")
            continue

        errors = sorted(validator.iter_errors(doc), key=lambda e: e.json_path)
        if errors:
            first = errors[0]
            report.add(
                "schema",
                subject,
                False,
                f"{len(errors)} error(s): {first.json_path}: {first.message}",
            )
            continue

        report.add("schema", subject, True, "matches task.v1.json")

        task_id = doc["id"]
        if task_id in seen_at:
            had_duplicate = True
            report.add(
                "ids",
                task_id,
                False,
                f"duplicate id: also defined in {seen_at[task_id].name}",
            )
            continue
        seen_at[task_id] = path
        tasks[task_id] = Task(path, doc)

    if not had_duplicate and tasks:
        report.add("ids", "all task ids", True, f"{len(tasks)} unique id(s)")

    return tasks


def check_needs(report: Report, tasks: dict[str, Task]) -> None:
    known = set(tasks)
    for task in tasks.values():
        for need in task.needs:
            need_id = need.get("task")
            need_state = need.get("state")
            subject = f"{task.id} needs {need_id}"
            if need_id in known:
                report.add("needs", subject, True, f"resolved task, state={need_state}")
            elif need_state == "merged":
                report.add("needs", subject, True, "external marker, trusted as already merged")
            else:
                report.add(
                    "needs",
                    subject,
                    False,
                    f"'{need_id}' is not a known task and state is {need_state!r}, not 'merged' "
                    "-- an unresolvable task can only be trusted as an already-merged external marker",
                )


def _find_cycle(tasks: dict[str, Task]) -> list[str] | None:
    """DFS over edges task -> known need, reporting the first cycle found."""
    known = set(tasks)
    WHITE, GREY, BLACK = 0, 1, 2
    color = {task_id: WHITE for task_id in tasks}
    stack: list[str] = []

    def visit(task_id: str) -> list[str] | None:
        color[task_id] = GREY
        stack.append(task_id)
        for need in tasks[task_id].needs:
            need_id = need.get("task")
            if need_id not in known:
                continue
            if color[need_id] == GREY:
                cycle_start = stack.index(need_id)
                return stack[cycle_start:] + [need_id]
            if color[need_id] == WHITE:
                found = visit(need_id)
                if found:
                    return found
        stack.pop()
        color[task_id] = BLACK
        return None

    for task_id in tasks:
        if color[task_id] == WHITE:
            found = visit(task_id)
            if found:
                return found
    return None


def check_acyclic(report: Report, tasks: dict[str, Task]) -> None:
    cycle = _find_cycle(tasks)
    if cycle is None:
        report.add("graph", "needs graph", True, f"acyclic over {len(tasks)} task(s)")
    else:
        report.add("graph", "needs graph", False, "cycle: " + " -> ".join(cycle))


def check_prompt_files(report: Report, tasks: dict[str, Task], base: Path) -> None:
    for task in tasks.values():
        prompt_path = base / task.prompt_file
        subject = f"{task.id}: {task.prompt_file}"
        if prompt_path.is_file():
            report.add("prompt", subject, True, "exists")
        else:
            report.add("prompt", subject, False, "prompt_file does not exist")


def run_validate(base: Path) -> int:
    tasks_dir = base / "fleet" / "tasks"
    validator = load_task_schema()
    report = Report()

    tasks = load_tasks(report, tasks_dir, validator)
    check_needs(report, tasks)
    check_acyclic(report, tasks)
    check_prompt_files(report, tasks, base)

    report.print()
    if report.ok:
        print("\nOK: all task files valid.")
        return 0
    print("\nFAILED: see the rows marked FAIL above.")
    return 1


# --- glob overlap, for `plan` -------------------------------------------------------


@lru_cache(maxsize=None)
def _segment_compatible(a: str, b: str) -> bool:
    """Whether two single-path-segment globs (only '*' wildcards) can match one string."""

    @lru_cache(maxsize=None)
    def rec(i: int, j: int) -> bool:
        if i == len(a) and j == len(b):
            return True
        if i < len(a) and a[i] == "*":
            return rec(i + 1, j) or (j < len(b) and rec(i, j + 1))
        if j < len(b) and b[j] == "*":
            return rec(i, j + 1) or (i < len(a) and rec(i + 1, j))
        if i < len(a) and j < len(b) and a[i] == b[j]:
            return rec(i + 1, j + 1)
        return False

    return rec(0, 0)


def _segments_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Whether two sequences of path segments (globs may use '*' or '**') can both
    match the same path. '**' matches zero or more whole segments."""
    if not a and not b:
        return True
    if not a:
        return b[0] == "**" and _segments_overlap(a, b[1:])
    if not b:
        return a[0] == "**" and _segments_overlap(a[1:], b)
    if a[0] == "**":
        return _segments_overlap(a[1:], b) or _segments_overlap(a, b[1:])
    if b[0] == "**":
        return _segments_overlap(a, b[1:]) or _segments_overlap(a[1:], b)
    if not _segment_compatible(a[0], b[0]):
        return False
    return _segments_overlap(a[1:], b[1:])


def globs_overlap(glob_a: str, glob_b: str) -> bool:
    return _segments_overlap(tuple(glob_a.split("/")), tuple(glob_b.split("/")))


def find_overlapping_paths(paths_a: list[str], paths_b: list[str]) -> list[tuple[str, str]]:
    return [(pa, pb) for pa in paths_a for pb in paths_b if globs_overlap(pa, pb)]


def compute_layers(tasks: dict[str, Task]) -> dict[str, int]:
    """Longest-path layering: a task's layer is one past the deepest known dependency."""
    known = set(tasks)
    layer: dict[str, int] = {}

    def layer_of(task_id: str) -> int:
        if task_id in layer:
            return layer[task_id]
        known_deps = [n["task"] for n in tasks[task_id].needs if n["task"] in known]
        result = 0 if not known_deps else 1 + max(layer_of(dep) for dep in known_deps)
        layer[task_id] = result
        return result

    for task_id in tasks:
        layer_of(task_id)
    return layer


def run_plan(base: Path) -> int:
    tasks_dir = base / "fleet" / "tasks"
    validator = load_task_schema()
    report = Report()
    tasks = load_tasks(report, tasks_dir, validator)
    check_needs(report, tasks)
    check_acyclic(report, tasks)
    check_prompt_files(report, tasks, base)
    if not report.ok:
        report.print()
        print("\nFAILED: fix the task files above before planning (run `validate` for detail).")
        return 1

    layers = compute_layers(tasks)
    max_layer = max(layers.values(), default=-1)

    print(f"{len(tasks)} task(s), {max_layer + 1} layer(s)\n")

    serialize_pairs: list[tuple[str, str, str, str]] = []
    for layer_num in range(max_layer + 1):
        layer_tasks = sorted((t for t in tasks.values() if layers[t.id] == layer_num), key=lambda t: t.id)
        print(f"Layer {layer_num}:")
        for task in layer_tasks:
            print(f"  {task.id:<14} repo={task.repo:<16} branch={task.branch}")

        by_repo: dict[str, list[Task]] = {}
        for task in layer_tasks:
            by_repo.setdefault(task.repo, []).append(task)

        for repo, repo_tasks in sorted(by_repo.items()):
            for i in range(len(repo_tasks)):
                for j in range(i + 1, len(repo_tasks)):
                    t1, t2 = repo_tasks[i], repo_tasks[j]
                    overlaps = find_overlapping_paths(t1.allowed_paths, t2.allowed_paths)
                    for glob_a, glob_b in overlaps:
                        serialize_pairs.append((t1.id, t2.id, glob_a, glob_b))
        print()

    if serialize_pairs:
        print("Must serialize (same repo, same layer, overlapping allowed_paths):")
        for t1, t2, glob_a, glob_b in serialize_pairs:
            print(f"  {t1} <-> {t2}: '{glob_a}' overlaps '{glob_b}'")
    else:
        print("No same-repo/same-layer path overlaps: every layer can run fully in parallel.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        type=Path,
        default=ROOT,
        help="Workspace root containing fleet/tasks and fleet/prompts (default: repo root).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="Validate every task file.")
    sub.add_parser("plan", help="Print topological layers and required serializations.")
    args = parser.parse_args()

    base = args.base.resolve()
    if args.cmd == "validate":
        return run_validate(base)
    if args.cmd == "plan":
        return run_plan(base)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
