"""Single source of truth for "which host path does this deny entry name".

Claude Code folds both deny layers into the bwrap deny set: Layer 2
``permissions.deny`` ``Read`` / ``Edit`` / ``Write`` rules and Layer 3
``sandbox.filesystem.deny{Read,Write}`` entries. The generator rewrites the
ones that cross an absolute symlink and ``sandbox doctor`` flags them; both
must agree on which entries name a concrete host path and what that path
is. They used to answer that question with separate code, and every
divergence (``~/`` expanded on one side only, ``startswith("/")`` dropping
Windows drive paths) was a deny path one of them silently skipped. Both now
call :func:`resolve_deny_entry` and nothing else.

Resolution is independent of ``sandbox.enabled``: deny arrays are unioned
across settings scopes, so an entry under a locally disabled sandbox still
reaches bwrap once any other scope enables it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

PERMISSION_DENY_LAYER = "permissions.deny"
SANDBOX_DENY_KEYS = ("denyRead", "denyWrite")

# Layer 2 tools whose argument is a filesystem path. ``Read`` / ``Edit`` are
# the pair Claude Code's sandbox docs name as contributing to the deny set;
# ``Write`` is included because this repo's own schema and docs treat it as a
# Layer 2 filesystem deny (``role_configs_schema.json`` ships
# ``Write(*/workers/*/...)`` entries). Canonicalizing a rule that turns out
# not to reach bwrap is harmless -- the realpath form denies the same files,
# since path matching resolves symlinks -- whereas omitting one that does
# reach it leaves the sandbox-launch failure in place.
PERMISSION_PATH_TOOLS = ("Read", "Edit", "Write")


@dataclass(frozen=True)
class ResolvedDeny:
    """A deny entry that names a concrete host path.

    ``path`` keeps any glob tail. ``tool`` is the Layer 2 tool name
    (``Read`` etc.) so a caller can rebuild the rule; ``None`` on Layer 3.
    """

    path: str
    tool: str | None = None


def split_permission_rule(rule: Any) -> tuple[str, str] | None:
    """Split ``'Read(~/.aws/*)'`` into ``('Read', '~/.aws/*')``.

    Returns ``None`` for anything that is not a well-formed
    ``Tool(argument)`` string so the caller passes it through untouched.
    """
    if not isinstance(rule, str) or not rule.endswith(")"):
        return None
    open_idx = rule.find("(")
    if open_idx <= 0:
        return None
    return rule[:open_idx], rule[open_idx + 1 : -1]


def _expand_home(spec: str) -> str | None:
    """``~/x`` -> ``<home>/x``; ``None`` when ``spec`` is not home-anchored.

    Only the anchor is substituted; the remainder keeps its authored ``/``
    separators. On Windows that yields a mixed spelling
    (``C:\\Users\\u/.aws/*``), which is deliberate: rule paths separate
    with ``/``, every OS accepts ``/`` for the filesystem probing this
    feeds, and normalizing would rewrite the glob tail into a spelling
    the rule grammar does not use.
    """
    if spec.startswith("~/"):
        return os.path.expanduser("~") + spec[1:]
    return None


def permission_rule_host_path(spec: str) -> str | None:
    """Absolute host path a ``Read`` / ``Edit`` rule spec anchors at.

    Per https://code.claude.com/docs/en/permissions the rule syntax uses
    ``//path`` for an absolute path and ``~/`` for a home-relative one; a
    bare or single-slash spec is project-relative. Only the first two name
    a concrete host path that Claude Code can expand into the bwrap deny
    set. Unanchored globs such as ``**/credentials*`` return ``None`` too,
    which matches the observed behavior: they never made bwrap fail
    because they are not expanded into host paths.
    """
    home = _expand_home(spec)
    if home is not None:
        return home
    if spec.startswith("//"):
        return spec[1:]
    return None


def sandbox_entry_host_path(entry: str) -> str | None:
    """Absolute host path a Layer 3 string entry names, if any.

    ``~/`` is expanded because Claude Code resolves that prefix against
    the home directory when building the deny set. The absolute test is
    ``os.path.isabs``, not ``startswith("/")``: a Windows entry begins
    with a drive letter, and the prefix test silently dropped every one.
    """
    path = _expand_home(entry) or entry
    return path if os.path.isabs(path) else None


def resolve_deny_entry(entry: Any, *, layer: str) -> ResolvedDeny | None:
    """Resolve one deny entry to the host path it contributes to bwrap.

    ``layer`` is :data:`PERMISSION_DENY_LAYER` or
    ``sandbox.filesystem.<key>`` for a key in :data:`SANDBOX_DENY_KEYS`.
    Returns ``None`` when the entry names no concrete host path: a
    non-path tool, a project-relative or unanchored spec, or a value that
    is not a string. Rendered settings carry only string entries;
    structured ``{anchor, path}`` entries are resolved to strings by the
    generator before this point, so callers that must flag a surviving
    non-string entry check the type themselves.
    """
    if not isinstance(entry, str):
        return None
    if layer == PERMISSION_DENY_LAYER:
        parsed = split_permission_rule(entry)
        if parsed is None:
            return None
        tool, spec = parsed
        if tool not in PERMISSION_PATH_TOOLS:
            return None
        path = permission_rule_host_path(spec)
        return None if path is None else ResolvedDeny(path=path, tool=tool)
    if layer in tuple(f"sandbox.filesystem.{k}" for k in SANDBOX_DENY_KEYS):
        path = sandbox_entry_host_path(entry)
        return None if path is None else ResolvedDeny(path=path)
    raise ValueError(f"unknown deny layer: {layer!r}")
