#!/usr/bin/env python3
"""Validate fleet task files and plan their execution order (FLEET-1, FLEET-1 follow-up).

Two subcommands:

  validate  — every fleet/tasks/*.json file is schema-valid against
              fleet/schemas/task.v1.json; fleet/external.json (if present) is
              schema-valid against fleet/schemas/external.v1.json; task ids are
              unique; every `needs` entry resolves to a fleet/tasks/ id or a
              fleet/external.json key (no implicit "unknown id passes"); the
              needs graph (over known tasks only) is acyclic; every allowed_paths/
              prompt_file/external paths entry is a safe relative path (no '..',
              no absolute/drive prefix); every prompt_file exists.
  plan      — topological layers over the needs graph; per task, "ready" or
              "blocked on: <ids>" (external readiness comes only from
              fleet/external.json's own hand-edited `state`, never from a need's
              own claim); for tasks in the same layer and repo, whether their
              allowed_paths overlap (must be serialized); and whether any task's
              allowed_paths overlaps an *open* external item's declared paths in
              the same repo.

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
EXTERNAL_SCHEMA_PATH = ROOT / "fleet" / "schemas" / "external.v1.json"

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


def _load_schema_validator(path: Path) -> Draft202012Validator:
    doc, err = load_json(path)
    if doc is None:
        raise SystemExit(f"cannot load {path}: {err}")
    if doc.get("$schema") != META_SCHEMA:
        raise SystemExit(f"{path} does not declare {META_SCHEMA!r}")
    try:
        Draft202012Validator.check_schema(doc)
    except SchemaError as exc:
        raise SystemExit(f"{path} is not a valid Draft 2020-12 schema: {exc.message}")
    return Draft202012Validator(doc)


def load_task_schema() -> Draft202012Validator:
    return _load_schema_validator(TASK_SCHEMA_PATH)


def load_external_schema() -> Draft202012Validator:
    return _load_schema_validator(EXTERNAL_SCHEMA_PATH)


def task_files(tasks_dir: Path) -> list[Path]:
    return sorted(tasks_dir.glob("*.json"))


class Task:
    def __init__(self, path: Path, doc: dict) -> None:
        self.path = path
        self.doc = doc
        self.id: str = doc.get("id", "")
        self.repo: str = doc.get("repo", "")
        self.base: str = doc.get("base", "main")
        self.branch: str = doc.get("branch", "")
        self.needs: list[dict] = doc.get("needs", [])
        self.allowed_paths: list[str] = doc.get("allowed_paths", [])
        self.prompt_file: str = doc.get("prompt_file", "")


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

        task_id = doc.get("id", "")
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


def load_external(report: Report, base: Path, validator: Draft202012Validator) -> dict[str, dict]:
    """Load fleet/external.json. Absent file -> empty registry (not a failure):
    a task set with no external dependencies is legitimate."""
    path = base / "fleet" / "external.json"
    if not path.is_file():
        report.add("external", "fleet/external.json", True, "not present -- 0 external item(s)")
        return {}

    doc, err = load_json(path)
    if doc is None:
        report.add("external", "fleet/external.json", False, err)
        return {}
    if not isinstance(doc, dict):
        report.add("external", "fleet/external.json", False, "must be a JSON object of id -> item")
        return {}

    errors = sorted(validator.iter_errors(doc), key=lambda e: e.json_path)
    if errors:
        first = errors[0]
        report.add(
            "external",
            "fleet/external.json",
            False,
            f"{len(errors)} error(s): {first.json_path}: {first.message}",
        )
        return {}

    report.add("external", "fleet/external.json", True, f"{len(doc)} external item(s)")
    return doc


def check_needs(report: Report, tasks: dict[str, Task], external: dict[str, dict]) -> None:
    known = set(tasks)
    for task in tasks.values():
        for need in task.needs:
            need_id = need.get("task")
            need_state = need.get("state")
            subject = f"{task.id} needs {need_id}"
            if need_id in known:
                report.add("needs", subject, True, f"resolved to fleet/tasks/{need_id}.json, claim state={need_state}")
            elif need_id in external:
                report.add(
                    "needs",
                    subject,
                    True,
                    f"resolved to fleet/external.json, actual state={external[need_id]['state']!r}",
                )
            else:
                report.add(
                    "needs",
                    subject,
                    False,
                    f"'{need_id}' is not a fleet/tasks/ id and not a fleet/external.json key",
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


# --- path safety ---------------------------------------------------------------------
#
# Deliberately two separate, independently-mutable functions (rather than one combined
# regex) so each failure mode -- an absolute path, and a '..' escape -- has its own
# check that a mutation test can disable on its own and show exactly that fixture's
# test go red (tools/fleet/tests/test_fleet.py).


def _path_is_absolute(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        return True
    return False


def _path_escapes_repo(path: str) -> bool:
    return any(segment == ".." for segment in path.replace("\\", "/").split("/"))


def _check_one_path(report: Report, subject: str, path: str) -> None:
    if _path_is_absolute(path):
        report.add("paths", subject, False, "absolute path is not allowed -- must be relative to the repo")
    elif _path_escapes_repo(path):
        report.add("paths", subject, False, "contains a '..' path segment -- escapes the repo")
    else:
        report.add("paths", subject, True, "relative, no '..'")


def check_path_safety(report: Report, tasks: dict[str, Task], external: dict[str, dict]) -> None:
    for task in tasks.values():
        for glob in task.allowed_paths:
            _check_one_path(report, f"{task.id}: allowed_paths={glob}", glob)
        if task.prompt_file:
            _check_one_path(report, f"{task.id}: prompt_file={task.prompt_file}", task.prompt_file)
    for ext_id, item in external.items():
        for glob in item.get("paths", []):
            _check_one_path(report, f"external/{ext_id}: paths={glob}", glob)


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
    task_validator = load_task_schema()
    external_validator = load_external_schema()
    report = Report()

    tasks = load_tasks(report, tasks_dir, task_validator)
    external = load_external(report, base, external_validator)
    check_needs(report, tasks, external)
    check_acyclic(report, tasks)
    check_path_safety(report, tasks, external)
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


def compute_readiness(tasks: dict[str, Task], external: dict[str, dict]) -> dict[str, list[str]]:
    """Per task, the list of need ids currently blocking it. Empty list = ready.

    External readiness comes only from fleet/external.json's own hand-edited 'state'
    ('merged' = satisfied), never from the need's own claimed state. A need on another
    fleet task is always reported as blocking: this tool has no record yet of whether
    that task has actually been run/pushed/merged (fleet/results/ tracking is future
    work), so it conservatively never claims a sibling task is done on its behalf.
    """
    blocking: dict[str, list[str]] = {}
    known = set(tasks)
    for task in tasks.values():
        blocked_on = []
        for need in task.needs:
            need_id = need["task"]
            if need_id in known:
                blocked_on.append(need_id)
            elif need_id in external:
                if external[need_id]["state"] != "merged":
                    blocked_on.append(need_id)
            else:
                blocked_on.append(need_id)  # unresolved; validate() would already have failed this
        blocking[task.id] = blocked_on
    return blocking


def find_external_overlaps(
    tasks: dict[str, Task], external: dict[str, dict]
) -> list[tuple[str, str, str, str, str]]:
    """(task_id, external_id, external_state, glob_from_task, glob_from_external) for every
    same-repo pair where the task's allowed_paths overlaps an OPEN external item's paths."""
    overlaps = []
    for task in tasks.values():
        for ext_id, item in external.items():
            if item["state"] == "merged" or item["repo"] != task.repo:
                continue
            for glob_a, glob_b in find_overlapping_paths(task.allowed_paths, item.get("paths", [])):
                overlaps.append((task.id, ext_id, item["state"], glob_a, glob_b))
    return overlaps


def run_plan(base: Path) -> int:
    tasks_dir = base / "fleet" / "tasks"
    task_validator = load_task_schema()
    external_validator = load_external_schema()
    report = Report()
    tasks = load_tasks(report, tasks_dir, task_validator)
    external = load_external(report, base, external_validator)
    check_needs(report, tasks, external)
    check_acyclic(report, tasks)
    check_path_safety(report, tasks, external)
    check_prompt_files(report, tasks, base)
    if not report.ok:
        report.print()
        print("\nFAILED: fix the task files above before planning (run `validate` for detail).")
        return 1

    layers = compute_layers(tasks)
    blocking = compute_readiness(tasks, external)
    max_layer = max(layers.values(), default=-1)

    print(f"{len(tasks)} task(s), {max_layer + 1} layer(s)\n")

    serialize_pairs: list[tuple[str, str, str, str]] = []
    for layer_num in range(max_layer + 1):
        layer_tasks = sorted((t for t in tasks.values() if layers[t.id] == layer_num), key=lambda t: t.id)
        print(f"Layer {layer_num}:")
        for task in layer_tasks:
            blocked_on = blocking[task.id]
            status = "ready" if not blocked_on else "blocked on: " + ", ".join(blocked_on)
            print(f"  {task.id:<14} repo={task.repo:<16} branch={task.branch:<42} {status}")

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

    print("Must serialize (same repo, same layer, overlapping allowed_paths):")
    if serialize_pairs:
        for t1, t2, glob_a, glob_b in serialize_pairs:
            print(f"  {t1} <-> {t2}: '{glob_a}' overlaps '{glob_b}'")
    else:
        print("  none -- every layer's fleet tasks can run fully in parallel.")

    print("\nExternal path overlaps (open external items vs. fleet task allowed_paths):")
    external_overlaps = find_external_overlaps(tasks, external)
    if external_overlaps:
        for task_id, ext_id, ext_state, glob_a, glob_b in external_overlaps:
            print(f"  {task_id} overlaps external {ext_id} ({ext_state}): '{glob_a}' overlaps '{glob_b}'")
    else:
        print("  none.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        type=Path,
        default=ROOT,
        help="Workspace root containing fleet/tasks, fleet/external.json and fleet/prompts (default: repo root).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="Validate every task file.")
    sub.add_parser("plan", help="Print topological layers, readiness, and required serializations.")
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
