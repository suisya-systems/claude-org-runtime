"""Sandbox preflight / canary for a rendered ``settings.local.json``.

This is the *detection* half of the symlink-deny fix. The generator
(:mod:`claude_org_runtime.settings.generator`) canonicalizes deny paths it
renders itself, but a worker's effective deny set is the merge of several
settings scopes -- user ``~/.claude/settings.json``, project settings,
managed settings -- and only some of them come from this runtime. Any scope
can contribute a deny path that crosses an absolute symlink and takes the
whole sandbox down.

That failure is dangerously quiet. bubblewrap aborts at launch:

    bwrap: Can't create file at /home/<user>/.aws/config: No such file or
    directory

and Claude Code's documented escape hatch then retries the command with
``dangerouslyDisableSandbox``, so the session keeps working -- unsandboxed --
with no standing signal that isolation is off. An operator can believe the
sandbox is enforcing a boundary for months while it enforces nothing.

``sandbox doctor`` turns that into a loud, checkable failure:

1. **Static analysis** -- collect every deny path the settings contribute to
   the sandbox (Layer 3 ``sandbox.filesystem.deny{Read,Write}`` plus Layer 2
   ``permissions.deny`` ``Read`` / ``Edit`` rules, which Claude Code merges
   into the same set) and flag the ones whose component chain crosses an
   absolute symlink, with the realpath rewrite that would fix each.
2. **Live canary** -- when ``bwrap`` is present, actually launch it with the
   collected deny paths bound and report whether the sandbox comes up. This
   catches unbindable paths whose cause is something other than a symlink.

Exit status is non-zero when either check fails, so it can gate a worker
launch instead of being advisory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .generator import (
    _absolute_symlink_in_chain,
    _literal_path_prefix,
    _permission_rule_host_path,
    _split_permission_rule,
    _PERMISSION_PATH_TOOLS,
)

# Status values for a single deny target.
STATUS_OK = "ok"
STATUS_SYMLINK_ESCAPE = "symlink-escape"
STATUS_UNSUPPORTED = "unsupported-entry"

# Status values for the live bwrap canary.
CANARY_PASS = "pass"
CANARY_FAIL = "fail"
CANARY_SKIPPED = "skipped"


@dataclass(frozen=True)
class DenyTarget:
    """One concrete host path the settings contribute to the deny set."""

    layer: str  # "permissions.deny" | "sandbox.filesystem.denyRead" | ...
    source: Any  # the original rule / entry as authored
    path: str  # absolute host path (glob tail included)


@dataclass(frozen=True)
class Finding:
    """Verdict for a single deny target."""

    layer: str
    source: Any
    path: str
    status: str
    detail: str
    suggestion: str | None = None

    def to_jsonable(self) -> dict:
        return {
            "layer": self.layer,
            "source": self.source,
            "path": self.path,
            "status": self.status,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }


@dataclass
class DoctorReport:
    """Full preflight result."""

    findings: list[Finding]
    canary_status: str
    canary_detail: str
    sandbox_disabled: bool = False

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.status != STATUS_OK]

    @property
    def ok(self) -> bool:
        """Whether this settings file should be allowed to gate a launch.

        A file that explicitly sets ``sandbox.enabled: false`` never
        launches a sandbox of its own, so its deny paths cannot abort one
        and the gate passes. The findings are still reported: the deny
        arrays merge across settings scopes, so a symlinked path here
        becomes live the moment any other scope enables the sandbox.
        """
        if self.sandbox_disabled:
            return True
        return not self.failures and self.canary_status != CANARY_FAIL

    def to_jsonable(self) -> dict:
        return {
            "ok": self.ok,
            "sandbox_disabled": self.sandbox_disabled,
            "findings": [f.to_jsonable() for f in self.findings],
            "canary": {
                "status": self.canary_status,
                "detail": self.canary_detail,
            },
        }


def validate_settings(settings: Any) -> str | None:
    """Return an error message when the settings shape cannot be checked.

    Only the containers this module reads are validated, but they are
    validated strictly: a ``deny`` given as a bare string is iterable, so
    without this the scan would walk it character by character, find no
    targets, and hand out a clean bill of health for a malformed file.
    A preflight that gates a launch must not pass by accident.
    """
    if not isinstance(settings, dict):
        return "settings root must be a JSON object"

    permissions = settings.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, dict):
            return "permissions must be an object"
        deny = permissions.get("deny")
        if deny is not None and not isinstance(deny, list):
            return "permissions.deny must be an array"

    sandbox = settings.get("sandbox")
    if sandbox is not None:
        if not isinstance(sandbox, dict):
            return "sandbox must be an object"
        fs = sandbox.get("filesystem")
        if fs is not None:
            if not isinstance(fs, dict):
                return "sandbox.filesystem must be an object"
            for key in ("denyRead", "denyWrite"):
                entries = fs.get(key)
                if entries is not None and not isinstance(entries, list):
                    return f"sandbox.filesystem.{key} must be an array"
    return None


def collect_deny_targets(settings: dict) -> list[DenyTarget]:
    """Collect every deny path ``settings`` contributes to the sandbox.

    Both layers are collected because Claude Code merges them: per
    https://code.claude.com/docs/en/sandboxing, "Paths from both
    sandbox.filesystem settings and permission rules are merged together
    into the final sandbox configuration". Auditing Layer 3 alone is what
    lets a Layer 2 credential mirror silently break the sandbox.

    Only entries that name a *concrete host path* are collected;
    project-relative and unanchored-glob rules are skipped because Claude
    Code does not expand them into host paths for the deny set.
    """
    targets: list[DenyTarget] = []

    permissions = settings.get("permissions")
    if isinstance(permissions, dict):
        for rule in permissions.get("deny") or []:
            if not isinstance(rule, str):
                targets.append(
                    DenyTarget(layer="permissions.deny", source=rule, path="")
                )
                continue
            parsed = _split_permission_rule(rule)
            if parsed is None:
                continue
            tool, spec = parsed
            if tool not in _PERMISSION_PATH_TOOLS:
                continue
            host_path = _permission_rule_host_path(spec)
            if host_path is None:
                continue
            targets.append(
                DenyTarget(layer="permissions.deny", source=rule, path=host_path)
            )

    sandbox = settings.get("sandbox")
    if isinstance(sandbox, dict):
        fs = sandbox.get("filesystem")
        if isinstance(fs, dict):
            for key in ("denyRead", "denyWrite"):
                for entry in fs.get(key) or []:
                    if not isinstance(entry, str):
                        # The renderer emits kept entries as strings; a
                        # structured dict surviving into a rendered file
                        # means the entry was malformed, so surface it
                        # rather than skipping to a clean result.
                        targets.append(
                            DenyTarget(
                                layer=f"sandbox.filesystem.{key}",
                                source=entry,
                                path="",
                            )
                        )
                        continue
                    path = entry
                    if path.startswith("~/"):
                        path = os.path.expanduser("~") + path[1:]
                    if not path.startswith("/"):
                        continue
                    targets.append(
                        DenyTarget(
                            layer=f"sandbox.filesystem.{key}",
                            source=entry,
                            path=path,
                        )
                    )

    return targets


def analyze_targets(targets: list[DenyTarget]) -> list[Finding]:
    """Statically classify each deny target as bwrap-safe or not."""
    findings: list[Finding] = []
    for target in targets:
        if not target.path:
            findings.append(
                Finding(
                    layer=target.layer,
                    source=target.source,
                    path="",
                    status=STATUS_UNSUPPORTED,
                    detail=(
                        "entry is not a string, so its bwrap usability cannot "
                        "be verified; a rendered settings file should contain "
                        "only string deny entries"
                    ),
                )
            )
            continue
        literal = _literal_path_prefix(target.path)
        if literal is None:
            findings.append(
                Finding(
                    layer=target.layer,
                    source=target.source,
                    path=target.path,
                    status=STATUS_OK,
                    detail="no anchored literal prefix; not expanded to a host path",
                )
            )
            continue
        link = _absolute_symlink_in_chain(literal)
        if link is None:
            findings.append(
                Finding(
                    layer=target.layer,
                    source=target.source,
                    path=target.path,
                    status=STATUS_OK,
                    detail="no absolute symlink in the path chain",
                )
            )
            continue
        resolved = os.path.realpath(literal)
        rewritten = resolved + target.path[len(literal) :]
        findings.append(
            Finding(
                layer=target.layer,
                source=target.source,
                path=target.path,
                status=STATUS_SYMLINK_ESCAPE,
                detail=(
                    f"absolute symlink at {link} -> {os.path.realpath(link)}; "
                    "bwrap cannot create a mount point under it and will abort "
                    "the sandbox launch"
                ),
                suggestion=rewritten,
            )
        )
    return findings


def _bwrap_source_for(path: str) -> str | None:
    """Pick a bind source matching the deny target's type."""
    real = os.path.realpath(path)
    if not os.path.exists(real):
        return None
    return None if os.path.isdir(real) else "/dev/null"


