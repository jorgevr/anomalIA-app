"""FLEET-2: the live task runner (herdr + claude + gh), built on FLEET-1's task
registry (fleet.py).

Every external command -- git, herdr, gh -- goes through one injected
CommandRunner (command_runner.py) so the full command sequence, JSON parsing,
and state-reaction logic can be tested with FakeCommandRunner and no live herdr
session, no network, no real git repo.

Safety boundary (non-negotiable, enforced by REPO_PATHS below): every live run
targets 'sandbox' (C:\\code\\fleet-sandbox) -- never root, ingestion-func, or
processing-func. There is no code path in this module that resolves a task's
repo_path to anything inside the workspace repo or its submodules for a `run`;
'root'/'ingestion-func'/'processing-func' resolve too (fleet.py validate/plan use
them), but `fleet run` only ever receives sandbox task files in practice and
nothing here special-cases that -- the operator is responsible for never handing
`run` a root/service task file. See docs/fleet.md.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet as fleet1  # noqa: E402  (FLEET-1: schemas, Task, Report, glob helpers)

from command_runner import CommandResult, CommandRunner  # noqa: E402

ROOT = fleet1.ROOT
RESULT_SCHEMA_PATH = ROOT / "fleet" / "schemas" / "result.v1.json"
STOP_FILE = ROOT / "fleet" / "STOP"

DEFAULT_FLEET_HOME = Path(os.environ.get("FLEET_HOME", r"C:\code\_fleet"))

REPO_PATHS = {
    "root": ROOT,
    "ingestion-func": ROOT / "services" / "ingestion-func",
    "processing-func": ROOT / "services" / "processing-func",
    "sandbox": Path(r"C:\code\fleet-sandbox"),
}

# Checked at --version's first two dot-separated components ("major.minor"); gh's
# scheme ("2.102.0") makes "major.minor" == "2.102", which is what we compare.
TESTED_VERSIONS = {"herdr": "0.9", "claude": "2.1", "gh": "2.102"}

ALLOWED_TOOLS = ["Bash(git *)", "Bash(dotnet *)", "Bash(python *)", "Bash(pytest *)", "Bash(ruff *)", "Edit", "Read", "Write"]
DISALLOWED_TOOLS = ["Bash(git push*)", "Bash(gh *)", "Bash(curl *)", "Bash(wget *)", "Bash(nc *)", "Bash(ssh *)"]

AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

NEEDS_HUMAN_STATUSES = {"blocked", "timeout", "agent_prompt_stalled"}
READY_STATUSES = {"idle", "done"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agent_name_for(task_id: str) -> str:
    name = re.sub(r"[^a-z0-9_-]", "-", task_id.lower()).strip("-")
    name = re.sub(r"^[^a-z]+", "", name) or "t"
    name = name[:32]
    if not AGENT_NAME_RE.match(name):
        name = "t-" + name[:30]
    return name


# --- fleet home layout ---------------------------------------------------------------


def fleet_home_dirs(fleet_home: Path) -> dict[str, Path]:
    return {
        "wt": fleet_home / "wt",
        "state": fleet_home / "state",
        "outbox": fleet_home / "outbox",
        "logs": fleet_home / "logs",
        "results": fleet_home / "results",
    }


def ensure_fleet_home(fleet_home: Path) -> None:
    for path in fleet_home_dirs(fleet_home).values():
        path.mkdir(parents=True, exist_ok=True)


def docker_lock_path(fleet_home: Path) -> Path:
    return fleet_home / "docker.lock"


# --- version preflight -----------------------------------------------------------


def _extract_version(text: str) -> str | None:
    match = re.search(r"\d+\.\d+\.\d+", text)
    return match.group(0) if match else None


def _major_minor(version: str) -> str:
    parts = version.split(".")
    return ".".join(parts[:2])


def _version_is_tested(installed_mm: str, tested_mm: str) -> bool:
    """Separated from check_versions so a mutation test can disable exactly this
    comparison without having to fake every tool's version string consistently."""
    return installed_mm == tested_mm


def check_versions(runner: CommandRunner, allow_untested: bool) -> tuple[bool, list[tuple[str, str, bool, str]]]:
    """Returns (all_ok, rows) where each row is (tool, installed_version, ok, note)."""
    rows = []
    all_ok = True
    for tool, tested_mm in TESTED_VERSIONS.items():
        result = runner.run([tool, "--version"])
        installed = _extract_version(result.stdout + result.stderr)
        if installed is None:
            rows.append((tool, "<unparseable>", False, f"could not parse a version from: {(result.stdout + result.stderr)!r}"))
            all_ok = False
            continue
        installed_mm = _major_minor(installed)
        if _version_is_tested(installed_mm, tested_mm):
            rows.append((tool, installed, True, f"matches tested {tested_mm}.x"))
        elif allow_untested:
            rows.append((tool, installed, True, f"UNTESTED ({installed_mm} != {tested_mm}), allowed via --allow-untested"))
        else:
            rows.append((tool, installed, False, f"untested major.minor ({installed_mm} != {tested_mm}); pass --allow-untested to override"))
            all_ok = False
    return all_ok, rows


