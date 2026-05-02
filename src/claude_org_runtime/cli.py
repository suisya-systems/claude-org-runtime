"""Unified ``claude-org-runtime`` CLI.

Subcommands:

- ``dispatcher delegate-plan ...`` -> :mod:`claude_org_runtime.dispatcher.runner`
- ``settings generate ...`` -> :mod:`claude_org_runtime.settings.generator`
- ``migrate ...`` -> :mod:`claude_org_runtime.migrate.v1_to_v2`

The subcommands re-use the same parser builders the per-module CLIs
expose, so flags stay in lock-step.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import __version__
from .dispatcher import runner as dispatcher_runner
from .migrate import v1_to_v2 as migrate_v1_to_v2
from .settings import generator as settings_generator


def _run_settings_generate(args: argparse.Namespace) -> int:
    return settings_generator.run(args)


def _run_migrate_v1_to_v2(args: argparse.Namespace) -> int:
    return migrate_v1_to_v2.run(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-org-runtime",
        description=(
            "Python runtime for claude-org-ja: dispatcher runner, "
            "settings generator, state-schema migrate."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"claude-org-runtime {__version__}",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # dispatcher
    dispatcher_p = sub.add_parser(
        "dispatcher",
        help="Dispatcher state-machine helpers (delegate-plan ...)",
    )
    dispatcher_sub = dispatcher_p.add_subparsers(dest="cmd", required=True)
    dispatcher_runner.add_subparsers(dispatcher_sub)

    # settings
    settings_p = sub.add_parser(
        "settings",
        help="Worker settings.local.json generator",
    )
    settings_sub = settings_p.add_subparsers(dest="cmd", required=True)
    gen_p = settings_sub.add_parser(
        "generate",
        help=(
            "Render a per-role settings.local.json from the bundled "
            "role_configs_schema.json"
        ),
    )
    settings_generator.add_arguments(gen_p)
    gen_p.set_defaults(func=_run_settings_generate)

    # migrate
    migrate_p = sub.add_parser(
        "migrate",
        help="State-schema migration helpers (v1 -> v2)",
    )
    migrate_sub = migrate_p.add_subparsers(dest="cmd", required=True)
    v1v2_p = migrate_sub.add_parser(
        "v1-to-v2",
        help=(
            "Migrate a v1 .state/ artefact (journal.jsonl or org-state.md) "
            "to the v2 polymorphic schema."
        ),
    )
    migrate_v1_to_v2.add_arguments(v1v2_p)
    v1v2_p.set_defaults(func=_run_migrate_v1_to_v2)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