def run_bwrap_canary(
    targets: list[DenyTarget],
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    bwrap_path: str | None = None,
) -> tuple[str, str]:
    """Launch bwrap with the collected deny paths bound.

    Returns ``(status, detail)``. This is the canary that answers the
    question the static check can only approximate: does the sandbox
    actually come up on *this* machine with *these* settings?
    """
    resolved_bwrap = bwrap_path or shutil.which("bwrap")
    if resolved_bwrap is None:
        return CANARY_SKIPPED, "bwrap not found on PATH; live canary not run"

    argv = [resolved_bwrap, "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev"]
    with tempfile.TemporaryDirectory() as empty_dir:
        bound = 0
        for target in targets:
            literal = _literal_path_prefix(target.path)
            if literal is None or not os.path.lexists(literal):
                continue
            source = _bwrap_source_for(literal)
            argv += ["--ro-bind", source or empty_dir, literal]
            bound += 1
        if bound == 0:
            return CANARY_SKIPPED, "no concrete deny paths to probe"
        argv.append("true")

        run = runner or (
            lambda cmd: subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
        )
        try:
            proc = run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            return CANARY_FAIL, f"could not launch bwrap: {exc}"

    if proc.returncode == 0:
        return CANARY_PASS, f"bwrap started with {bound} deny path(s) bound"
    stderr = (proc.stderr or "").strip().splitlines()
    first = stderr[0] if stderr else f"exit status {proc.returncode}"
    return CANARY_FAIL, first


