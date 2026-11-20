"""Tests for scripts/gen-task-prompt.py's render() (the fix described in the
fleet-prompt-fix task): the prompt must inline everything the worker needs
instead of pointing it at fleet/tasks/<id>.json, must spell out what a model
tier letter actually means, and must get the `.github/workflows/**` hard-deny
rule right for a task like CI-4 that names an exact workflow file in its own
allowed_paths -- the old render() denied every workflow file unconditionally,
contradicting docs/fleet.md's own documented escape hatch.

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
TASKS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "fleet" / "tasks"


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


# --- model tier letters are spelled out, not left as a bare letter ------------------


def test_describe_model_known_tier_is_spelled_out(gen):
    assert gen.describe_model("A") != "A"
    out = gen.describe_model("C")
    assert out.startswith("C (")
    assert "Tier C" in out
    assert "Ollama" in out


def test_describe_model_unknown_value_passed_through(gen):
    assert gen.describe_model("claude-sonnet-5") == "claude-sonnet-5"


def test_render_model_line_is_not_a_bare_letter(gen):
    out = gen.render(_task(model="C"))
    assert "Model: C (" in out
    assert "Model: C\n" not in out and "Model: C |" not in out


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
    exists to cover (its allowed_paths must still name the workflow file verbatim)."""
    task = json.loads((TASKS_DIR / "CI-4.json").read_text(encoding="utf-8"))
    assert ".github/workflows/ci.yml" in task["allowed_paths"]
    out = gen.render(task)
    assert OLD_ABSOLUTE_SENTENCE_FRAGMENT not in out
    assert "`.github/workflows/ci.yml`" in out
    assert "may be touched" in out
