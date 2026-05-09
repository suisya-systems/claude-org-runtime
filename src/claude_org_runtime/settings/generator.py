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

Phase 1 (Refs `claude-org-ja#378`) extends the renderer with:

- ``role_kind='org'|'worker'`` so the same ``render_role_with_metadata``
  call site can render org roles (``schema['roles'][...]``) in
  addition to worker roles (``schema['worker_roles'][...]``).
- A structured anchor entry shape on ``sandbox.filesystem.deny{Read,Write}``
  (``{anchor, path, suppressOnSymlinkEscape}``) plus a backward-compat
  legacy adapter for raw strings; see
  ``role_configs_schema.json`` ``worker_roles.$comment_sandbox_anchor``.
- A Pattern B context (``base_clone`` / ``task_id`` / ``branch_ref``)
  whose placeholders are substituted alongside ``{worker_dir}`` /
  ``{claude_org_path}`` in entry paths and ``additionalDirectories``.
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

    The boundary separator is composed from ``os.sep`` so the prefix
    check works on both POSIX (``/``) and Windows (``\\``); ``normpath``
    has already normalized either input to native separators.
    """
    target_norm = os.path.normpath(target)
    for r in roots:
        if not r:
            continue
        normalized = _normalize_root(r)
        if target_norm == normalized:
            return True
        if normalized.endswith(("/", os.sep)):
            sep = normalized
        else:
            sep = normalized + os.sep
        if target_norm.startswith(sep):
            return True
    return False


_VALID_ANCHORS = ("home", "worker_dir", "claude_org_path", "absolute")


@dataclass(frozen=True)
class GeneratorContext:
    """Context passed to the renderer / suppression evaluator.

    ``worker_dir`` and ``claude_org_path`` keep the legacy substitution
    semantics. The Phase 1 (Refs `claude-org-ja#378`) additions
    ``base_clone`` / ``task_id`` / ``branch_ref`` are optional Pattern B
    context placeholders -- when set, ``{base_clone}`` etc. are
    substituted in entry paths and ``additionalDirectories`` alongside
    the legacy placeholders. ``pattern`` is informational metadata for
    consumers that want to branch on the dispatch pattern; the renderer
    itself does not gate behavior on it.
    """

    worker_dir: str
    claude_org_path: str
    base_clone: str | None = None
    task_id: str | None = None
    branch_ref: str | None = None
    pattern: str | None = None  # "A" | "B" | None


def _build_substitution_mapping(ctx: GeneratorContext) -> dict[str, str]:
    """Substitution mapping fed to :func:`_substitute`.

    Optional Pattern B placeholders are only added to the mapping when
    set. Unknown placeholders therefore pass through untouched, which
    keeps backward compatibility with templates that never reference
    Pattern B context.
    """
    mapping: dict[str, str] = {
        "worker_dir": ctx.worker_dir,
        "claude_org_path": ctx.claude_org_path,
    }
    if ctx.base_clone is not None:
        mapping["base_clone"] = ctx.base_clone
    if ctx.task_id is not None:
        mapping["task_id"] = ctx.task_id
    if ctx.branch_ref is not None:
        mapping["branch_ref"] = ctx.branch_ref
    return mapping


def _anchor_base_path(anchor: str, ctx: GeneratorContext) -> str:
    """Resolve an anchor name to its absolute base path.

    ``home`` expands to the current user's home directory (via
    ``os.path.expanduser('~')`` so the value is consistent with the
    process's resolved ``HOME``). ``worker_dir`` / ``claude_org_path``
    pull from the generator context. ``absolute`` returns ``""`` so the
    caller treats the entry path itself as fully-qualified.
    """
    if anchor == "home":
        return os.path.expanduser("~")
    if anchor == "worker_dir":
        return ctx.worker_dir
    if anchor == "claude_org_path":
        return ctx.claude_org_path
    if anchor == "absolute":
        return ""
    raise ValueError(
        f"unknown sandbox entry anchor: {anchor!r}. "
        f"valid: {list(_VALID_ANCHORS)}"
    )


@dataclass(frozen=True)
class _NormalizedSandboxEntry:
    """Internal normalized form of a sandbox.filesystem deny entry.

    The legacy raw-string form and the new structured form converge
    here so the suppression evaluator has a single shape to reason
    about. ``raw`` preserves the operator's original entry value so it
    can be surfaced back in the rendered output and suppression report
    untouched.
    """

    anchor: str
    path: str
    suppress_on_symlink_escape: bool
    raw: Any


def _normalize_sandbox_entry(entry: Any) -> _NormalizedSandboxEntry | None:
    """Convert a raw-string or structured deny entry into the unified form.

    Legacy strings keep their historical anchoring: absolute paths are
    treated as ``anchor='absolute'``, everything else is anchored at
    ``worker_dir``. ``suppressOnSymlinkEscape`` defaults to ``True`` to
    match the prior unconditional suppression behavior.

    Returns ``None`` when the entry shape is unrecognized so the caller
    can pass it through to the rendered output untouched (the launcher
    will surface any malformed entries directly).
    """
    if isinstance(entry, str):
        if entry.startswith("/"):
            return _NormalizedSandboxEntry(
                anchor="absolute",
                path=entry,
                suppress_on_symlink_escape=True,
                raw=entry,
            )
        return _NormalizedSandboxEntry(
            anchor="worker_dir",
            path=entry,
            suppress_on_symlink_escape=True,
            raw=entry,
        )
    if isinstance(entry, dict):
        anchor = entry.get("anchor", "worker_dir")
        if anchor not in _VALID_ANCHORS:
            return None
        path = entry.get("path")
        if not isinstance(path, str):
            return None
        suppress = entry.get("suppressOnSymlinkEscape", True)
        # Strict bool check: ``bool('false') == True`` would silently
        # flip the operator's intent, so non-bool values cause the
        # entry to pass through to the rendered output untouched
        # (and the launcher / drift CI surfaces the malformed entry).
        if not isinstance(suppress, bool):
            return None
        return _NormalizedSandboxEntry(
            anchor=anchor,
            path=path,
            suppress_on_symlink_escape=suppress,
            raw=entry,
        )
    return None


@dataclass(frozen=True)
class SandboxSuppression:
    """One ``sandbox.filesystem`` entry that was dropped from Layer 3."""

    layer: str  # e.g. "sandbox.filesystem.denyRead"
    entry: Any  # original raw-string or structured-dict entry
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
    ctx: GeneratorContext,
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

    Phase 1 (Refs `claude-org-ja#378`) extends this with a structured
    anchor field: a deny entry can declare ``anchor='home'`` (resolves
    against ``/home/<current-user>``), ``'absolute'``, ``'worker_dir'``,
    or ``'claude_org_path'``. Operators may also opt out of escape
    suppression on a per-entry basis with
    ``suppressOnSymlinkEscape: false``. Pattern B placeholders
    (``{base_clone}`` etc.) are substituted before realpath evaluation
    when supplied via the generator context.
    """
    metadata = SandboxMetadata(wsl_detected=wsl_detector())
    if not isinstance(sandbox, dict) or not sandbox.get("enabled"):
        return sandbox, metadata
    metadata.enabled = True
    fs = sandbox.get("filesystem") or {}
    if not isinstance(fs, dict):
        fs = {}
    mapping = _build_substitution_mapping(ctx)
    additional_raw = list(fs.get("additionalDirectories") or [])
    additional = [_substitute(a, mapping) for a in additional_raw]
    read_roots_raw = [ctx.worker_dir, *additional]
    read_roots = [_normalize_root(r) for r in read_roots_raw if r]
    metadata.sandbox_read_roots = tuple(read_roots)

    new_fs: dict = {**fs}
    # Only emit additionalDirectories when the original sandbox had
    # the key -- the documented contract is "forwarded as-is" except
    # for the suppression-driven mutations on deny{Read,Write}, so an
    # absent key should stay absent.
    if "additionalDirectories" in fs:
        new_fs["additionalDirectories"] = additional
    for layer_key in ("denyRead", "denyWrite"):
        entries = list(fs.get(layer_key) or [])
        kept: list[Any] = []
        for entry in entries:
            normalized = _normalize_sandbox_entry(entry)
            if normalized is None:
                # Unrecognized shape: keep as-is so the launcher sees
                # the operator's original input.
                kept.append(entry)
                continue
            substituted_path = _substitute(normalized.path, mapping)
            anchor_base = _anchor_base_path(normalized.anchor, ctx)
            literal = _literal_path_prefix(substituted_path)
            absolute_pattern = substituted_path.startswith("/")

            anchored_relative_glob = False
            target_literal: str
            if literal is None and absolute_pattern:
                # Absolute pure-glob (e.g. ``/*``) -- without fnmatch'ing
                # the actual filesystem we can't compute reachability,
                # so keep the entry as-is.
                kept.append(entry)
                continue
            if literal is None:
                # Pure-glob anchored at the entry's anchor (worker_dir
                # by default for legacy strings; home / claude_org_path
                # / absolute when explicit).
                if normalized.anchor == "absolute":
                    # No anchor base to fall back on; can't reason
                    # about reachability without literal -> keep.
                    kept.append(entry)
                    continue
                target_literal = anchor_base
                anchored_relative_glob = True
            else:
                if os.path.isabs(literal):
                    target_literal = literal
                elif anchor_base:
                    # realpath the anchor base first so target/realpath
                    # composition matches the pre-Phase-1 worker_dir
                    # semantics on real filesystems.
                    target_literal = os.path.join(
                        realpath_fn(anchor_base), literal
                    )
                else:
                    # anchor=absolute with a relative path is malformed
                    # (no anchor base to join against). Resolving it
                    # against CWD would produce surprising suppressions,
                    # so keep-as-is and let the launcher / drift CI
                    # surface the issue.
                    kept.append(entry)
                    continue

            target_rp = realpath_fn(target_literal)
            if _is_inside_root(target_rp, read_roots):
                kept.append(entry)
                continue
            if not normalized.suppress_on_symlink_escape:
                kept.append(entry)
                continue
            if anchored_relative_glob:
                reason = (
                    f"{normalized.anchor} realpath escapes sandbox read "
                    f"roots (anchored relative pattern)"
                )
                # Preserve the legacy worker_dir wording for the common
                # case so existing operators / dashboards keep parsing
                # the message the same way.
                if normalized.anchor == "worker_dir":
                    reason = (
                        "worker_dir realpath escapes sandbox read "
                        "roots (anchored relative pattern)"
                    )
            else:
                reason = "realpath escapes sandbox read roots"
            metadata.suppressions.append(
                SandboxSuppression(
                    layer=f"sandbox.filesystem.{layer_key}",
                    entry=entry,
                    reason=reason,
                    realpath=target_rp,
                    sandbox_read_roots=tuple(read_roots),
                )
            )
        new_fs[layer_key] = kept

    new_sandbox = {**sandbox, "filesystem": new_fs}
    return new_sandbox, metadata


