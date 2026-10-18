"""Tests for tools/fleet/runner.py (FLEET-2) using FakeCommandRunner -- no live
herdr session, no network, no real git repo required.

Each test that corresponds to one of the required scenarios (command sequence,
JSON parsing, blocked handling, allowed_paths violation, missing commit, failing
acceptance, STOP file, version refusal) has a matching `test_mutation_*` that
breaks the real implementation in exactly the way that scenario exists to catch,
and shows the original test go red.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import runner  # noqa: E402
from command_runner import CommandResult, FakeCommandRunner, ok, fail  # noqa: E402

from conftest import FIXTURES

GOOD_VERSIONS = {
    ("herdr", "--version"): ok(stdout="herdr 0.9.1\n"),
    ("claude", "--version"): ok(stdout="2.1.268 (Claude Code)\n"),
    ("gh", "--version"): ok(stdout="gh version 2.102.0 (2026-09-30)\n"),
}


def _task_path(fixture: str, task_id: str) -> Path:
    return FIXTURES / fixture / "fleet" / "tasks" / f"{task_id}.json"


def _runner_for_happy_preflight() -> FakeCommandRunner:
    repo_path = str(runner.REPO_PATHS["sandbox"])
    script = dict(GOOD_VERSIONS)
    script[("git", "-C", repo_path, "status", "--porcelain")] = ok(stdout="")
    script[("git", "-C", repo_path, "fetch", "origin")] = ok()
    return FakeCommandRunner(script=script)


# --- version refusal ----------------------------------------------------------------


def test_version_refusal_without_allow_untested():
    script = dict(GOOD_VERSIONS)
    script[("herdr", "--version")] = ok(stdout="herdr 0.5.0\n")
    fake = FakeCommandRunner(script=script)
    all_ok, rows = runner.check_versions(fake, allow_untested=False)
    assert all_ok is False
    herdr_row = next(r for r in rows if r[0] == "herdr")
    assert herdr_row[2] is False
    assert "untested" in herdr_row[3]


def test_version_allowed_with_allow_untested_flag():
    script = dict(GOOD_VERSIONS)
    script[("herdr", "--version")] = ok(stdout="herdr 0.5.0\n")
    fake = FakeCommandRunner(script=script)
    all_ok, rows = runner.check_versions(fake, allow_untested=True)
    assert all_ok is True
    herdr_row = next(r for r in rows if r[0] == "herdr")
    assert herdr_row[2] is True
    assert "UNTESTED" in herdr_row[3]


def test_mutation_major_minor_disabled_breaks_version_refusal(monkeypatch):
    script = dict(GOOD_VERSIONS)
    script[("herdr", "--version")] = ok(stdout="herdr 0.5.0\n")
    fake = FakeCommandRunner(script=script)

    all_ok, _ = runner.check_versions(fake, allow_untested=False)
    assert all_ok is False, "sanity: real check catches an untested version"

    monkeypatch.setattr(runner, "_version_is_tested", lambda installed_mm, tested_mm: True)  # pretend everything matches
    fake2 = FakeCommandRunner(script=script)
    all_ok, _ = runner.check_versions(fake2, allow_untested=False)
    assert all_ok is True, "mutated version check wrongly accepted an untested herdr"


# --- STOP file -----------------------------------------------------------------------


def test_preflight_blocked_by_stop_file(tmp_path, monkeypatch):
    stop_file = tmp_path / "STOP"
    stop_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(runner, "STOP_FILE", stop_file)

    fake = _runner_for_happy_preflight()
    result = runner.preflight(fake, _task_path("sandbox_happy", "DEMO-1"), tmp_path / "fleet_home", allow_untested=True)
    assert result.ok is False
    stop_rows = [r for r in result.report.rows if r[0] == "stop"]
    assert stop_rows and stop_rows[0][2] is False


def test_mutation_stop_check_disabled_breaks_stop_fixture(tmp_path, monkeypatch):
    stop_file = tmp_path / "STOP"
    stop_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(runner, "STOP_FILE", stop_file)

    fake = _runner_for_happy_preflight()
    result = runner.preflight(fake, _task_path("sandbox_happy", "DEMO-1"), tmp_path / "fleet_home", allow_untested=True)
    assert result.ok is False, "sanity: real check catches the STOP file"

    monkeypatch.setattr(runner, "STOP_FILE", tmp_path / "definitely-does-not-exist")
    fake2 = _runner_for_happy_preflight()
    result2 = runner.preflight(fake2, _task_path("sandbox_happy", "DEMO-1"), tmp_path / "fleet_home", allow_untested=True)
    assert result2.ok is True, "mutated preflight wrongly ignored a present STOP file (by checking the wrong path)"


# --- command sequence + JSON parsing for worktree creation -------------------------


WORKTREE_JSON = json.dumps(
    {
        "id": "cli:worktree:create",
        "result": {
            "type": "worktree_created",
            "workspace": {"workspace_id": "w9"},
            "tab": {"tab_id": "w9:t1"},
            "root_pane": {"pane_id": "w9:p1"},
            "worktree": {"path": "C:/code/_fleet/wt/DEMO-1"},
        },
    }
)


def test_create_worktree_issues_exact_command_and_parses_json():
    fake = FakeCommandRunner(script={("herdr", "worktree", "create"): ok(stdout=WORKTREE_JSON)})
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    info = runner.create_worktree(fake, task, Path("C:/code/_fleet"))

    assert info.workspace_id == "w9"
    assert info.pane_id == "w9:p1"
    assert str(info.path).replace("\\", "/") == "C:/code/_fleet/wt/DEMO-1"

    [cmd] = fake.calls
    assert cmd[:3] == ["herdr", "worktree", "create"]
    assert "--branch" in cmd and cmd[cmd.index("--branch") + 1] == task.branch
    assert "--base" in cmd and cmd[cmd.index("--base") + 1] == f"origin/{task.base}"
    assert "--no-focus" in cmd
    assert "--label" in cmd and cmd[cmd.index("--label") + 1] == task.id


def test_mutation_worktree_json_parse_disabled_breaks_parsing_test(monkeypatch):
    fake = FakeCommandRunner(script={("herdr", "worktree", "create"): ok(stdout=WORKTREE_JSON)})
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    info = runner.create_worktree(fake, task, Path("C:/code/_fleet"))
    assert info.pane_id == "w9:p1", "sanity: real parser extracts the pane id"

    monkeypatch.setattr(runner, "_dig", lambda obj, *keys: None)
    fake2 = FakeCommandRunner(script={("herdr", "worktree", "create"): ok(stdout=WORKTREE_JSON)})
    with pytest.raises(ValueError):
        runner.create_worktree(fake2, task, Path("C:/code/_fleet"))


# --- blocked handling ----------------------------------------------------------------


def test_react_to_blocked_status_notifies_and_needs_human():
    fake = FakeCommandRunner(script={("herdr", "notification", "show"): ok()})
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    reaction = runner.react_to_status(fake, task, "demo-1", "blocked")
    assert reaction == "needs_human"
    assert fake.calls and fake.calls[0][:3] == ["herdr", "notification", "show"]


@pytest.mark.parametrize("status", ["timeout", "agent_prompt_stalled"])
def test_react_to_timeout_and_stalled_needs_human(status):
    fake = FakeCommandRunner(script={}, default=ok())
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    reaction = runner.react_to_status(fake, task, "demo-1", status)
    assert reaction == "needs_human"
    assert fake.calls == []  # never notifies/answers for these -- just reports needs_human


@pytest.mark.parametrize("status", ["idle", "done"])
def test_react_to_idle_done_proceeds_to_verify(status):
    fake = FakeCommandRunner(script={}, default=ok())
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    reaction = runner.react_to_status(fake, task, "demo-1", status)
    assert reaction == "proceed"


def test_mutation_blocked_handling_disabled_breaks_blocked_fixture(monkeypatch):
    fake = FakeCommandRunner(script={("herdr", "notification", "show"): ok()})
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    reaction = runner.react_to_status(fake, task, "demo-1", "blocked")
    assert reaction == "needs_human", "sanity: real reaction treats blocked as needs_human"

    monkeypatch.setattr(runner, "NEEDS_HUMAN_STATUSES", frozenset())
    monkeypatch.setattr(runner, "READY_STATUSES", frozenset({"idle", "done", "blocked"}))
    fake2 = FakeCommandRunner(script={}, default=ok())
    reaction2 = runner.react_to_status(fake2, task, "demo-1", "blocked")
    assert reaction2 == "proceed", "mutated reaction wrongly let a blocked agent proceed to verify"


# --- verify: allowed_paths violation, missing commit, failing acceptance ----------


def _git_log_result(entries: list[tuple[str, str]]) -> CommandResult:
    text = "\n".join(f"{sha}\t{msg}" for sha, msg in entries)
    return ok(stdout=text)


def test_verify_rejects_missing_commit():
    fake = FakeCommandRunner(
        script={
            ("git", "-C", r"C:\wt", "log", "--format=%H%x09%s", "base123..HEAD"): ok(stdout=""),
        }
    )
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    result = runner.verify(fake, task, Path(r"C:\wt"), "base123")
    assert result.status == "rejected"
    assert "no commits" in result.note


def test_mutation_missing_commit_check_disabled_breaks_missing_commit_test(monkeypatch):
    fake = FakeCommandRunner(
        script={("git", "-C", r"C:\wt", "log", "--format=%H%x09%s", "base123..HEAD"): ok(stdout="")}
    )
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    result = runner.verify(fake, task, Path(r"C:\wt"), "base123")
    assert result.status == "rejected", "sanity: real verify rejects zero commits"

    original_run = fake.run

    def patched_run(cmd, **kwargs):
        if cmd[:4] == ["git", "-C", r"C:\wt", "log"]:
            return CommandResult(args=cmd, returncode=0, stdout="deadbeef\tDEMO-1: fake commit", stderr="")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(fake, "run", patched_run)
    fake2 = fake
    # also stub the diff/acceptance calls this path now reaches
    fake2.script[("git", "-C", r"C:\wt", "diff", "--name-only", "base123..HEAD")] = ok(stdout="README.md")
    fake2.script[("sh", "-lc")] = ok(stdout="1 passed")
    result2 = runner.verify(fake2, task, Path(r"C:\wt"), "base123")
    assert result2.status != "rejected" or "no commits" not in result2.note, (
        "mutated git log wrongly fabricated a commit that was never there"
    )


def test_verify_rejects_allowed_paths_violation():
    fake = FakeCommandRunner(
        script={
            ("git", "-C", r"C:\wt", "log", "--format=%H%x09%s", "base123..HEAD"): _git_log_result(
                [("abc1234", "DEMO-1: edit readme")]
            ),
            ("git", "-C", r"C:\wt", "diff", "--name-only", "base123..HEAD"): ok(stdout="README.md\nsecrets.env"),
        }
    )
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))  # allowed_paths: README.md only
    result = runner.verify(fake, task, Path(r"C:\wt"), "base123")
    assert result.status == "rejected"
    assert result.allowed_paths_violations == ["secrets.env"]
    # acceptance must NOT have been run once a violation is found
    assert not any(c[0] not in ("git",) for c in fake.calls)


def test_mutation_allowed_paths_check_disabled_breaks_violation_test(monkeypatch):
    fake = FakeCommandRunner(
        script={
            ("git", "-C", r"C:\wt", "log", "--format=%H%x09%s", "base123..HEAD"): _git_log_result(
                [("abc1234", "DEMO-1: edit readme")]
            ),
            ("git", "-C", r"C:\wt", "diff", "--name-only", "base123..HEAD"): ok(stdout="README.md\nsecrets.env"),
            ("sh", "-lc"): ok(stdout="1 passed"),
        }
    )
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    result = runner.verify(fake, task, Path(r"C:\wt"), "base123")
    assert result.status == "rejected", "sanity: real verify rejects the out-of-bounds file"

    monkeypatch.setattr(runner.fleet1, "globs_overlap", lambda a, b: True)  # pretend everything matches
    fake2 = FakeCommandRunner(script=fake.script)
    result2 = runner.verify(fake2, task, Path(r"C:\wt"), "base123")
    assert result2.status != "rejected", "mutated glob check wrongly accepted a file outside allowed_paths"


def test_verify_reports_failing_acceptance():
    fake = FakeCommandRunner(
        script={
            ("git", "-C", r"C:\wt", "log", "--format=%H%x09%s", "base123..HEAD"): _git_log_result(
                [("abc1234", "DEMO-1: edit readme")]
            ),
            ("git", "-C", r"C:\wt", "diff", "--name-only", "base123..HEAD"): ok(stdout="README.md"),
            ("sh", "-lc"): fail(returncode=1, stdout="", stderr="1 failed"),
        }
    )
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    result = runner.verify(fake, task, Path(r"C:\wt"), "base123")
    assert result.status == "failed"
    assert result.acceptance_results[0].exit_code == 1
    assert result.acceptance_results[0].passed is False


def test_mutation_acceptance_exit_check_disabled_breaks_failing_acceptance_test(monkeypatch):
    fake = FakeCommandRunner(
        script={
            ("git", "-C", r"C:\wt", "log", "--format=%H%x09%s", "base123..HEAD"): _git_log_result(
                [("abc1234", "DEMO-1: edit readme")]
            ),
            ("git", "-C", r"C:\wt", "diff", "--name-only", "base123..HEAD"): ok(stdout="README.md"),
            ("sh", "-lc"): fail(returncode=1, stdout="", stderr="1 failed"),
        }
    )
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    result = runner.verify(fake, task, Path(r"C:\wt"), "base123")
    assert result.status == "failed", "sanity: real verify reports the failing acceptance command"

    original_verify = runner.verify

    def mutated_verify(r, t, wt, base):
        res = original_verify(r, t, wt, base)
        for ar in res.acceptance_results:
            ar.passed = True
        if res.acceptance_results:
            res.status = "succeeded"
        return res

    monkeypatch.setattr(runner, "verify", mutated_verify)
    fake2 = FakeCommandRunner(script=fake.script)
    result2 = runner.verify(fake2, task, Path(r"C:\wt"), "base123")
    assert result2.status == "succeeded", "mutated verify wrongly reported success for a failing acceptance command"


def test_verify_rejects_commit_message_missing_task_id_prefix():
    fake = FakeCommandRunner(
        script={
            ("git", "-C", r"C:\wt", "log", "--format=%H%x09%s", "base123..HEAD"): _git_log_result(
                [("abc1234", "fix: forgot the task id prefix")]
            ),
        }
    )
    task = runner.load_single_task(_task_path("sandbox_happy", "DEMO-1"))
    result = runner.verify(fake, task, Path(r"C:\wt"), "base123")
    assert result.status == "rejected"
    assert "do not start with task id" in result.note


# --- status / clean --------------------------------------------------------------


def test_clean_requires_recorded_state(tmp_path):
    fake = FakeCommandRunner(script={}, default=ok())
    with pytest.raises(ValueError, match="no recorded state"):
        runner.clean_task(fake, tmp_path, "NEVER-RAN")


def test_clean_removes_recorded_worktree(tmp_path):
    runner.save_state(tmp_path, "DEMO-1", {"task_id": "DEMO-1", "workspace_id": "w9"})
    fake = FakeCommandRunner(script={("herdr", "worktree", "remove"): ok()})
    msg = runner.clean_task(fake, tmp_path, "DEMO-1")
    assert "cleaned DEMO-1" in msg
    assert runner.load_state(tmp_path, "DEMO-1") is None
    [cmd] = fake.calls
    assert cmd[:3] == ["herdr", "worktree", "remove"]
    assert "--workspace" in cmd and cmd[cmd.index("--workspace") + 1] == "w9"
