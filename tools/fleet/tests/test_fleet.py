"""Tests for tools/fleet/fleet.py (FLEET-1 and its follow-up).

Each invalid fixture under fixtures/ carries exactly one defect and is asserted to
fail the relevant check with a message that names it, so a regression in fleet.py
shows up as a specific failing test rather than a silent pass.

A `test_mutation_*` function exists for every invalid-fixture check this module owns
(dotdot, absolute, overlap, missing_id, unknown_need, cycle): each disables exactly
that check via monkeypatch, shows the corresponding fixture test's assertion would now
fail (the check no longer catches the defect it exists for), then the monkeypatch
fixture restores the real function automatically at teardown.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from conftest import FIXTURES


def _ok(capsys, fn, *args):
    code = fn(*args)
    out = capsys.readouterr().out
    return code, out


# --- valid fixture ------------------------------------------------------------------


def test_valid_fixture_passes_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "valid")
    assert code == 0
    assert "FAIL" not in out
    assert "OK: all task files valid." in out


def test_valid_fixture_plan_has_two_layers_and_right_readiness(fleet, capsys):
    code, out = _ok(capsys, fleet.run_plan, FIXTURES / "valid")
    assert code == 0
    assert "2 task(s), 2 layer(s)" in out
    assert "Layer 0" in out and "Layer 1" in out
    assert "A1" in out and "ready" in out
    # B1 needs known task A1; fleet.py has no execution-state tracking yet, so a
    # known-task need is always conservatively reported as blocking (see
    # compute_readiness's docstring).
    assert "blocked on: A1" in out
    assert "none -- every layer's fleet tasks can run fully in parallel." in out
    assert "External path overlaps" in out and "none." in out


# --- invalid fixtures: each must fail validate with a message naming the defect ----


def test_missing_id_fails_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "missing_id")
    assert code == 1
    assert "FAIL" in out
    assert "'id' is a required property" in out


def test_cycle_fails_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "cycle")
    assert code == 1
    assert "FAIL" in out
    assert "cycle:" in out
    assert "X" in out and "Y" in out


def test_unknown_need_fails_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "unknown_need")
    assert code == 1
    assert "FAIL" in out
    assert "NOPE-999" in out
    assert "not a fleet/tasks/ id and not a fleet/external.json key" in out


def test_dotdot_path_fails_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "dotdot_path")
    assert code == 1
    assert "FAIL" in out
    assert "contains a '..' path segment" in out


def test_absolute_path_fails_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "absolute_path")
    assert code == 1
    assert "FAIL" in out
    assert "absolute path is not allowed" in out


# --- overlapping paths: valid on its own, but plan must flag the pair --------------


def test_overlapping_paths_passes_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "overlapping_paths")
    assert code == 0
    assert "FAIL" not in out


def test_overlapping_paths_flagged_by_plan(fleet, capsys):
    code, out = _ok(capsys, fleet.run_plan, FIXTURES / "overlapping_paths")
    assert code == 0
    assert "Must serialize" in out
    assert "M1 <-> M2" in out


# --- external.json: a need must resolve to a fleet task OR an external.json entry --


def test_external_resolved_need_passes_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "external_resolved")
    assert code == 0
    assert "FAIL" not in out
    assert "resolved to fleet/external.json, actual state='merged'" in out


def test_external_resolved_task_is_ready_in_plan(fleet, capsys):
    code, out = _ok(capsys, fleet.run_plan, FIXTURES / "external_resolved")
    assert code == 0
    assert "N1" in out and "ready" in out
    assert "blocked" not in out


# --- external path overlap: only OPEN external items are flagged ------------------


def test_external_overlap_flags_open_not_merged(fleet, capsys):
    code, out = _ok(capsys, fleet.run_plan, FIXTURES / "external_overlap")
    assert code == 0
    assert "O1 overlaps external EXT-2 (open)" in out
    assert "EXT-3" not in out  # merged -> must not be flagged


# --- agent object (task.v1.json, optional) -----------------------------------------


def test_agent_object_valid_passes_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "agent_valid")
    assert code == 0
    assert "FAIL" not in out


def test_agent_object_invalid_fails_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "agent_invalid")
    assert code == 1
    assert "FAIL" in out
    assert "agent" in out
    assert "'cli' is a required property" in out


# --- herdr object (result.v1.json, optional) -- schema-level, no CLI subcommand ----


def _base_result_doc(**overrides) -> dict:
    doc = {
        "task_id": "R2.11",
        "status": "succeeded",
        "started_at": "2026-10-17T10:00:00Z",
        "finished_at": "2026-10-17T10:20:00Z",
        "branch": "r2-11-delete-csv-normalizer",
        "base_sha": "abc1234",
        "head_sha": "def5678",
        "commits": ["def5678"],
        "files_changed": ["src/csv_normalizer.py", "tests/unit/test_csv_normalizer.py"],
        "diff_stat": {"files": 2, "insertions": 0, "deletions": 120},
        "allowed_paths_violations": [],
        "acceptance_results": [
            {"name": "ruff", "exit_code": 0, "output_tail": "All checks passed!"},
            {"name": "pytest", "exit_code": 0, "output_tail": "329 passed"},
        ],
        "agent_report": "Deleted the orphaned module and its test.",
        "pr": None,
        "reviewer": None,
        "cost": {"turns": 6, "usd": 0.42},
    }
    doc.update(overrides)
    return doc


def test_herdr_object_valid_passes_result_schema(fleet):
    schema_doc, _ = fleet.load_json(fleet.ROOT / "fleet" / "schemas" / "result.v1.json")
    validator = Draft202012Validator(schema_doc)
    doc = _base_result_doc(
        herdr={"pane": "Developer", "cli": "claude", "worktree": "C:/code/review-R2.11", "session_id": "sess-1"}
    )
    errors = list(validator.iter_errors(doc))
    assert errors == []


def test_herdr_object_invalid_fails_result_schema(fleet):
    schema_doc, _ = fleet.load_json(fleet.ROOT / "fleet" / "schemas" / "result.v1.json")
    validator = Draft202012Validator(schema_doc)
    doc = _base_result_doc(herdr={"pane": "Developer", "cli": "claude"})  # missing required 'worktree'
    errors = list(validator.iter_errors(doc))
    assert errors
    assert any("worktree" in e.message for e in errors)


# --- glob overlap helper, unit level -------------------------------------------------


def test_globs_overlap_examples(fleet):
    assert fleet.globs_overlap("docs/**", "docs/overview.md")
    assert fleet.globs_overlap("tests/**", "tests/ci/**")
    assert fleet.globs_overlap("src/*.py", "src/main.py")
    assert not fleet.globs_overlap("src/**", "docs/**")
    assert not fleet.globs_overlap("src/a.py", "src/b.py")


# --- mutation proofs: one per invalid-fixture check this module owns ---------------
#
# Pattern for each: (1) unmutated run correctly fails/flags; (2) monkeypatch disables
# exactly that check; (3) the same run now wrongly passes -- i.e. the assertion the
# corresponding fixture test makes would flip from green to red. monkeypatch restores
# the original function automatically when the test ends (= "restore, show green":
# the next test in the module, or a rerun of this one, observes the real function again).


def test_mutation_dotdot_check_disabled_breaks_dotdot_fixture(fleet, capsys, monkeypatch):
    code, _ = _ok(capsys, fleet.run_validate, FIXTURES / "dotdot_path")
    assert code == 1, "sanity: real check catches it"

    monkeypatch.setattr(fleet, "_path_escapes_repo", lambda path: False)
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "dotdot_path")
    assert code == 0, "mutated validator wrongly accepted a '..' path"
    assert "FAIL" not in out


def test_mutation_absolute_check_disabled_breaks_absolute_fixture(fleet, capsys, monkeypatch):
    code, _ = _ok(capsys, fleet.run_validate, FIXTURES / "absolute_path")
    assert code == 1, "sanity: real check catches it"

    monkeypatch.setattr(fleet, "_path_is_absolute", lambda path: False)
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "absolute_path")
    assert code == 0, "mutated validator wrongly accepted an absolute path"
    assert "FAIL" not in out


def test_mutation_overlap_check_disabled_breaks_overlapping_paths_fixture(fleet, capsys, monkeypatch):
    code, out = _ok(capsys, fleet.run_plan, FIXTURES / "overlapping_paths")
    assert "M1 <-> M2" in out, "sanity: real check flags it"

    monkeypatch.setattr(fleet, "find_overlapping_paths", lambda a, b: [])
    code, out = _ok(capsys, fleet.run_plan, FIXTURES / "overlapping_paths")
    assert code == 0
    assert "M1 <-> M2" not in out, "mutated overlap check wrongly found no overlap"
    assert "none -- every layer's fleet tasks can run fully in parallel." in out


def test_mutation_missing_id_check_disabled_breaks_missing_id_fixture(fleet, capsys, monkeypatch):
    code, _ = _ok(capsys, fleet.run_validate, FIXTURES / "missing_id")
    assert code == 1, "sanity: real check catches it"

    schema_doc, _ = fleet.load_json(fleet.TASK_SCHEMA_PATH)
    schema_doc["required"] = [r for r in schema_doc["required"] if r != "id"]
    mutated_validator = Draft202012Validator(schema_doc)
    monkeypatch.setattr(fleet, "load_task_schema", lambda: mutated_validator)

    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "missing_id")
    assert code == 0, "mutated schema wrongly accepted a task file with no id"
    assert "FAIL" not in out


def test_mutation_unknown_need_check_disabled_breaks_unknown_need_fixture(fleet, capsys, monkeypatch):
    code, _ = _ok(capsys, fleet.run_validate, FIXTURES / "unknown_need")
    assert code == 1, "sanity: real check catches it"

    monkeypatch.setattr(fleet, "check_needs", lambda report, tasks, external: None)
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "unknown_need")
    assert code == 0, "mutated validator wrongly accepted an unresolvable need"
    assert "FAIL" not in out


def test_mutation_cycle_check_disabled_breaks_cycle_fixture(fleet, capsys, monkeypatch):
    code, _ = _ok(capsys, fleet.run_validate, FIXTURES / "cycle")
    assert code == 1, "sanity: real check catches it"

    monkeypatch.setattr(fleet, "_find_cycle", lambda tasks: None)
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "cycle")
    assert code == 0, "mutated validator wrongly accepted a cyclic needs graph"
    assert "cycle:" not in out
