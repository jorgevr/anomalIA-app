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


# --- hard default deny: .claude/**, .mcp.json, .github/workflows/** ---------------


def _make_task(allowed_paths, task_id="HD-1"):
    doc = {
        "id": task_id,
        "title": "hard-deny test task",
        "repo": "sandbox",
        "branch": "hd-1-branch",
        "needs": [],
        "allowed_paths": allowed_paths,
        "acceptance": [],
        "prompt_file": "fleet/prompts/HD-1.md",
        "model": "A",
        "max_turns": 5,
        "risk": "code",
    }
    return runner.fleet1.Task(Path("/fake/fleet/tasks/HD-1.json"), doc)


def _verify_with_changed_files(task, files: list[str]):
    fake = FakeCommandRunner(
        script={
            ("git", "-C", r"C:\wt", "log", "--format=%H%x09%s", "base123..HEAD"): _git_log_result(
                [("abc1234", f"{task.id}: change files")]
            ),
            ("git", "-C", r"C:\wt", "diff", "--name-only", "base123..HEAD"): ok(stdout="\n".join(files)),
            ("sh", "-lc"): ok(stdout="ok"),
        }
    )
    return runner.verify(fake, task, Path(r"C:\wt"), "base123")


@pytest.mark.parametrize(
    "path",
    [".claude/settings.json", ".claude/skills/foo/SKILL.md", ".mcp.json", ".github/workflows/ci.yml"],
)
def test_hard_deny_rejects_sensitive_paths_despite_broad_allowed_paths(path):
    task = _make_task(allowed_paths=["**"])  # as broad as it gets
    result = _verify_with_changed_files(task, [path])
    assert result.status == "rejected"
    assert result.allowed_paths_violations == [path]


def test_hard_deny_allows_exact_literal_exception_like_ci4():
    task = _make_task(allowed_paths=[".github/workflows/ci.yml", "tests/unit/test_adls_store.py"])
    result = _verify_with_changed_files(task, [".github/workflows/ci.yml"])
    assert result.status == "succeeded"


def test_hard_deny_exact_literal_exception_does_not_cover_other_files_in_same_dir():
    task = _make_task(allowed_paths=[".github/workflows/ci.yml"])
    result = _verify_with_changed_files(task, [".github/workflows/ci.yml", ".github/workflows/other.yml"])
    assert result.status == "rejected"
    assert result.allowed_paths_violations == [".github/workflows/other.yml"]


def test_mutation_hard_deny_check_disabled_breaks_hard_deny_tests(monkeypatch):
    task = _make_task(allowed_paths=["**"])
    result = _verify_with_changed_files(task, [".mcp.json"])
    assert result.status == "rejected", "sanity: real check rejects .mcp.json even under a broad allowed_paths"

    monkeypatch.setattr(runner, "is_hard_denied_path", lambda path: False)
    result2 = _verify_with_changed_files(task, [".mcp.json"])
    assert result2.status != "rejected", "mutated hard-deny check wrongly allowed .mcp.json through"


# --- status / clean --------------------------------------------------------------


# --- resume ------------------------------------------------------------------------


def _resume_state(tmp_path, agent_name="demo-1", prompt_sent=False, wt_path=None):
    task_path = _task_path("sandbox_happy", "DEMO-1")
    wt_path = wt_path or (tmp_path / "wt" / "DEMO-1")
    state = {
        "task_id": "DEMO-1",
        "task_path": str(task_path.resolve()),
        "workspace_id": "w9",
        "tab_id": "w9:t1",
        "pane_id": "w9:p1",
        "worktree_path": str(wt_path),
        "agent_name": agent_name,
        "prompt_sent": prompt_sent,
        "repo": "sandbox",
        "branch": "demo-1-branch",
        "created_at": "2026-10-18T20:00:00Z",
    }
    runner.save_state(tmp_path, "DEMO-1", state)
    return state, wt_path


def test_resume_refuses_without_recorded_state(tmp_path):
    fake = FakeCommandRunner(script={}, default=ok())
    with pytest.raises(runner.ResumeRefused, match="no recorded state"):
        runner.resume_task(fake, "NEVER-RAN", tmp_path)