# --- task / needs ------------------------------------------------------------------


def load_single_task(task_path: Path) -> fleet1.Task:
    validator = fleet1.load_task_schema()
    report = fleet1.Report()
    doc, err = fleet1.load_json(task_path)
    if doc is None:
        raise ValueError(f"cannot load {task_path}: {err}")
    errors = sorted(validator.iter_errors(doc), key=lambda e: e.json_path)
    if errors:
        first = errors[0]
        raise ValueError(f"{task_path} fails task.v1.json: {first.json_path}: {first.message}")
    return fleet1.Task(task_path, doc)


@dataclass
class DependencyCheck:
    need_id: str
    satisfied: bool
    note: str


def check_dependency(
    runner: CommandRunner,
    task: fleet1.Task,
    need: dict,
    external: dict[str, dict],
    fleet_home: Path,
) -> DependencyCheck:
    need_id = need["task"]
    need_state = need["state"]

    if need_id in external:
        item = external[need_id]
        satisfied = item["state"] == "merged"
        return DependencyCheck(need_id, satisfied, f"external.json state={item['state']!r}")

    result_path = fleet_home_dirs(fleet_home)["results"] / f"{need_id}.json"
    if not result_path.is_file():
        return DependencyCheck(need_id, False, f"no {result_path} yet -- dependency has not been run")

    doc, err = fleet1.load_json(result_path)
    if doc is None or doc.get("status") != "succeeded":
        return DependencyCheck(need_id, False, f"result status={doc.get('status') if doc else err!r}")

    head_sha = doc.get("head_sha")
    repo_path = REPO_PATHS[task.repo]

    if need_state == "merged":
        base_ref = f"origin/{task.base}"
        result = runner.run(["git", "-C", str(repo_path), "merge-base", "--is-ancestor", head_sha, base_ref])
        satisfied = result.returncode == 0
        return DependencyCheck(need_id, satisfied, f"git merge-base --is-ancestor {head_sha} {base_ref} -> exit {result.returncode}")

    # need_state == "pushed": the dependency's branch must exist on origin at head_sha.
    dep_branch = doc.get("branch")
    result = runner.run(["git", "-C", str(repo_path), "rev-parse", f"origin/{dep_branch}"])
    satisfied = result.ok and result.stdout.strip() == head_sha
    return DependencyCheck(need_id, satisfied, f"origin/{dep_branch} -> {result.stdout.strip() or result.stderr.strip()}")


# --- preflight ----------------------------------------------------------------------


def task_needs_docker(task: fleet1.Task) -> bool:
    """Heuristic (FLEET-1's task.v1.json has no explicit docker flag): a task needs
    docker if any acceptance command mentions it."""
    for check in task.doc.get("acceptance", []):
        if "docker" in check.get("command", "").lower():
            return True
    return False


@dataclass
class PreflightResult:
    report: fleet1.Report
    task: fleet1.Task | None
    docker_lock_needed: bool

    @property
    def ok(self) -> bool:
        return self.report.ok


