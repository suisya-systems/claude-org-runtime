"""Schema-driven worker ``.claude/settings.local.json`` generator.

Port of claude-org-ja ``tools/generate_worker_settings.py``. The schema
SoT (``role_configs_schema.json``) now ships inside this package, so
consumers no longer need to keep their copy under ``tools/`` in sync;
they install ``claude-org-runtime`` and invoke this module.

CLI parity with the in-tree script is preserved -- the ``--role`` /
``--worker-dir`` / ``--claude-org-path`` / ``--out`` / ``--schema``
arguments behave identically. ``--schema`` defaults to the bundled
schema instead of ``<repo>/tools/role_configs_schema.json``.

Phase 3 case E (Issue #392) adds an optional ``sandbox`` object on
``worker_roles.<role>`` plus Layer 3 suppression. See
``role_configs_schema.json`` ``worker_roles.$comment_sandbox`` for the
shape; rendered output and suppression metadata are surfaced via
``claude-org-runtime settings show --explain``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

# Keys under worker_roles[<role>] that are metadata, not part of the emitted
# settings.local.json content.
_META_KEYS = {"description", "$comment"}

# WSL kernel marker as exposed by /proc/version + /proc/sys/kernel/osrelease.
_WSL_MARKER = "microsoft-standard-WSL"
_DEFAULT_WSL_PROBE_PATHS: tuple[str, ...] = (
    "/proc/version",
    "/proc/sys/kernel/osrelease",
)


def _bundled_schema_path() -> Path:
    """Path to the schema bundled with the package."""
    resource = files("claude_org_runtime.settings").joinpath(
        "role_configs_schema.json"
    )
    # ``files()`` returns a ``MultiplexedPath``-compatible object; for the
    # common installed layout this is a real filesystem path.
    return Path(str(resource))


def load_schema(path: Path | None = None) -> dict:
    """Load the role-configs schema. ``None`` -> bundled SoT."""
    target = path if path is not None else _bundled_schema_path()
    with Path(target).open(encoding="utf-8") as fh:
        return json.load(fh)


def _substitute(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for placeholder, replacement in mapping.items():
            out = out.replace("{" + placeholder + "}", replacement)
        return out
    if isinstance(value, list):
        return [_substitute(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, mapping) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Phase 3 case E: sandbox + Layer 3 suppression
# ---------------------------------------------------------------------------


def _detect_wsl(probe_paths: tuple[str, ...] = _DEFAULT_WSL_PROBE_PATHS) -> bool:
    """Annotation-only WSL detection.

    Reads ``/proc/version`` and ``/proc/sys/kernel/osrelease`` and looks
    for the standard WSL kernel marker. The result is recorded in
    suppression metadata for ``settings show --explain`` but does NOT
    gate the suppression decision -- escape is judged from realpath.
    """
    for path in probe_paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                if _WSL_MARKER in fh.read():
                    return True
        except OSError:
            continue
    return False


def _literal_path_prefix(pattern: str) -> str | None:
    """Return the leading non-glob path prefix of ``pattern``.

    For example, ``/etc/passwd`` -> ``/etc/passwd``; ``/etc/**`` ->
    ``/etc``; ``foo/bar`` -> ``foo/bar``; ``**/credentials*`` -> None
    (pattern's first segment is itself a glob, so there is no anchored
    prefix that ``realpath`` could meaningfully resolve). Patterns of
    the form ``/*…`` (absolute but the first non-empty segment is a
    glob) also return None.
    """
    glob_chars = ("*", "?", "[")
    parts = pattern.split("/")
    if not parts:
        return None
    if any(c in parts[0] for c in glob_chars):
        return None
    out: list[str] = []
    for part in parts:
        if any(c in part for c in glob_chars):
            break
        out.append(part)
    if not out:
        return None
    result = "/".join(out)
    if not result:
        # Pattern was "/<glob>..."; no usable anchored prefix.
        return None
    return result


def _normalize_root(root: str) -> str:
    """Normalize a sandbox read root path for prefix comparisons."""
    return os.path.normpath(root).rstrip("/") or "/"


def _is_inside_root(target: str, roots: list[str]) -> bool:
    """True if ``target`` (already realpath'd) is inside any of ``roots``.

    The roots are compared *without* an additional realpath pass: WSL /
    devcontainer suppression hinges on the realpath'd target landing
    outside the user-specified read roots (e.g. ``/mnt/c/...`` outside
    of ``/home/<user>/work/wd``). If the roots were realpath'd too, the
    symlink would be resolved on both sides and the escape would
    silently disappear.
    """
    target_norm = os.path.normpath(target)
    for r in roots:
        if not r:
            continue
        normalized = _normalize_root(r)
        if target_norm == normalized:
            return True
        sep = "/" if normalized == "/" else normalized + "/"
        if target_norm.startswith(sep):
            return True
    return False


@dataclass(frozen=True)
class SandboxSuppression:
    """One ``sandbox.filesystem`` entry that was dropped from Layer 3."""

    layer: str  # e.g. "sandbox.filesystem.denyRead"
    entry: str
    reason: str
    realpath: str
    sandbox_read_roots: tuple[str, ...]


@dataclass
class SandboxMetadata:
    """Suppression report exposed via ``settings show --explain``."""

    enabled: bool = False
    wsl_detected: bool = False
    sandbox_read_roots: tuple[str, ...] = ()
    suppressions: list[SandboxSuppression] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        return {
            "enabled": self.enabled,
            "wsl_detected": self.wsl_detected,
            "sandbox_read_roots": list(self.sandbox_read_roots),
            "suppressions": [
                {
                    "layer": s.layer,
                    "entry": s.entry,
                    "reason": s.reason,
                    "realpath": s.realpath,
                    "sandbox_read_roots": list(s.sandbox_read_roots),
                }
                for s in self.suppressions
            ],
        }


@dataclass
class RenderResult:
    """Bundle of rendered settings + sandbox suppression metadata."""

    settings: dict
    sandbox: SandboxMetadata


def _evaluate_sandbox_suppressions(
    sandbox: dict,
    worker_dir: str,
    *,
    realpath_fn: Callable[[str], str] = os.path.realpath,
    wsl_detector: Callable[[], bool] = _detect_wsl,
) -> tuple[dict, SandboxMetadata]:
    """Apply realpath-escape suppression to ``sandbox.filesystem.deny{Read,Write}``.

    A deny entry is suppressed when its realpath resolves outside the
    sandbox's read roots (``worker_dir`` + ``filesystem.additionalDirectories``)
    -- on WSL this typically happens when the worker_dir is a symlink
    that resolves into ``/mnt/c``, and the sandbox bind-mount tree does
    not include ``/mnt/c``. Layer 2 ``permissions.deny`` is untouched.
    """
    metadata = SandboxMetadata(wsl_detected=wsl_detector())
    if not isinstance(sandbox, dict) or not sandbox.get("enabled"):
        return sandbox, metadata
    metadata.enabled = True
    fs = sandbox.get("filesystem") or {}
    if not isinstance(fs, dict):
        fs = {}
    additional = list(fs.get("additionalDirectories") or [])
    read_roots_raw = [worker_dir, *additional]
    read_roots = [_normalize_root(r) for r in read_roots_raw if r]
    metadata.sandbox_read_roots = tuple(read_roots)

    # If worker_dir's realpath escapes the sandbox read roots, every
    # entry that is anchored at worker_dir (relative literal or
    # relative pure-glob) is unreachable in the sandbox view and must
    # also be suppressed -- not just the literal-prefix cases. This is
    # the WSL case where /home/<u>/work/wd resolves into /mnt/c/... .
    worker_dir_rp = realpath_fn(worker_dir)
    worker_dir_reachable = _is_inside_root(worker_dir_rp, read_roots)

    new_fs: dict = {**fs}
    for layer_key in ("denyRead", "denyWrite"):
        entries = list(fs.get(layer_key) or [])
        kept: list[str] = []
        for entry in entries:
            if not isinstance(entry, str):
                kept.append(entry)
                continue
            literal = _literal_path_prefix(entry)
            absolute_pattern = entry.startswith("/")
            if literal is None and not absolute_pattern:
                # Pure-glob, relative -> anchored at worker_dir.
                if worker_dir_reachable:
                    kept.append(entry)
                else:
                    metadata.suppressions.append(
                        SandboxSuppression(
                            layer=f"sandbox.filesystem.{layer_key}",
                            entry=entry,
                            reason=(
                                "worker_dir realpath escapes sandbox read "
                                "roots (anchored relative pattern)"
                            ),
                            realpath=worker_dir_rp,
                            sandbox_read_roots=tuple(read_roots),
                        )
                    )
                continue
            if literal is None:
                # Absolute pure-glob (e.g. ``/*``) -- without fnmatch'ing
                # the actual filesystem we can't compute reachability,
                # so keep the entry as-is.
                kept.append(entry)
                continue
            target = (
                literal
                if os.path.isabs(literal)
                else os.path.join(worker_dir, literal)
            )
            target_rp = realpath_fn(target)
            if _is_inside_root(target_rp, read_roots):
                kept.append(entry)
            else:
                metadata.suppressions.append(
                    SandboxSuppression(
                        layer=f"sandbox.filesystem.{layer_key}",
                        entry=entry,
                        reason="realpath escapes sandbox read roots",
                        realpath=target_rp,
                        sandbox_read_roots=tuple(read_roots),
                    )
                )
        new_fs[layer_key] = kept

    new_sandbox = {**sandbox, "filesystem": new_fs}
    return new_sandbox, metadata


def render_role_with_metadata(
    schema: dict,
    role: str,
    worker_dir: str,
    claude_org_path: str,
    *,
    realpath_fn: Callable[[str], str] = os.path.realpath,
    wsl_detector: Callable[[], bool] = _detect_wsl,
) -> RenderResult:
    """Render the per-role ``settings.local.json`` plus suppression metadata.

    Same substitution rules as :func:`render_role`. When the role
    declares an enabled ``sandbox`` object, Layer 3 suppression is
    applied (see :func:`_evaluate_sandbox_suppressions`); the rendered
    sandbox object reflects the suppression while
    ``permissions.deny`` is preserved untouched.
    """
    roles = schema.get("worker_roles") or {}
    available = sorted(
        k
        for k, v in roles.items()
        if not k.startswith("$") and isinstance(v, dict)
    )
    if (
        role not in roles
        or role.startswith("$")
        or not isinstance(roles[role], dict)
    ):
        raise KeyError(
            f"unknown worker role: {role!r}. available: {available}"
        )
    template = {
        k: v for k, v in roles[role].items() if k not in _META_KEYS
    }
    rendered = _substitute(
        template,
        {"worker_dir": worker_dir, "claude_org_path": claude_org_path},
    )
    sandbox = rendered.get("sandbox")
    if isinstance(sandbox, dict):
        new_sandbox, metadata = _evaluate_sandbox_suppressions(
            sandbox,
            worker_dir,
            realpath_fn=realpath_fn,
            wsl_detector=wsl_detector,
        )
        rendered["sandbox"] = new_sandbox
    else:
        metadata = SandboxMetadata(wsl_detected=wsl_detector())
    return RenderResult(settings=rendered, sandbox=metadata)


def render_role(
    schema: dict,
    role: str,
    worker_dir: str,
    claude_org_path: str,
) -> dict:
    """Render the per-role ``settings.local.json`` content.

    Substitutes ``{worker_dir}`` and ``{claude_org_path}`` in the role's
    template, drops ``description`` / ``$comment`` metadata keys, and
    applies Phase 3 case E sandbox suppression when applicable. For the
    suppression metadata use :func:`render_role_with_metadata`.
    """
    return render_role_with_metadata(
        schema,
        role=role,
        worker_dir=worker_dir,
        claude_org_path=claude_org_path,
    ).settings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-org-runtime-settings",
        description=(
            "Generate <worker_dir>/.claude/settings.local.json from "
            "role_configs_schema.json -> worker_roles[<role>]."
        ),
    )
    add_arguments(parser)
    return parser


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the generator's flags to an existing parser.

    Used by both the standalone module CLI and the unified
    ``claude-org-runtime`` entry point.
    """
    parser.add_argument(
        "--role",
        required=True,
        help="worker role name (e.g. default, claude-org-self-edit, doc-audit)",
    )
    parser.add_argument(
        "--worker-dir",
        required=True,
        help="absolute path that {worker_dir} resolves to",
    )
    parser.add_argument(
        "--claude-org-path",
        required=True,
        help="absolute path to the claude-org repo (for hook script paths)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output file (default: stdout)",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=None,
        help="schema path override (default: bundled role_configs_schema.json)",
    )


def add_show_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the ``settings show --explain`` flags."""
    add_arguments(parser)
    parser.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Include sandbox suppression metadata (Phase 3 case E) in the "
            "output. Without --explain only the rendered settings are shown."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-readable text.",
    )


def run(args: argparse.Namespace) -> int:
    try:
        schema = load_schema(args.schema)
    except FileNotFoundError as exc:
        print(f"error: schema not found: {exc.filename}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: schema is not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        rendered = render_role(
            schema,
            role=args.role,
            worker_dir=args.worker_dir,
            claude_org_path=args.claude_org_path,
        )
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2

    text = json.dumps(rendered, indent=2, ensure_ascii=False) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


def run_show(args: argparse.Namespace) -> int:
    try:
        schema = load_schema(args.schema)
    except FileNotFoundError as exc:
        print(f"error: schema not found: {exc.filename}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: schema is not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        result = render_role_with_metadata(
            schema,
            role=args.role,
            worker_dir=args.worker_dir,
            claude_org_path=args.claude_org_path,
        )
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2

    explain = bool(getattr(args, "explain", False))
    as_json = bool(getattr(args, "json", False))
    text = _format_show_output(result, args.role, explain=explain, as_json=as_json)
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


def _format_show_output(
    result: RenderResult, role: str, *, explain: bool, as_json: bool
) -> str:
    """Render ``settings show`` output.

    Both the JSON and the human-readable text variants project from
    the same :class:`RenderResult` so the final deny set + suppression
    reasons come from a single source of truth.
    """
    if as_json:
        payload: dict[str, Any] = {
            "role": role,
            "settings": result.settings,
        }
        if explain:
            payload["sandbox"] = result.sandbox.to_jsonable()
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    lines: list[str] = [f"role: {role}"]
    permissions = result.settings.get("permissions") or {}
    deny = list(permissions.get("deny") or [])
    lines.append(f"permissions.deny ({len(deny)}):")
    for d in deny:
        lines.append(f"  - {d}")

    sandbox = result.settings.get("sandbox")
    if isinstance(sandbox, dict):
        lines.append(f"sandbox.enabled: {bool(sandbox.get('enabled'))}")
        if sandbox.get("enabled"):
            fs = sandbox.get("filesystem") or {}
            for key in ("denyRead", "denyWrite", "additionalDirectories"):
                entries = list(fs.get(key) or [])
                lines.append(f"sandbox.filesystem.{key} ({len(entries)}):")
                for e in entries:
                    lines.append(f"  - {e}")
            lines.append(
                f"sandbox.failIfUnavailable: "
                f"{bool(sandbox.get('failIfUnavailable'))}"
            )
    else:
        lines.append("sandbox.enabled: false")

    if explain:
        lines.append(f"wsl_detected: {result.sandbox.wsl_detected}")
        lines.append(
            f"sandbox_read_roots ({len(result.sandbox.sandbox_read_roots)}):"
        )
        for r in result.sandbox.sandbox_read_roots:
            lines.append(f"  - {r}")
        if result.sandbox.suppressions:
            lines.append(
                f"suppressions ({len(result.sandbox.suppressions)}):"
            )
            for s in result.sandbox.suppressions:
                lines.append(
                    f"  - {s.layer} entry={s.entry!r} "
                    f"reason={s.reason!r} realpath={s.realpath}"
                )
        else:
            lines.append("suppressions: (none)")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