def test_resume_refuses_when_worktree_missing(tmp_path):
    _, wt_path = _resume_state(tmp_path)
    fake = FakeCommandRunner(
        script={("git", "-C", str(wt_path), "rev-parse", "--is-inside-work-tree"): fail(returncode=128, stderr="not a git repository")}
    )
    with pytest.raises(runner.ResumeRefused, match="no longer exists"):
        runner.resume_task(fake, "DEMO-1", tmp_path)


def test_mutation_resume_worktree_check_disabled_breaks_missing_worktree_test(monkeypatch, tmp_path):
    _, wt_path = _resume_state(tmp_path)
    fake = FakeCommandRunner(
        script={("git", "-C", str(wt_path), "rev-parse", "--is-inside-work-tree"): fail(returncode=128, stderr="not a git repository")}
    )
    with pytest.raises(runner.ResumeRefused):
        runner.resume_task(fake, "DEMO-1", tmp_path)  # sanity

    # Mutate: pretend the worktree check always succeeds.
    original_run = FakeCommandRunner.run

    def always_ok_run(self, cmd, **kwargs):
        if cmd[:3] == ["git", "-C", str(wt_path)] and cmd[3:5] == ["rev-parse", "--is-inside-work-tree"]:
            return ok(stdout="true")
        return original_run(self, cmd, **kwargs)

    monkeypatch.setattr(FakeCommandRunner, "run", always_ok_run)
    fake2 = FakeCommandRunner(
        script={
            ("herdr", "agent", "get"): fail(returncode=1, stderr='{"error":{"code":"no_such_agent"}}'),
        }
    )
    with pytest.raises(runner.ResumeRefused, match="pane/agent no longer exists"):
        # still refused, but for a DIFFERENT reason now (agent gone, not worktree) --
        # proving the worktree-missing case alone is no longer caught once mutated.
        runner.resume_task(fake2, "DEMO-1", tmp_path)


def test_resume_refuses_when_result_already_has_pr(tmp_path):
    _resume_state(tmp_path)
    runner.write_result(
        tmp_path,
        {
            "task_id": "DEMO-1",
            "status": "succeeded",
            "started_at": "2026-10-18T20:00:00Z",
            "finished_at": "2026-10-18T20:05:00Z",
            "branch": "demo-1-branch",
            "base_sha": "abc1234",
            "head_sha": "def5678",
            "commits": ["def5678"],
            "files_changed": ["README.md"],
            "diff_stat": {"files": 1, "insertions": 1, "deletions": 0},
            "allowed_paths_violations": [],
            "acceptance_results": [],
            "agent_report": "",
            "pr": {"number": 7, "url": "https://github.com/jorgevr/fleet-sandbox/pull/7"},
            "reviewer": None,
            "cost": {"turns": 1, "usd": 0.0},
        },
    )
    fake = FakeCommandRunner(script={}, default=ok())
    with pytest.raises(runner.ResumeRefused, match="already has a PR"):
        runner.resume_task(fake, "DEMO-1", tmp_path)


def test_mutation_resume_pr_check_disabled_breaks_already_has_pr_test(monkeypatch, tmp_path):
    _resume_state(tmp_path)
    runner.write_result(
        tmp_path,
        {
            "task_id": "DEMO-1", "status": "succeeded", "started_at": "2026-10-18T20:00:00Z",
            "finished_at": "2026-10-18T20:05:00Z", "branch": "demo-1-branch", "base_sha": "abc1234",
            "head_sha": "def5678", "commits": ["def5678"], "files_changed": ["README.md"],
            "diff_stat": {"files": 1, "insertions": 1, "deletions": 0}, "allowed_paths_violations": [],
            "acceptance_results": [], "agent_report": "", "pr": {"number": 7, "url": "https://x/pull/7"},
            "reviewer": None, "cost": {"turns": 1, "usd": 0.0},
        },
    )
    fake = FakeCommandRunner(script={}, default=ok())
    with pytest.raises(runner.ResumeRefused, match="already has a PR"):
        runner.resume_task(fake, "DEMO-1", tmp_path)  # sanity

    monkeypatch.setattr(runner, "existing_result_pr", lambda fleet_home, task_id: None)
    fake2 = FakeCommandRunner(
        script={
            ("git", "-C"): ok(stdout="true"),
            ("herdr", "agent", "get"): fail(returncode=1, stderr='{"error":{"code":"no_such_agent"}}'),
        },
        default=ok(stdout="true"),
    )
    # With the PR guard mutated to always report "no PR", resume proceeds past it
    # to the next check instead of refusing immediately for "already has a PR".
    with pytest.raises(runner.ResumeRefused, match="pane/agent no longer exists"):
        runner.resume_task(fake2, "DEMO-1", tmp_path)