_ROLE_KIND_TO_SCHEMA_KEY = {
    "worker": "worker_roles",
    "org": "roles",
}


def render_role_with_metadata(
    schema: dict,
    role: str,
    worker_dir: str,
    claude_org_path: str,
    *,
    role_kind: str = "worker",
    base_clone: str | None = None,
    task_id: str | None = None,
    branch_ref: str | None = None,
    pattern: str | None = None,
    realpath_fn: Callable[[str], str] = os.path.realpath,
    wsl_detector: Callable[[], bool] = _detect_wsl,
) -> RenderResult:
    """Render the per-role ``settings.local.json`` plus suppression metadata.

    Same substitution rules as :func:`render_role`. When the role
    declares an enabled ``sandbox`` object, Layer 3 suppression is
    applied (see :func:`_evaluate_sandbox_suppressions`); the rendered
    sandbox object reflects the suppression while
    ``permissions.deny`` is preserved untouched.

    ``role_kind`` selects which schema bucket to look up the role in:
    ``'worker'`` (default, ``schema['worker_roles']``) preserves the
    pre-Phase-1 behavior; ``'org'`` looks the role up in
    ``schema['roles']`` so Phase 1 callers can render the org-side
    sandbox intent for secretary / dispatcher / curator.

    Pattern B context (``base_clone`` / ``task_id`` / ``branch_ref``)
    is optional. When supplied, the matching ``{...}`` placeholders are
    substituted alongside ``{worker_dir}`` / ``{claude_org_path}`` in
    every string in the rendered template. ``pattern`` is informational
    metadata; the renderer does not branch on it directly.
    """
    schema_key = _ROLE_KIND_TO_SCHEMA_KEY.get(role_kind)
    if schema_key is None:
        raise ValueError(
            f"unknown role_kind: {role_kind!r}. "
            f"valid: {sorted(_ROLE_KIND_TO_SCHEMA_KEY)}"
        )
    roles = schema.get(schema_key) or {}
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
        kind_label = "worker role" if role_kind == "worker" else "org role"
        raise KeyError(
            f"unknown {kind_label}: {role!r}. available: {available}"
        )
    ctx = GeneratorContext(
        worker_dir=worker_dir,
        claude_org_path=claude_org_path,
        base_clone=base_clone,
        task_id=task_id,
        branch_ref=branch_ref,
        pattern=pattern,
    )
    template = {
        k: v for k, v in roles[role].items() if k not in _META_KEYS
    }
    rendered = _substitute(template, _build_substitution_mapping(ctx))
    sandbox = rendered.get("sandbox")
    if isinstance(sandbox, dict):
        new_sandbox, metadata = _evaluate_sandbox_suppressions(
            sandbox,
            ctx,
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
    *,
    role_kind: str = "worker",
    base_clone: str | None = None,
    task_id: str | None = None,
    branch_ref: str | None = None,
    pattern: str | None = None,
) -> dict:
    """Render the per-role ``settings.local.json`` content.

    Substitutes ``{worker_dir}`` and ``{claude_org_path}`` in the role's
    template, drops ``description`` / ``$comment`` metadata keys, and
    applies Phase 3 case E sandbox suppression when applicable. For the
    suppression metadata use :func:`render_role_with_metadata`.

    See :func:`render_role_with_metadata` for the Phase 1 ``role_kind``
    and Pattern B context parameters.
    """
    return render_role_with_metadata(
        schema,
        role=role,
        worker_dir=worker_dir,
        claude_org_path=claude_org_path,
        role_kind=role_kind,
        base_clone=base_clone,
        task_id=task_id,
        branch_ref=branch_ref,
        pattern=pattern,
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
    parser.add_argument(
        "--role-kind",
        choices=sorted(_ROLE_KIND_TO_SCHEMA_KEY),
        default="worker",
        help=(
            "schema bucket to look up the role in: 'worker' (default, "
            "schema['worker_roles']) or 'org' (schema['roles'], for "
            "secretary / dispatcher / curator)."
        ),
    )
    parser.add_argument(
        "--base-clone",
        default=None,
        help=(
            "Pattern B context: substituted as {base_clone} in entry "
            "paths and additionalDirectories before realpath evaluation."
        ),
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="Pattern B context: substituted as {task_id}.",
    )
    parser.add_argument(
        "--branch-ref",
        default=None,
        help="Pattern B context: substituted as {branch_ref}.",
    )
    parser.add_argument(
        "--pattern",
        default=None,
        help=(
            "Informational dispatch pattern label (e.g. 'A', 'B'); "
            "passed through to the renderer for consumers that branch on "
            "it. The renderer itself does not gate behavior on --pattern."
        ),
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
            role_kind=getattr(args, "role_kind", "worker"),
            base_clone=getattr(args, "base_clone", None),
            task_id=getattr(args, "task_id", None),
            branch_ref=getattr(args, "branch_ref", None),
            pattern=getattr(args, "pattern", None),
        )
    except (KeyError, ValueError) as exc:
        msg = exc.args[0] if exc.args else str(exc)
        print(f"error: {msg}", file=sys.stderr)
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
            role_kind=getattr(args, "role_kind", "worker"),
            base_clone=getattr(args, "base_clone", None),
            task_id=getattr(args, "task_id", None),
            branch_ref=getattr(args, "branch_ref", None),
            pattern=getattr(args, "pattern", None),
        )
    except (KeyError, ValueError) as exc:
        msg = exc.args[0] if exc.args else str(exc)
        print(f"error: {msg}", file=sys.stderr)
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