def preflight(
    runner: CommandRunner,
    task_path: Path,
    fleet_home: Path,
    allow_untested: bool,
) -> PreflightResult:
    report = fleet1.Report()

    versions_ok, version_rows = check_versions(runner, allow_untested)
    for tool, installed, ok, note in version_rows:
        report.add("version", tool, ok, f"{installed}: {note}")

    report.add("stop", str(STOP_FILE), not STOP_FILE.exists(), "absent" if not STOP_FILE.exists() else "PRESENT -- kill switch is set, no new tasks")

    task: fleet1.Task | None = None
    try:
        task = load_single_task(task_path)
        report.add("schema", str(task_path), True, f"valid task.v1.json, id={task.id}")
    except ValueError as exc:
        report.add("schema", str(task_path), False, str(exc))
        return PreflightResult(report, None, False)

    external_validator = fleet1.load_external_schema()
    external = fleet1.load_external(report, ROOT, external_validator)

    for need in task.needs:
        check = check_dependency(runner, task, need, external, fleet_home)
        report.add("needs", f"{task.id} needs {check.need_id}", check.satisfied, check.note)

    if task.repo not in REPO_PATHS:
        report.add("repo", task.repo, False, f"unknown repo {task.repo!r}")
        return PreflightResult(report, task, False)

    repo_path = REPO_PATHS[task.repo]
    status = runner.run(["git", "-C", str(repo_path), "status", "--porcelain"])
    clean = status.ok and status.stdout.strip() == ""
    report.add("repo-clean", str(repo_path), clean, "clean" if clean else f"dirty:\n{status.stdout}")

    fetch = runner.run(["git", "-C", str(repo_path), "fetch", "origin"])
    report.add("repo-fetch", str(repo_path), fetch.ok, "fetched origin" if fetch.ok else fetch.stderr.strip())

    docker_needed = task_needs_docker(task)
    if docker_needed:
        lock = docker_lock_path(fleet_home)
        held = lock.is_file()
        report.add("docker-lock", str(lock), not held, "free" if not held else f"HELD -- another task is using the docker stack: {lock.read_text(encoding='utf-8') if held else ''}")

    return PreflightResult(report, task, docker_needed)


# --- worktree + pane ----------------------------------------------------------------


@dataclass
class WorktreeInfo:
    workspace_id: str
    tab_id: str | None
    pane_id: str
    path: Path
    raw: dict


def _dig(obj: dict, *keys: str):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def parse_worktree_response(raw_stdout: str) -> WorktreeInfo:
    """herdr worktree create prints one JSON object on stdout. Its exact shape is
    not documented by --help; this tries the response shapes the skill doc implies
    by analogy with `workspace create` (.result.workspace / .result.root_pane) and
    raises a clear error naming what WAS present if none of them match, rather than
    silently guessing a wrong field."""
    doc = json.loads(raw_stdout)
    result = doc.get("result", doc)

    workspace_id = _dig(result, "workspace", "workspace_id") or _dig(result, "workspace") if isinstance(result.get("workspace"), str) else _dig(result, "workspace", "workspace_id")
    tab_id = _dig(result, "tab", "tab_id")
    pane_id = _dig(result, "root_pane", "pane_id") or _dig(result, "pane", "pane_id")
    path = _dig(result, "worktree", "path") or result.get("path")

    if not pane_id:
        raise ValueError(f"could not find a pane id in herdr worktree create's response: {doc!r}")

    return WorktreeInfo(
        workspace_id=workspace_id or "",
        tab_id=tab_id,
        pane_id=pane_id,
        path=Path(path) if path else Path(),
        raw=doc,
    )


def create_worktree(runner: CommandRunner, task: fleet1.Task, fleet_home: Path) -> WorktreeInfo:
    repo_path = REPO_PATHS[task.repo]
    wt_path = fleet_home_dirs(fleet_home)["wt"] / task.id
    cmd = [
        "herdr", "worktree", "create",
        "--cwd", str(repo_path),
        "--branch", task.branch,
        "--base", f"origin/{task.base}",
        "--path", str(wt_path),
        "--label", task.id,
        "--no-focus",
    ]
    result = runner.run(cmd)
    if not result.ok:
        raise RuntimeError(f"herdr worktree create failed: {result.stderr or result.stdout}")
    info = parse_worktree_response(result.stdout)
    if not info.path or info.path == Path():
        info.path = wt_path
    return info


def save_state(fleet_home: Path, task_id: str, state: dict) -> Path:
    ensure_fleet_home(fleet_home)
    path = fleet_home_dirs(fleet_home)["state"] / f"{task_id}.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def load_state(fleet_home: Path, task_id: str) -> dict | None:
    path = fleet_home_dirs(fleet_home)["state"] / f"{task_id}.json"
    if not path.is_file():
        return None
    doc, _ = fleet1.load_json(path)
    return doc


# --- agent -----------------------------------------------------------------------


def task_base_dir(task: fleet1.Task) -> Path:
    """task.path is always .../fleet/tasks/<id>.json (real registry, a test
    fixture, or a standalone demo dir); prompt_file is documented as relative to
    that base, i.e. three levels up from the task file itself -- not hardcoded to
    the workspace ROOT, so this resolves correctly for all three."""
    return task.path.resolve().parent.parent.parent


def write_outbox_prompt(task: fleet1.Task, fleet_home: Path) -> tuple[Path, Path]:
    outbox_dir = fleet_home_dirs(fleet_home)["outbox"] / task.id
    outbox_dir.mkdir(parents=True, exist_ok=True)
    prompt_src = task_base_dir(task) / task.prompt_file
    prompt_text = prompt_src.read_text(encoding="utf-8") if prompt_src.is_file() else "paste prompt here\n"
    prompt_path = outbox_dir / "prompt.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    return outbox_dir, prompt_path


