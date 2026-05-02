"""Schema-driven worker ``.claude/settings.local.json`` generator.

Port of claude-org-ja ``tools/generate_worker_settings.py``. The schema
SoT (``role_configs_schema.json``) now ships inside this package, so
consumers no longer need to keep their copy under ``tools/`` in sync;
they install ``claude-org-runtime`` and invoke this module.

CLI parity with the in-tree script is preserved -- the ``--role`` /
``--worker-dir`` / ``--claude-org-path`` / ``--out`` / ``--schema``
arguments behave identically. ``--schema`` defaults to the bundled
schema instead of ``<repo>/tools/role_configs_schema.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

# Keys under worker_roles[<role>] that are metadata, not part of the emitted
# settings.local.json content.
_META_KEYS = {"description", "$comment"}


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


def render_role(
    schema: dict,
    role: str,
    worker_dir: str,
    claude_org_path: str,
) -> dict:
    """Render the per-role ``settings.local.json`` content.

    Substitutes the ``{worker_dir}`` and ``{claude_org_path}`` placeholders
    inside the role's template, dropping the ``description`` /
    ``$comment`` metadata keys.
    """
    roles = schema.get("worker_roles") or {}
    available = sorted(
        k for k, v in roles.items()
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
    return _substitute(
        template,
        {"worker_dir": worker_dir, "claude_org_path": claude_org_path},
    )


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
