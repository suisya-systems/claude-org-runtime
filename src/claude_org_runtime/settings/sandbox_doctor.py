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


# Settings scopes Claude Code merges besides the file under test. The deny
# arrays are unioned across every scope, so a symlinked path in any one of
# them aborts the launch no matter how clean the rendered worker file is.
# Checking the worker file alone would hand out a clean bill of health for
# a sandbox that cannot start.
USER_SETTINGS_PATH = "~/.claude/settings.json"
MANAGED_SETTINGS_PATHS = (
    "/etc/claude-code/managed-settings.json",
    "/Library/Application Support/ClaudeCode/managed-settings.json",
)
# Project-level scopes live side by side: ``settings.json`` is the checked-in
# one and ``settings.local.json`` the generated / personal one. Claude Code
# unions both, so checking whichever was passed and ignoring its sibling
# would leave half the project scope unaudited.
PROJECT_SCOPE_FILENAMES = ("settings.json", "settings.local.json")


@dataclass(frozen=True)
class SettingsSource:
    """One settings file participating in the merge."""

    label: str
    settings: dict


@dataclass(frozen=True)
class DenyTarget:
    """One concrete host path the settings contribute to the deny set."""

    layer: str  # "permissions.deny" | "sandbox.filesystem.denyRead" | ...
    source: Any  # the original rule / entry as authored
    path: str  # absolute host path (glob tail included)
    source_file: str = ""  # which settings scope contributed it