class AgentBlockedAtStartup(Exception):
    """Raised when `herdr agent start` itself returns agent_not_ready: the agent
    process came up but is sitting at a dialog (e.g. claude's first-run workspace
    trust prompt) before it ever reached an idle, promptable state. The name stays
    valid for `agent read`/`agent send-keys` (herdr's own doc) even though start
    failed, so this is a real, nameable blocked state -- not a crash."""

    def __init__(self, name: str, detail: str):
        super().__init__(detail)
        self.name = name
        self.detail = detail


def start_agent(runner: CommandRunner, task: fleet1.Task, pane_id: str, outbox_dir: Path) -> str:
    name = agent_name_for(task.id)
    agent_args = [
        "--permission-mode", "acceptEdits",
        "--permission-prompts", "none",
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--add-dir", str(outbox_dir),
    ]
    cmd = ["herdr", "agent", "start", name, "--kind", "claude", "--pane", pane_id, "--", *agent_args]
    result = runner.run(cmd)
    if not result.ok:
        raw = result.stderr or result.stdout
        try:
            code = json.loads(raw).get("error", {}).get("code")
        except json.JSONDecodeError:
            code = None
        if code == "agent_not_ready":
            raise AgentBlockedAtStartup(name, raw)
        raise RuntimeError(f"herdr agent start failed: {raw}")
    return name


def send_kickoff_prompt(runner: CommandRunner, agent_name: str, prompt_path: Path, timeout_ms: int) -> dict:
    message = (
        f"Read {prompt_path} and follow it exactly from start to finish. "
        "When you are completely done, stop and wait for review -- do not ask me anything."
    )
    cmd = ["herdr", "agent", "prompt", agent_name, message, "--wait", "--timeout", str(timeout_ms)]
    result = runner.run(cmd, timeout=(timeout_ms / 1000.0) + 30)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"result": {"status": "unknown"}, "_raw_stdout": result.stdout, "_raw_stderr": result.stderr, "_returncode": result.returncode}


def agent_status_from_response(doc: dict) -> str:
    status = _dig(doc, "result", "status") or doc.get("status") or doc.get("error", {}).get("code") if isinstance(doc.get("error"), dict) else None
    return status or "unknown"


# --- react -----------------------------------------------------------------------


def react_to_status(runner: CommandRunner, task: fleet1.Task, agent_name: str, status: str) -> str:
    """Returns 'proceed' (go verify) or 'needs_human' (stop, do not touch the
    pane/dialog). Idle/done land in 'proceed' -- idle never means success on its
    own, only that verification may now run."""
    if status == "blocked":
        # Side effect only -- never answers the dialog. Classification (and thus
        # whether this short-circuits to needs_human) still runs through the sets
        # below, so a mutation that drops "blocked" from NEEDS_HUMAN_STATUSES is
        # actually observable instead of being masked by an early return here.
        runner.run(["herdr", "notification", "show", f"FLEET-2: {task.id} is blocked", "--body", "An agent is waiting at an approval/question dialog. Review it yourself -- the runner will not answer it."])
    if status in NEEDS_HUMAN_STATUSES:
        return "needs_human"
    if status in READY_STATUSES:
        return "proceed"
    return "needs_human"  # unknown: conservative default, never silently proceed


# --- verify ------------------------------------------------------------------------


@dataclass
class AcceptanceResult:
    name: str
    exit_code: int
    output_tail: str
    passed: bool


@dataclass
class VerifyResult:
    status: str  # "succeeded" | "failed" | "rejected"
    commits: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    allowed_paths_violations: list[str] = field(default_factory=list)
    acceptance_results: list[AcceptanceResult] = field(default_factory=list)
    note: str = ""


def _tail(text: str, lines: int = 40) -> str:
    parts = text.splitlines()
    return "\n".join(parts[-lines:])


