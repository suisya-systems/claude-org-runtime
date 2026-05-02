"""Reference role prompts bundled with claude-org-runtime.

These templates are English reference prompts for the secretary, dispatcher,
and curator roles used in the ``claude-org-ja`` reference organization.
They are intentionally non-prescriptive: consumers are expected to load
them as a starting point and override or adapt the contents from their
own ``CLAUDE.md`` files.
"""

from __future__ import annotations

import re
from importlib.resources import files
from typing import Literal

Role = Literal["secretary", "dispatcher", "curator"]

_VALID_ROLES: frozenset[str] = frozenset({"secretary", "dispatcher", "curator"})

__all__ = ["Role", "load", "load_meta", "available_roles"]


def available_roles() -> tuple[str, ...]:
    """Return the role names that ship with the runtime."""

    return ("secretary", "dispatcher", "curator")


def _validate(role: str) -> None:
    if role not in _VALID_ROLES:
        raise ValueError(
            f"unknown role {role!r}; expected one of {sorted(_VALID_ROLES)}"
        )


def _read(role: str) -> str:
    _validate(role)
    resource = files(__name__).joinpath("templates", f"{role}.md")
    return resource.read_text(encoding="utf-8")


def load(role: Role) -> str:
    """Return the raw markdown of the reference prompt for ``role``.

    The frontmatter block is preserved; consumers can parse it themselves
    via :func:`load_meta` or strip it as they prefer.
    """

    return _read(role)


_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<body>.*?)\r?\n---\r?\n", re.DOTALL
)
_KV_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*)$")


def load_meta(role: Role) -> dict[str, str]:
    """Return the YAML-like frontmatter of ``role`` as a flat ``dict``.

    The bundled templates use a tiny ``key: value`` frontmatter (no nested
    structures), so this parser is intentionally limited: one entry per
    line, no list/dict values, no quoting rules. It is sufficient for the
    fields shipped here (``role``, ``source``, ``status``).
    """

    text = _read(role)
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError(f"prompt for role {role!r} is missing frontmatter")
    meta: dict[str, str] = {}
    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        kv = _KV_RE.match(line)
        if kv is None:
            raise ValueError(
                f"unparseable frontmatter line in {role!r}: {raw_line!r}"
            )
        meta[kv.group("key")] = kv.group("value").strip()
    return meta