def diagnose(
    settings: dict,
    *,
    probe_bwrap: bool = True,
    runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> DoctorReport:
    """Run the full preflight against a parsed settings mapping."""
    sandbox = settings.get("sandbox")
    # Only an *explicit* disable downgrades the gate. An absent key is
    # treated as unknown rather than off, because user or managed settings
    # can enable the sandbox for a role that never mentions it.
    sandbox_disabled = (
        isinstance(sandbox, dict) and sandbox.get("enabled") is False
    )
    targets = collect_deny_targets(settings)
    findings = analyze_targets(targets)
    if sandbox_disabled:
        status, detail = (
            CANARY_SKIPPED,
            "sandbox.enabled is false; no sandbox launch to probe",
        )
    elif probe_bwrap:
        status, detail = run_bwrap_canary(targets, runner=runner)
    else:
        status, detail = CANARY_SKIPPED, "live canary disabled (--no-probe-bwrap)"
    return DoctorReport(
        findings=findings,
        canary_status=status,
        canary_detail=detail,
        sandbox_disabled=sandbox_disabled,
    )


def format_report(report: DoctorReport, *, verbose: bool = False) -> str:
    """Human-readable rendering of a :class:`DoctorReport`."""
    lines: list[str] = []
    shown = report.findings if verbose else report.failures
    lines.append(
        f"deny targets: {len(report.findings)} "
        f"({len(report.failures)} unusable by bwrap)"
    )
    for f in shown:
        marker = "ok " if f.status == STATUS_OK else "FAIL"
        lines.append(f"  [{marker}] {f.layer}: {f.source}")
        lines.append(f"         path: {f.path}")
        lines.append(f"         {f.detail}")
        if f.suggestion:
            lines.append(f"         suggested rewrite: {f.suggestion}")
    lines.append(f"bwrap canary: {report.canary_status} - {report.canary_detail}")
    if report.sandbox_disabled:
        lines.append("")
        lines.append(
            "RESULT: sandbox.enabled is false in these settings, so no sandbox "
            "launch can be aborted here and the check passes. Any finding "
            "above is still latent: deny arrays merge across settings scopes, "
            "so it becomes live as soon as another scope enables the sandbox."
        )
    elif not report.ok:
        lines.append("")
        lines.append(
            "RESULT: the sandbox will NOT start with these settings. Claude "
            "Code falls back to running Bash commands unsandboxed, so this "
            "fails silently unless it is checked. Re-render the worker "
            "settings with a runtime that canonicalizes symlinked deny paths, "
            "or set sandbox.allowUnsandboxedCommands=false to make the "
            "fallback a hard error."
        )
    else:
        lines.append("RESULT: sandbox deny paths are usable by bwrap.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach ``sandbox doctor`` flags to an existing parser."""
    parser.add_argument(
        "--settings",
        type=Path,
        required=True,
        help="path to the settings.local.json to check",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the human-readable report",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="list every deny target, not just the failing ones",
    )
    parser.add_argument(
        "--no-probe-bwrap",
        dest="probe_bwrap",
        action="store_false",
        default=True,
        help=(
            "skip the live bwrap canary and run only the static path "
            "analysis (useful where bwrap is unavailable or in CI)"
        ),
    )


def run(args: argparse.Namespace) -> int:
    try:
        with Path(args.settings).open(encoding="utf-8") as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        print(f"error: settings not found: {args.settings}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: settings is not valid JSON: {exc}", file=sys.stderr)
        return 2
    invalid = validate_settings(settings)
    if invalid is not None:
        print(f"error: {invalid}", file=sys.stderr)
        return 2

    report = diagnose(settings, probe_bwrap=getattr(args, "probe_bwrap", True))
    if getattr(args, "json", False):
        sys.stdout.write(
            json.dumps(report.to_jsonable(), indent=2, ensure_ascii=False) + "\n"
        )
    else:
        sys.stdout.write(
            format_report(report, verbose=getattr(args, "verbose", False))
        )
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-org-runtime-sandbox-doctor",
        description=(
            "Check that a worker's sandbox deny paths can actually be "
            "mounted by bubblewrap, so a failed sandbox launch cannot go "
            "unnoticed."
        ),
    )
    add_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
