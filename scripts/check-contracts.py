#!/usr/bin/env python3
"""Validate the contract registry and detect vendoring drift (R1.2, ADR 0004).

Four checks, all run before exiting so one command reports everything:

  1. schemas   — every contracts/*.json is a valid JSON Schema Draft 2020-12, declares the
                 2020-12 meta-schema, and carries an $id matching its filename.
  2. examples  — contracts/examples/<contract>/valid-*.json validate; invalid-*.json fail;
                 each contract has at least 2 valid and 3 invalid examples, and no two
                 invalid examples of one contract break the same rule.
  3. vendoring — every service listed in contracts/vendoring.json is a checked-out
                 submodule, and every file it lists is byte-identical to the root copy at
                 services/<service>/schemas/contracts/<file>.
  4. manifest  — every file named in the manifest exists in contracts/.

This runs in the workspace repo only: it is the one place that sees contracts/ and both
submodules at once (ADR 0004 rule 4). An uninitialised submodule is a FAILURE, not a skip —
otherwise the check passes vacuously in exactly the situation it exists to catch.

Exit status: 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"
EXAMPLES = CONTRACTS / "examples"
MANIFEST = CONTRACTS / "vendoring.json"
SERVICES = ROOT / "services"
VENDOR_SUBDIR = Path("schemas") / "contracts"

META_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
ID_BASE = "https://github.com/jorgevr/anomalIA-app/contracts/"

MIN_VALID_EXAMPLES = 2
MIN_INVALID_EXAMPLES = 3

# Not every environment registers a checker for every format (date-time needs
# rfc3339-validator, for instance), so the contracts back each format with an equivalent
# pattern. Format assertion is still switched on here to catch what it can.
FORMAT_CHECKER = Draft202012Validator.FORMAT_CHECKER


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


def contract_files() -> list[Path]:
    return sorted(p for p in CONTRACTS.glob("*.json") if p.name != MANIFEST.name)


def load_json(path: Path) -> tuple[dict | None, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except OSError as exc:
        return None, f"unreadable: {exc.strerror}"
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"


def rule_signature(errors: list) -> frozenset[tuple[str, str]]:
    """Identify which rule(s) an instance broke, so two invalid examples can be compared.

    A rule is the validating keyword plus its location in the schema, which distinguishes
    'required at the root' from 'required inside data' and from 'required under then'.
    """
    return frozenset(
        (str(error.validator), "/".join(str(part) for part in error.absolute_schema_path))
        for error in errors
    )


def first_difference(root: bytes, vendored: bytes) -> str:
    """Locate the drift precisely — a one-byte edit is the case this check exists to catch."""
    if root.replace(b"\r\n", b"\n") == vendored.replace(b"\r\n", b"\n"):
        # Same content, different newlines: a .gitattributes divergence between this repo and
        # the submodule, not an edit. Still a failure — byte-identity is the guarantee — but
        # the fix is the attributes, not the contract.
        return (
            "line endings only: content matches after newline normalisation. Align the "
            "submodule's .gitattributes with this repo's so the vendored copy checks out "
            "with the same newlines"
        )
    for offset, (a, b) in enumerate(zip(root, vendored)):
        if a != b:
            return (
                f"first differing byte at offset {offset}: "
                f"root {bytes([a])!r} vs vendored {bytes([b])!r}"
            )
    longer, label = (
        (vendored, "vendored") if len(vendored) > len(root) else (root, "root")
    )
    return (
        f"identical for {min(len(root), len(vendored))} bytes, then {label} continues "
        f"for {len(longer) - min(len(root), len(vendored))} more"
    )


def check_schemas(report: Report) -> dict[str, Draft202012Validator]:
    validators: dict[str, Draft202012Validator] = {}
    files = contract_files()
    if not files:
        report.add("schemas", "contracts/*.json", False, "no contract files found")
        return validators

    for path in files:
        subject = f"contracts/{path.name}"
        doc, err = load_json(path)
        if doc is None:
            report.add("schemas", subject, False, err)
            continue

        problems = []
        if doc.get("$schema") != META_SCHEMA:
            problems.append(f"$schema is {doc.get('$schema')!r}, expected {META_SCHEMA!r}")
        expected_id = ID_BASE + path.name
        if doc.get("$id") != expected_id:
            problems.append(f"$id is {doc.get('$id')!r}, expected {expected_id!r}")
        try:
            Draft202012Validator.check_schema(doc)
        except SchemaError as exc:
            problems.append(f"not a valid Draft 2020-12 schema: {exc.message}")

        if problems:
            report.add("schemas", subject, False, "; ".join(problems))
            continue

        validators[path.name] = Draft202012Validator(doc, format_checker=FORMAT_CHECKER)
        report.add("schemas", subject, True, "Draft 2020-12")

    return validators


def check_examples(report: Report, validators: dict[str, Draft202012Validator]) -> None:
    if not EXAMPLES.is_dir():
        report.add("examples", "contracts/examples/", False, "directory missing")
        return

    dirs = sorted(p for p in EXAMPLES.iterdir() if p.is_dir())
    expected = {path.name.removesuffix(".json") for path in contract_files()}
    seen = {d.name for d in dirs}

    for missing in sorted(expected - seen):
        report.add("examples", f"examples/{missing}/", False, "no examples for this contract")
    for extra in sorted(seen - expected):
        report.add("examples", f"examples/{extra}/", False, "no contract of this name")

    for directory in dirs:
        contract_name = f"{directory.name}.json"
        validator = validators.get(contract_name)
        if validator is None:
            report.add(
                "examples",
                f"examples/{directory.name}/",
                False,
                "contract did not load; examples not checked",
            )
            continue

        valid_files = sorted(directory.glob("valid-*.json"))
        invalid_files = sorted(directory.glob("invalid-*.json"))
        unclassified = sorted(
            p.name
            for p in directory.glob("*.json")
            if not p.name.startswith(("valid-", "invalid-"))
        )

        for path in valid_files:
            subject = f"examples/{directory.name}/{path.name}"
            doc, err = load_json(path)
            if doc is None:
                report.add("examples", subject, False, err)
                continue
            errors = sorted(validator.iter_errors(doc), key=lambda e: e.json_path)
            if errors:
                first = errors[0]
                report.add(
                    "examples",
                    subject,
                    False,
                    f"expected valid, got {len(errors)} error(s): {first.json_path}: {first.message}",
                )
            else:
                report.add("examples", subject, True, "valid, as expected")

        signatures: dict[frozenset, str] = {}
        for path in invalid_files:
            subject = f"examples/{directory.name}/{path.name}"
            doc, err = load_json(path)
            if doc is None:
                report.add("examples", subject, False, err)
                continue
            errors = list(validator.iter_errors(doc))
            if not errors:
                report.add("examples", subject, False, "expected invalid, but it validated")
                continue

            signature = rule_signature(errors)
            clash = signatures.get(signature)
            if clash is not None:
                report.add(
                    "examples",
                    subject,
                    False,
                    f"breaks the same rule as {clash}; each invalid example must break a different one",
                )
                continue
            signatures[signature] = path.name
            broken = ", ".join(sorted(validator_name for validator_name, _ in signature))
            report.add("examples", subject, True, f"rejected on: {broken}")

        counts_ok = (
            len(valid_files) >= MIN_VALID_EXAMPLES
            and len(invalid_files) >= MIN_INVALID_EXAMPLES
            and not unclassified
        )
        note = f"{len(valid_files)} valid, {len(invalid_files)} invalid"
        if unclassified:
            note += f"; unclassified: {', '.join(unclassified)}"
        elif not counts_ok:
            note += f" (need >= {MIN_VALID_EXAMPLES} valid, >= {MIN_INVALID_EXAMPLES} invalid)"
        report.add("examples", f"examples/{directory.name}/ coverage", counts_ok, note)


def submodule_checked_out(path: Path) -> bool:
    """An uninitialised submodule is an empty directory, or absent entirely."""
    if not path.is_dir():
        return False
    return any(path.iterdir())


def check_vendoring(report: Report) -> None:
    manifest, err = load_json(MANIFEST)
    if manifest is None:
        report.add("vendoring", "contracts/vendoring.json", False, err or "unreadable")
        return
    if not isinstance(manifest, dict):
        report.add("vendoring", "contracts/vendoring.json", False, "expected an object of service -> [files]")
        return

    known = {path.name for path in contract_files()}
    total = sum(len(files) for files in manifest.values() if isinstance(files, list))
    report.add(
        "vendoring",
        "contracts/vendoring.json",
        True,
        f"{len(manifest)} service(s), {total} vendored file(s)",
    )

    for service, files in sorted(manifest.items()):
        service_dir = SERVICES / service
        subject = f"services/{service}"

        if not isinstance(files, list):
            report.add("vendoring", subject, False, "manifest entry is not a list of filenames")
            continue

        # Checked out before anything else: a missing submodule must fail the run, not
        # silently reduce it to zero comparisons.
        if not submodule_checked_out(service_dir):
            report.add(
                "vendoring",
                subject,
                False,
                "submodule listed in the manifest is not checked out "
                "(run: git submodule update --init --recursive)",
            )
            continue
        report.add("vendoring", subject, True, "submodule checked out")

        for name in files:
            subject = f"services/{service}/{VENDOR_SUBDIR.as_posix()}/{name}"
            root_copy = CONTRACTS / name
            if name not in known:
                report.add("vendoring", subject, False, f"contracts/{name} does not exist")
                continue
            vendored = service_dir / VENDOR_SUBDIR / name
            if not vendored.is_file():
                report.add("vendoring", subject, False, "vendored copy missing")
                continue
            root_bytes = root_copy.read_bytes()
            vendored_bytes = vendored.read_bytes()
            if root_bytes != vendored_bytes:
                report.add(
                    "vendoring",
                    subject,
                    False,
                    f"drift from contracts/{name}: {first_difference(root_bytes, vendored_bytes)}",
                )
            else:
                report.add("vendoring", subject, True, f"byte-identical ({len(root_bytes)} bytes)")


def main() -> int:
    print(f"Workspace root:  {ROOT}")
    print(f"Registry:        {CONTRACTS.relative_to(ROOT)}")
    print(f"Format checkers: {', '.join(sorted(FORMAT_CHECKER.checkers)) or 'none'}")
    print()

    report = Report()
    validators = check_schemas(report)
    check_examples(report, validators)
    check_vendoring(report)
    report.print()

    if report.ok:
        print("OK: contracts valid, examples behave as declared, no vendoring drift.")
        return 0
    print("FAILED: see the rows marked FAIL above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
