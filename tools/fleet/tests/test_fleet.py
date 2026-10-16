"""Tests for tools/fleet/fleet.py (FLEET-1).

Each invalid fixture under fixtures/ carries exactly one defect and is asserted to
fail the relevant check with a message that names it, so a regression in fleet.py
shows up as a specific failing test rather than a silent pass.
"""

from __future__ import annotations

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


def test_valid_fixture_plan_has_two_layers_and_no_overlap(fleet, capsys):
    code, out = _ok(capsys, fleet.run_plan, FIXTURES / "valid")
    assert code == 0
    assert "2 task(s), 2 layer(s)" in out
    assert "Layer 0" in out
    assert "Layer 1" in out
    assert "No same-repo/same-layer path overlaps" in out


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
    assert "not 'merged'" in out


def test_dotdot_path_fails_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "dotdot_path")
    assert code == 1
    assert "FAIL" in out
    assert "allowed_paths" in out


def test_absolute_path_fails_validate(fleet, capsys):
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "absolute_path")
    assert code == 1
    assert "FAIL" in out
    assert "allowed_paths" in out


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


# --- glob overlap helper, unit level -------------------------------------------------


def test_globs_overlap_examples(fleet):
    assert fleet.globs_overlap("docs/**", "docs/overview.md")
    assert fleet.globs_overlap("tests/**", "tests/ci/**")
    assert fleet.globs_overlap("src/*.py", "src/main.py")
    assert not fleet.globs_overlap("src/**", "docs/**")
    assert not fleet.globs_overlap("src/a.py", "src/b.py")


# --- mutation: proves the cycle fixture test above actually exercises the check ----


def test_mutation_disabling_cycle_detection_is_caught_by_the_cycle_fixture(fleet, capsys, monkeypatch):
    # Unmutated: the cycle fixture correctly fails.
    code, _ = _ok(capsys, fleet.run_validate, FIXTURES / "cycle")
    assert code == 1

    # Mutate: break cycle detection so it always reports "no cycle found". If this
    # mutation ever landed for real, test_cycle_fails_validate would go red -- that
    # is what this test demonstrates by reproducing the flip directly.
    monkeypatch.setattr(fleet, "_find_cycle", lambda tasks: None)
    code, out = _ok(capsys, fleet.run_validate, FIXTURES / "cycle")
    assert code == 0, "mutated validator wrongly accepted a cyclic needs graph"
    assert "cycle:" not in out