def verify(runner: CommandRunner, task: fleet1.Task, wt_path: Path, base_sha: str) -> VerifyResult:
    log = runner.run(["git", "-C", str(wt_path), "log", "--format=%H%x09%s", f"{base_sha}..HEAD"])
    commit_lines = [line for line in log.stdout.splitlines() if line.strip()]
    if not commit_lines:
        return VerifyResult(status="rejected", note="no commits ahead of base -- nothing to verify")

    commits = []
    bad_prefix = []
    for line in commit_lines:
        sha, _, message = line.partition("\t")
        commits.append(sha)
        if not message.startswith(task.id):
            bad_prefix.append((sha, message))
    if bad_prefix:
        detail = "; ".join(f"{sha[:8]} {message!r}" for sha, message in bad_prefix)
        return VerifyResult(status="rejected", commits=commits, note=f"commit message(s) do not start with task id {task.id!r}: {detail}")

    diff = runner.run(["git", "-C", str(wt_path), "diff", "--name-only", f"{base_sha}..HEAD"])
    files_changed = [f for f in diff.stdout.splitlines() if f.strip()]

    violations = [f for f in files_changed if not any(fleet1.globs_overlap(f, glob) for glob in task.allowed_paths)]
    if violations:
        return VerifyResult(
            status="rejected",
            commits=commits,
            files_changed=files_changed,
            allowed_paths_violations=violations,
            note=f"{len(violations)} file(s) outside allowed_paths -- not fixing, rejecting",
        )

    acceptance_results = []
    all_passed = True
    for check in task.doc.get("acceptance", []):
        cwd = wt_path / check["cwd"] if check["cwd"] != "." else wt_path
        result = runner.run(["sh", "-lc", check["command"]], cwd=cwd)
        passed = result.returncode == check["expect_exit"]
        all_passed = all_passed and passed
        acceptance_results.append(
            AcceptanceResult(
                name=check["name"],
                exit_code=result.returncode,
                output_tail=_tail(result.stdout + result.stderr),
                passed=passed,
            )
        )

    status = "succeeded" if all_passed else "failed"
    return VerifyResult(
        status=status,
        commits=commits,
        files_changed=files_changed,
        acceptance_results=acceptance_results,
        note="" if all_passed else "one or more acceptance checks did not pass",
    )


# --- push + PR -----------------------------------------------------------------------


def render_pr_body(task: fleet1.Task, verify_result: VerifyResult, agent_note: str) -> str:
    lines = [
        f"FLEET-2 automated run for task `{task.id}`.",
        "",
        "## Facts (observed by the orchestrator, not the agent)",
        f"- Commits: {len(verify_result.commits)}",
        f"- Files changed: {', '.join(verify_result.files_changed) or '(none)'}",
        "",
        "## Acceptance",
    ]
    for ar in verify_result.acceptance_results:
        lines.append(f"- `{ar.name}`: exit {ar.exit_code} ({'PASS' if ar.passed else 'FAIL'})")
    if agent_note:
        lines += ["", "## Agent note (UNTRUSTED, quoted verbatim)", "> " + agent_note.replace("\n", "\n> ")]
    return "\n".join(lines)


def push_and_create_pr(runner: CommandRunner, task: fleet1.Task, wt_path: Path, verify_result: VerifyResult, agent_note: str) -> dict | None:
    push = runner.run(["git", "-C", str(wt_path), "push", "-u", "origin", task.branch])
    if not push.ok:
        return None
    body = render_pr_body(task, verify_result, agent_note)
    pr = runner.run(["gh", "pr", "create", "--base", task.base, "--head", task.branch, "--title", f"{task.id}: {task.doc["title"]}", "--body", body], cwd=wt_path)
    if not pr.ok:
        return None
    url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
    number_match = re.search(r"/pull/(\d+)", url)
    number = int(number_match.group(1)) if number_match else None
    return {"number": number, "url": url}


# --- result file + logs --------------------------------------------------------------


def write_result(fleet_home: Path, result_doc: dict) -> Path:
    validator_doc, err = fleet1.load_json(RESULT_SCHEMA_PATH)
    if validator_doc is None:
        raise SystemExit(f"cannot load {RESULT_SCHEMA_PATH}: {err}")
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(validator_doc)
    errors = sorted(validator.iter_errors(result_doc), key=lambda e: e.json_path)
    if errors:
        first = errors[0]
        raise ValueError(f"result document fails result.v1.json: {first.json_path}: {first.message}")

    ensure_fleet_home(fleet_home)
    path = fleet_home_dirs(fleet_home)["results"] / f"{result_doc['task_id']}.json"
    path.write_text(json.dumps(result_doc, indent=2), encoding="utf-8")
    return path


def capture_agent_log(runner: CommandRunner, agent_name: str, fleet_home: Path, task_id: str) -> Path:
    ensure_fleet_home(fleet_home)
    result = runner.run(["herdr", "agent", "read", agent_name, "--source", "recent-unwrapped", "--lines", "500"])
    path = fleet_home_dirs(fleet_home)["logs"] / f"{task_id}.txt"
    path.write_text(result.stdout, encoding="utf-8")
    return path


# --- top-level orchestration: `fleet run` -------------------------------------------


