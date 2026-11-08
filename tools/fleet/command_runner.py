"""The one seam between tools/fleet/runner.py and the outside world (FLEET-2).

Every external command the runner issues -- git, herdr, gh, docker-lock file
operations via plain Path calls stay outside this, but every subprocess -- goes
through a CommandRunner. Tests inject FakeCommandRunner instead of hitting real
git/herdr/gh, so the command sequence, JSON parsing, and reaction logic are all
testable without a live herdr session or network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(Protocol):
    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
    ) -> CommandResult: ...


class RealCommandRunner:
    """Actually shells out. The only implementation allowed to touch the real
    machine -- runner.py never imports subprocess directly."""

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        import subprocess

        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return CommandResult(
            args=list(cmd),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


@dataclass
class FakeCommandRunner:
    """Scripted responses for tests. `script` maps a command-prefix tuple to a
    CommandResult (or a callable returning one, for responses that depend on how
    many times a prefix has been called). Every call is recorded in `calls` so
    tests can assert the exact command sequence issued.

    Matching is by longest-matching-prefix: register the most specific tuple you
    care about (e.g. ("git", "push") before a catch-all ("git",)).
    """

    script: dict[tuple[str, ...], object] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    default: CommandResult | None = None

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        cmd = list(cmd)
        self.calls.append(cmd)

        best_match: tuple[str, ...] | None = None
        for prefix in self.script:
            if tuple(cmd[: len(prefix)]) == prefix:
                if best_match is None or len(prefix) > len(best_match):
                    best_match = prefix

        if best_match is not None:
            entry = self.script[best_match]
            if callable(entry):
                result = entry(cmd)
            else:
                result = entry
            if isinstance(result, CommandResult):
                return CommandResult(args=cmd, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
            raise TypeError(f"script entry for {best_match} did not resolve to a CommandResult: {result!r}")

        if self.default is not None:
            return CommandResult(args=cmd, returncode=self.default.returncode, stdout=self.default.stdout, stderr=self.default.stderr)

        raise AssertionError(f"FakeCommandRunner: no script entry matches command {cmd!r}")


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(args=[], returncode=0, stdout=stdout, stderr=stderr)


def fail(returncode: int = 1, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