def test_resume_after_blocked_reports_still_blocked(tmp_path):
    _, wt_path = _resume_state(tmp_path, agent_name="demo-1", prompt_sent=False)
    sandbox_repo = str(runner.REPO_PATHS["sandbox"])
    fake = FakeCommandRunner(
        script={
            ("git", "-C", str(wt_path), "rev-parse", "--is-inside-work-tree"): ok(stdout="true"),
            ("git", "-C", sandbox_repo, "rev-parse", "origin/main"): ok(stdout="abc1234"),
            ("herdr", "agent", "get"): ok(stdout=json.dumps({"result": {"agent": {"agent_status": "blocked"}}})),
            ("herdr", "notification", "show"): ok(),
            ("herdr", "agent", "read"): ok(stdout=""),
        }
    )
    result = runner.resume_task(fake, "DEMO-1", tmp_path)
    assert result["status"] == "blocked"
    assert "still blocked" in result["agent_report"] or "blocked" in result["agent_report"]
    assert any(c[:3] == ["herdr", "notification", "show"] for c in fake.calls)
    # must NOT have sent a real kickoff prompt while still blocked
    assert not any(c[:3] == ["herdr", "agent", "prompt"] for c in fake.calls)


def _ready_to_verify_script(wt_path: Path, agent_status: str = "idle") -> dict:
    """Precise command->response keys for a resume that reaches finish_from_ready_agent
    with zero commits ahead of base (-> status 'rejected', but a schema-valid result:
    head_sha/base_sha must still look like real shas, never the string "true")."""
    sandbox_repo = str(runner.REPO_PATHS["sandbox"])
    return {
        ("git", "-C", str(wt_path), "rev-parse", "--is-inside-work-tree"): ok(stdout="true"),
        ("git", "-C", sandbox_repo, "rev-parse", "origin/main"): ok(stdout="abc1234"),
        ("git", "-C", str(wt_path), "rev-parse", "HEAD"): ok(stdout="abc1234"),
        ("git", "-C", str(wt_path), "log", "--format=%H%x09%s"): ok(stdout=""),
        ("git", "-C", str(wt_path), "diff", "--shortstat"): ok(stdout=""),
        ("herdr", "agent", "get"): ok(stdout=json.dumps({"result": {"agent": {"agent_status": agent_status}}})),
        ("herdr", "agent", "read"): ok(stdout=""),
    }


def test_resume_sends_prompt_when_not_yet_sent_and_agent_ready(tmp_path):
    _, wt_path = _resume_state(tmp_path, agent_name="demo-1", prompt_sent=False)
    script = _ready_to_verify_script(wt_path)
    script[("herdr", "agent", "prompt")] = ok(stdout=json.dumps({"result": {"status": "idle"}}))
    fake = FakeCommandRunner(script=script)

    result = runner.resume_task(fake, "DEMO-1", tmp_path)
    assert any(c[:3] == ["herdr", "agent", "prompt"] for c in fake.calls)
    assert result["status"] == "rejected"  # no commits ahead of base in this fake scenario
    state_after = runner.load_state(tmp_path, "DEMO-1")
    assert state_after["prompt_sent"] is True


def test_resume_skips_prompt_when_already_sent(tmp_path):
    _, wt_path = _resume_state(tmp_path, agent_name="demo-1", prompt_sent=True)
    fake = FakeCommandRunner(script=_ready_to_verify_script(wt_path))

    result = runner.resume_task(fake, "DEMO-1", tmp_path)
    assert not any(c[:3] == ["herdr", "agent", "prompt"] for c in fake.calls)
    assert result["status"] == "rejected"  # no commits ahead of base in this fake scenario


def test_resume_never_calls_worktree_create(tmp_path):
    _, wt_path = _resume_state(tmp_path, agent_name="demo-1", prompt_sent=True)
    fake = FakeCommandRunner(script=_ready_to_verify_script(wt_path))

    runner.resume_task(fake, "DEMO-1", tmp_path)
    assert not any(c[:3] == ["herdr", "worktree", "create"] for c in fake.calls)


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