DEFAULT_TIMEOUT_SECONDS_PER_TURN = 90  # heuristic only -- claude 2.1.268 has no --max-turns (Step 0 finding)


def timeout_ms_for(task: fleet1.Task) -> int:
    max_turns = task.doc.get("max_turns", 10)
    return max_turns * DEFAULT_TIMEOUT_SECONDS_PER_TURN * 1000


def print_dry_run_plan(task_path: Path, fleet_home: Path) -> None:
    """Prints every command `run` would execute, in order, with placeholders for
    values only known at runtime (a pane id, a base sha, the kickoff prompt text).
    Does not execute or fake-execute anything -- no JSON is threaded through real
    control flow, so there is nothing here that can misparse and crash."""
    task = load_single_task(task_path)
    repo_path = REPO_PATHS[task.repo]
    wt_path = fleet_home_dirs(fleet_home)["wt"] / task.id
    outbox_dir = fleet_home_dirs(fleet_home)["outbox"] / task.id
    prompt_path = outbox_dir / "prompt.md"
    agent_name = agent_name_for(task.id)

    agent_args = [
        "--permission-mode", "acceptEdits",
        "--permission-prompts", "none",
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--add-dir", str(outbox_dir),
    ]

    commands: list[list[str]] = [
        ["herdr", "--version"],
        ["claude", "--version"],
        ["gh", "--version"],
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        ["git", "-C", str(repo_path), "fetch", "origin"],
    ]
    for need in task.needs:
        commands.append(["git", "-C", str(repo_path), "merge-base", "--is-ancestor", "<dependency-head-sha>", f"origin/{task.base}"])
    commands += [
        ["git", "-C", str(repo_path), "rev-parse", f"origin/{task.base}"],
        ["herdr", "worktree", "create", "--cwd", str(repo_path), "--branch", task.branch, "--base", f"origin/{task.base}", "--path", str(wt_path), "--label", task.id, "--no-focus"],
        ["herdr", "agent", "start", agent_name, "--kind", "claude", "--pane", "<pane-id-from-worktree-create>", "--", *agent_args],
        ["herdr", "agent", "prompt", agent_name, "<kickoff message pointing at " + str(prompt_path) + ">", "--wait", "--timeout", str(timeout_ms_for(task))],
        ["git", "-C", str(wt_path), "log", "--format=%H%x09%s", "<base-sha>..HEAD"],
        ["git", "-C", str(wt_path), "diff", "--name-only", "<base-sha>..HEAD"],
    ]
    for check in task.doc.get("acceptance", []):
        cwd = wt_path if check["cwd"] == "." else wt_path / check["cwd"]
        commands.append(["sh", "-lc", check["command"], f"(cwd={cwd})"])
    commands += [
        ["git", "-C", str(wt_path), "diff", "--shortstat", "<base-sha>..HEAD"],
        ["herdr", "agent", "read", agent_name, "--source", "recent-unwrapped", "--lines", "500"],
        ["git", "-C", str(wt_path), "push", "-u", "origin", task.branch],
        ["gh", "pr", "create", "--base", task.base, "--head", task.branch, "--title", f"{task.id}: {task.doc["title"]}", "--body", "<generated from recorded facts>"],
    ]

    print(f"[dry-run] {task.id}: {len(commands)} command(s) would run, in order:")
    for cmd in commands:
        print("  " + " ".join(str(c) for c in cmd))


