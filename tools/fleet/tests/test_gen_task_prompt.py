"""Tests for scripts/gen-task-prompt.py's render() (the fix described in the
fleet-prompt-fix task): the prompt must inline everything the worker needs
instead of pointing it at fleet/tasks/<id>.json, must never leak the
orchestrator's own "model" dispatch metadata into the agent's instructions,
and must get the `.github/workflows/**` hard-deny rule right for a task like
CI-4 that names an exact workflow file in its own allowed_paths -- the old
render() denied every workflow file unconditionally, contradicting
docs/fleet.md's own documented escape hatch.

scripts/gen-task-prompt.py is a standalone script (hyphenated filename, not an
importable package), so it is loaded the same way conftest.py loads fleet.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "gen-task-prompt.py"
DONE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "fleet" / "done"


@pytest.fixture
def gen(monkeypatch):
    monkeypatch.setenv("FLEET_HOME", str(Path(__file__).resolve().parent))  # never touch the real one
    spec = importlib.util.spec_from_file_location("gen_task_prompt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_task_prompt"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("gen_task_prompt", None)


def _task(**overrides) -> dict:
    base = {
        "id": "T1",
        "title": "Example task",
        "repo": "ingestion-func",
        "base": "main",
        "branch": "t1-example",
        "needs": [{"task": "R2.9", "state": "merged"}],
        "allowed_paths": ["src/thing.py", "tests/unit/test_thing.py"],
        "acceptance": [
            {"name": "pytest", "cwd": ".", "command": "pytest", "expect_exit": 0},
        ],
        "prompt_file": "fleet/prompts/T1.md",
        "model": "B",
        "max_turns": 20,
        "risk": "code",
    }
    base.update(overrides)
    return base


# --- the worker is never told to go read fleet/tasks/<id>.json ----------------------


def test_render_never_references_task_json(gen):
    out = gen.render(_task())
    assert "fleet/tasks/T1.json" not in out
    assert "trust the file" not in out


def test_render_inlines_branch_needs_allowed_paths_and_acceptance(gen):
    task = _task()
    out = gen.render(task)
    assert "t1-example" in out  # branch
    assert "`R2.9` must be **merged**" in out  # needs
    assert "`src/thing.py`" in out and "`tests/unit/test_thing.py`" in out  # allowed_paths
    assert "`pytest` -- must exit 0" in out  # acceptance


# --- "model" is orchestrator dispatch metadata, never rendered into the prompt -----


def test_render_never_mentions_model(gen):
    # "A"/"B"/"C" are single letters that legitimately occur elsewhere in the
    # rendered text (e.g. "AGENTS.md"), so only the distinctive multi-char
    # model id is checked for a literal leak; the "Model" header word itself
    # covers the tier-letter cases.
    for model in ("A", "B", "C", "claude-sonnet-5"):
        out = gen.render(_task(model=model))
        assert "Model" not in out
    assert "claude-sonnet-5" not in gen.render(_task(model="claude-sonnet-5"))


# --- .github/workflows/** hard-deny rule, including CI-4's exact-file exception -----


OLD_ABSOLUTE_SENTENCE_FRAGMENT = (
    "or `.github/workflows/**` -- hard-denied for every fleet task regardless of what "
    "`allowed_paths` says"
)


def test_exact_workflow_file_in_allowed_paths_is_not_absolutely_denied(gen):
    task = _task(
        repo="root",
        allowed_paths=["tests/unit/test_adls_store.py", ".github/workflows/ci.yml"],
    )
    out = gen.render(task)
    assert OLD_ABSOLUTE_SENTENCE_FRAGMENT not in out
    assert "`.github/workflows/ci.yml` " in out or "`.github/workflows/ci.yml`," in out
    assert "may be touched" in out
    # still hard-denied unconditionally: .claude/**, .mcp.json, .agents/**, .codex/**
    assert "`.agents/**`" in out and "`.codex/**`" in out


def test_no_exact_workflow_file_stays_fully_denied(gen):
    task = _task(allowed_paths=["src/thing.py"])
    out = gen.render(task)
    assert "does not name any workflow file verbatim" in out
    assert ".github/workflows/ci.yml" not in out


def test_ci4_task_file_matches_this_contract(gen):
    """Guards against CI-4.json itself drifting away from the scenario this task
    exists to cover (its allowed_paths must still name the workflow file verbatim).
    CI-4 shipped and lives in fleet/done/ now, not fleet/tasks/."""
    task = json.loads((DONE_DIR / "CI-4.json").read_text(encoding="utf-8"))
    assert ".github/workflows/ci.yml" in task["allowed_paths"]
    out = gen.render(task)
    assert OLD_ABSOLUTE_SENTENCE_FRAGMENT not in out
    assert "`.github/workflows/ci.yml`" in out
    assert "may be touched" in out