@dataclass(frozen=True)
class Finding:
    """Verdict for a single deny target."""

    layer: str
    source: Any
    path: str
    status: str
    detail: str
    suggestion: str | None = None
    source_file: str = ""

    def to_jsonable(self) -> dict:
        return {
            "layer": self.layer,
            "source": self.source,
            "path": self.path,
            "status": self.status,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "source_file": self.source_file,
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


def collect_deny_targets(
    settings: dict, *, source_file: str = ""
) -> list[DenyTarget]:
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
                    DenyTarget(
                        layer="permissions.deny",
                        source=rule,
                        path="",
                        source_file=source_file,
                    )
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
                DenyTarget(
                    layer="permissions.deny",
                    source=rule,
                    path=host_path,
                    source_file=source_file,
                )
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
                                source_file=source_file,
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
                            source_file=source_file,
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
                    source_file=target.source_file,
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
                    source_file=target.source_file,
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
                    source_file=target.source_file,
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
                source_file=target.source_file,
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
    # An injected ``runner`` stands in for the real binary, so requiring
    # bwrap on PATH would make the caller's substitution depend on the
    # host having the tool it is substituting for.
    if resolved_bwrap is None and runner is None:
        return CANARY_SKIPPED, "bwrap not found on PATH; live canary not run"
    resolved_bwrap = resolved_bwrap or "bwrap"

    # Deliberately no ``--proc`` / ``--dev``: those mount fresh filesystems
    # that *shadow* the corresponding host trees, and a shadowed region has
    # no symlink for bwrap to trip over -- it simply creates plain
    # directories and succeeds. Probing with them would blind the canary to
    # any deny path under the shadowed prefix and make it disagree with the
    # static analysis. The probe only needs to create the mount points, and
    # ``true`` needs neither /proc nor /dev.
    argv = [resolved_bwrap, "--ro-bind", "/", "/"]
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
    """Run the full preflight against a single parsed settings mapping."""
    return diagnose_sources(
        [SettingsSource(label="", settings=settings)],
        probe_bwrap=probe_bwrap,
        runner=runner,
    )


def diagnose_sources(
    sources: list[SettingsSource],
    *,
    probe_bwrap: bool = True,
    runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> DoctorReport:
    """Run the preflight against the *merged* deny set of every scope.

    Claude Code unions the deny arrays across settings scopes, so a
    symlinked path contributed by ``~/.claude/settings.json`` or by
    managed settings aborts the launch no matter how clean the rendered
    worker file is. Checking one file in isolation would report a clean
    preflight for a sandbox that cannot start -- exactly the silent
    failure this command exists to catch.

    ``sandbox.enabled`` is resolved conservatively: the gate is only
    relaxed when no scope enables the sandbox and at least one explicitly
    disables it. Any scope turning it on means a launch can be aborted.
    """
    enabled_anywhere = False
    disabled_anywhere = False
    for source in sources:
        sandbox = source.settings.get("sandbox")
        if not isinstance(sandbox, dict):
            continue
        if sandbox.get("enabled") is True:
            enabled_anywhere = True
        elif sandbox.get("enabled") is False:
            disabled_anywhere = True
    sandbox_disabled = disabled_anywhere and not enabled_anywhere

    targets: list[DenyTarget] = []
    for source in sources:
        targets.extend(
            collect_deny_targets(source.settings, source_file=source.label)
        )
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


def load_source(path: Path) -> tuple[SettingsSource | None, str | None]:
    """Load and shape-validate one settings file.

    Returns ``(source, error)``; exactly one is non-``None``.
    """
    try:
        with Path(path).open(encoding="utf-8") as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        return None, f"settings not found: {path}"
    except OSError as exc:
        return None, f"could not read {path}: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"{path} is not valid JSON: {exc}"
    invalid = validate_settings(settings)
    if invalid is not None:
        return None, f"{path}: {invalid}"
    return SettingsSource(label=str(path), settings=settings), None


def discover_merged_scopes(inputs: list[Path] | None = None) -> list[Path]:
    """Settings scopes that merge into the effective deny set, if present.

    Two families are discovered:

    - **Sibling project scopes.** ``.claude/settings.json`` and
      ``.claude/settings.local.json`` are separate scopes that Claude
      Code unions, so pointing ``--settings`` at one must not leave the
      other unchecked. They are derived from each input's directory
      rather than from a fixed list, so the project scope is picked up
      wherever the file happens to live.
    - **Global scopes.** The user settings and any managed settings.

    Only files that exist are returned, so a machine without managed
    settings simply contributes fewer scopes rather than erroring.
    Discovery never returns a path already present in ``inputs``.
    """
    given = [Path(p) for p in (inputs or [])]
    seen = {p.resolve() for p in given if p.exists()}
    found: list[Path] = []

    def add(candidate: Path) -> None:
        if not candidate.is_file():
            return
        resolved = candidate.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        found.append(candidate)

    for path in given:
        for name in PROJECT_SCOPE_FILENAMES:
            add(path.parent / name)
    add(Path(os.path.expanduser(USER_SETTINGS_PATH)))
    for candidate in MANAGED_SETTINGS_PATHS:
        add(Path(candidate))
    return found


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
        if f.source_file:
            lines.append(f"         from: {f.source_file}")
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
    elif report.failures and report.canary_status == CANARY_PASS:
        lines.append("")
        lines.append(
            "RESULT: bwrap started here, but the deny paths above cross an "
            "absolute symlink and are only bindable while some mount hides "
            "the link from the sandbox. That is not a property to depend on: "
            "the same settings abort the launch as soon as the link is "
            "visible. Treated as a failure; apply the suggested rewrites."
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
        action="append",
        required=True,
        dest="settings",
        metavar="PATH",
        help=(
            "settings file to check; repeat to add more scopes. Their deny "
            "sets are merged the way Claude Code merges them."
        ),
    )
    parser.add_argument(
        "--no-merge-scopes",
        dest="merge_scopes",
        action="store_false",
        default=True,
        help=(
            "check only the files given with --settings. By default the "
            "user settings (~/.claude/settings.json) and managed settings "
            "are merged in too, because a deny path in any scope aborts "
            "the sandbox launch."
        ),
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
    requested = args.settings
    if not isinstance(requested, list):
        requested = [requested]

    paths = [Path(p) for p in requested]
    if getattr(args, "merge_scopes", True):
        paths.extend(discover_merged_scopes(paths))

    sources: list[SettingsSource] = []
    for path in paths:
        source, error = load_source(path)
        if error is not None:
            print(f"error: {error}", file=sys.stderr)
            return 2
        assert source is not None
        sources.append(source)

    report = diagnose_sources(
        sources, probe_bwrap=getattr(args, "probe_bwrap", True)
    )
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