def run_task(
    runner: CommandRunner,
    task_path: Path,
    fleet_home: Path = DEFAULT_FLEET_HOME,
    allow_untested: bool = False,
) -> dict:
    """The full pipeline for one task. Returns the result.v1.json document that
    was (or would have been, for a status short of succeeded/failed/rejected)
    written. Every decision here comes from a CommandResult this function itself
    observed -- never from agent prose."""
    started_at = now_iso()
    ensure_fleet_home(fleet_home)

    pf = preflight(runner, task_path, fleet_home, allow_untested)
    pf.report.print()
    if not pf.ok:
        return {
            "task_id": pf.task.id if pf.task else task_path.stem,
            "status": "blocked",
            "started_at": started_at,
            "finished_at": now_iso(),
            "branch": pf.task.branch if pf.task else "",
            "base_sha": "",
            "head_sha": "",
            "commits": [],
            "files_changed": [],
            "diff_stat": {"files": 0, "insertions": 0, "deletions": 0},
            "allowed_paths_violations": [],
            "acceptance_results": [],
            "agent_report": "",
            "pr": None,
            "reviewer": None,
            "cost": {"turns": 0, "usd": 0.0},
        }

    task = pf.task
    repo_path = REPO_PATHS[task.repo]
    base_sha_result = runner.run(["git", "-C", str(repo_path), "rev-parse", f"origin/{task.base}"])
    base_sha = base_sha_result.stdout.strip()

    wt_info = create_worktree(runner, task, fleet_home)
    save_state(
        fleet_home,
        task.id,
        {
            "task_id": task.id,
            "workspace_id": wt_info.workspace_id,
            "tab_id": wt_info.tab_id,
            "pane_id": wt_info.pane_id,
            "worktree_path": str(wt_info.path),
            "agent_name": None,
            "repo": task.repo,
            "branch": task.branch,
            "created_at": now_iso(),
        },
    )

    outbox_dir, prompt_path = write_outbox_prompt(task, fleet_home)

    base_result = dict(
        task_id=task.id,
        started_at=started_at,
        branch=task.branch,
        base_sha=base_sha,
    )

    def blocked_result(agent_name: str | None, note: str) -> dict:
        result_doc = {
            **base_result,
            "status": "blocked",
            "finished_at": now_iso(),
            "head_sha": "",
            "commits": [],
            "files_changed": [],
            "diff_stat": {"files": 0, "insertions": 0, "deletions": 0},
            "allowed_paths_violations": [],
            "acceptance_results": [],
            "agent_report": note,
            "pr": None,
            "reviewer": None,
            "cost": {"turns": 0, "usd": 0.0},
        }
        write_result(fleet_home, result_doc)
        if agent_name:
            capture_agent_log(runner, agent_name, fleet_home, task.id)
        return result_doc

    try:
        agent_name = start_agent(runner, task, wt_info.pane_id, outbox_dir)
    except AgentBlockedAtStartup as exc:
        state = load_state(fleet_home, task.id) or {}
        state["agent_name"] = exc.name
        save_state(fleet_home, task.id, state)
        runner.run(
            [
                "herdr", "notification", "show", f"FLEET-2: {task.id} is blocked at startup",
                "--body", "The agent came up at a dialog (e.g. claude's workspace trust prompt) before it ever reached idle. Review the pane yourself -- the runner will not answer it.",
            ]
        )
        return blocked_result(exc.name, f"agent blocked at startup: {exc.detail}")

    state = load_state(fleet_home, task.id) or {}
    state["agent_name"] = agent_name
    save_state(fleet_home, task.id, state)

    prompt_response = send_kickoff_prompt(runner, agent_name, prompt_path, timeout_ms_for(task))
    status = agent_status_from_response(prompt_response)
    reaction = react_to_status(runner, task, agent_name, status)

    if reaction == "needs_human":
        return blocked_result(agent_name, f"agent status at handoff: {status}")

    head_sha_result = runner.run(["git", "-C", str(wt_info.path), "rev-parse", "HEAD"])
    head_sha = head_sha_result.stdout.strip()

    verify_result = verify(runner, task, wt_info.path, base_sha)

    agent_note = ""
    agent_log_path = capture_agent_log(runner, agent_name, fleet_home, task.id)
    agent_note = agent_log_path.read_text(encoding="utf-8")[-2000:] if agent_log_path.is_file() else ""

    diff_stat_result = runner.run(["git", "-C", str(wt_info.path), "diff", "--shortstat", f"{base_sha}..HEAD"])
    diff_stat = _parse_shortstat(diff_stat_result.stdout)

    pr_info = None
    if verify_result.status == "succeeded":
        pr_info = push_and_create_pr(runner, task, wt_info.path, verify_result, agent_note)
        if pr_info is None:
            verify_result.status = "failed"
            verify_result.note = (verify_result.note + "; push or gh pr create failed").strip("; ")

    result_doc = {
        **base_result,
        "status": verify_result.status,
        "finished_at": now_iso(),
        "head_sha": head_sha,
        "commits": verify_result.commits,
        "files_changed": verify_result.files_changed,
        "diff_stat": diff_stat,
        "allowed_paths_violations": verify_result.allowed_paths_violations,
        "acceptance_results": [
            {"name": ar.name, "exit_code": ar.exit_code, "output_tail": ar.output_tail} for ar in verify_result.acceptance_results
        ],
        "agent_report": agent_note,
        "pr": pr_info,
        "reviewer": None,
        "cost": {"turns": 0, "usd": 0.0},
    }
    write_result(fleet_home, result_doc)
    return result_doc


def _parse_shortstat(text: str) -> dict:
    files = ins = dels = 0
    m = re.search(r"(\d+) files? changed", text)
    if m:
        files = int(m.group(1))
    m = re.search(r"(\d+) insertions?", text)
    if m:
        ins = int(m.group(1))
    m = re.search(r"(\d+) deletions?", text)
    if m:
        dels = int(m.group(1))
    return {"files": files, "insertions": ins, "deletions": dels}


# --- fleet status / fleet clean -------------------------------------------------


def status_rows(fleet_home: Path) -> list[dict]:
    rows = []
    state_dir = fleet_home_dirs(fleet_home)["state"]
    results_dir = fleet_home_dirs(fleet_home)["results"]
    if not state_dir.is_dir():
        return rows
    for state_path in sorted(state_dir.glob("*.json")):
        state, _ = fleet1.load_json(state_path)
        if state is None:
            continue
        task_id = state.get("task_id", state_path.stem)
        result_path = results_dir / f"{task_id}.json"
        result, _ = fleet1.load_json(result_path) if result_path.is_file() else (None, "")
        rows.append(
            {
                "task": task_id,
                "repo": state.get("repo", ""),
                "state": result.get("status") if result else "running/unknown",
                "pane": state.get("pane_id", ""),
                "branch": state.get("branch", ""),
                "pr": (result.get("pr") or {}).get("url", "") if result else "",
                "elapsed": "",
            }
        )
    return rows


def print_status(fleet_home: Path) -> None:
    rows = status_rows(fleet_home)
    if not rows:
        print("No recorded fleet tasks under", fleet_home)
        return
    headers = ["task", "repo", "state", "pane", "branch", "pr", "elapsed"]
    widths = {h: max(len(h), *(len(str(r[h])) for r in rows)) for h in headers}
    print("  ".join(h.upper().ljust(widths[h]) for h in headers))
    for r in rows:
        print("  ".join(str(r[h]).ljust(widths[h]) for h in headers))


def clean_task(runner: CommandRunner, fleet_home: Path, task_id: str) -> str:
    """Removes a worktree -- ONLY for a task id recorded in fleet_home/state/.
    Never called automatically; this is the one place a worktree/pane is torn
    down, and only on an explicit `fleet clean <id>`."""
    state = load_state(fleet_home, task_id)
    if state is None:
        raise ValueError(f"no recorded state for task {task_id!r} under {fleet_home} -- nothing to clean")

    workspace_id = state.get("workspace_id")
    if workspace_id:
        result = runner.run(["herdr", "worktree", "remove", "--workspace", workspace_id, "--force"])
        if not result.ok:
            return f"herdr worktree remove failed: {result.stderr or result.stdout}"
    state_path = fleet_home_dirs(fleet_home)["state"] / f"{task_id}.json"
    state_path.unlink(missing_ok=True)
    return f"cleaned {task_id} (workspace {workspace_id})"


# --- CLI -----------------------------------------------------------------------


def resolve_task_path(task_arg: str) -> Path:
    candidate = Path(task_arg)
    if candidate.is_file():
        return candidate
    by_id = ROOT / "fleet" / "tasks" / f"{task_arg}.json"
    if by_id.is_file():
        return by_id
    raise SystemExit(f"no task file found for {task_arg!r} (tried it as a path, and as fleet/tasks/{task_arg}.json)")


def main(argv: list[str] | None = None) -> int:
    import argparse

    from command_runner import RealCommandRunner

    parser = argparse.ArgumentParser(description="FLEET-2: live task runner (herdr + claude + gh)")
    parser.add_argument("--fleet-home", type=Path, default=DEFAULT_FLEET_HOME)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run one task live (worktree, pane, agent, verify, push, PR).")
    run_p.add_argument("task", help="Task id (looked up in fleet/tasks/) or a path to a task JSON file.")
    run_p.add_argument("--allow-untested", action="store_true")
    run_p.add_argument("--dry-run", action="store_true", help="Print every command that would run; execute nothing that mutates the world.")

    sub.add_parser("status", help="Print the table of recorded fleet tasks.")

    clean_p = sub.add_parser("clean", help="Remove a worktree for a recorded task id. Never automatic.")
    clean_p.add_argument("task_id")

    args = parser.parse_args(argv)

    if args.cmd == "status":
        print_status(args.fleet_home)
        return 0

    if args.cmd == "clean":
        print(clean_task(RealCommandRunner(), args.fleet_home, args.task_id))
        return 0

    if args.cmd == "run":
        task_path = resolve_task_path(args.task)
        if args.dry_run:
            print_dry_run_plan(task_path, args.fleet_home)
            return 0
        result_doc = run_task(RealCommandRunner(), task_path, args.fleet_home, allow_untested=args.allow_untested)
        print(json.dumps(result_doc, indent=2))
        return 0 if result_doc.get("status") == "succeeded" else 1

    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
