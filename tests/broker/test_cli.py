# -*- coding: utf-8 -*-
"""Tests for the broker daemon CLI wiring (parser construction only).

``run`` blocks in a serve loop, so these exercise the argument parser and
the top-level CLI integration rather than actually starting the daemon.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.broker import cli as broker_cli
from claude_org_runtime.cli import build_parser as build_top_parser


def test_broker_parser_defaults():
    parser = broker_cli.build_parser()
    args = parser.parse_args(["serve"])
    assert args.state_dir == broker_cli.DEFAULT_STATE_DIR == ".state/broker"
    assert args.port == broker_cli.DEFAULT_PORT
    assert args.host == "127.0.0.1"
    assert args.no_nudge is False
    assert args.func is broker_cli.run


def test_broker_parser_overrides():
    parser = broker_cli.build_parser()
    args = parser.parse_args(
        ["serve", "--port", "0", "--state-dir", "/tmp/q",
         "--backend", "tmux", "--no-nudge"]
    )
    assert args.port == 0
    assert args.state_dir == "/tmp/q"
    assert args.backend == "tmux"
    assert args.no_nudge is True


def test_broker_rejects_unknown_backend():
    parser = broker_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--backend", "screen"])


def test_top_level_cli_exposes_broker_serve():
    parser = build_top_parser()
    args = parser.parse_args(["broker", "serve", "--no-nudge"])
    assert args.func is broker_cli.run
    assert args.no_nudge is True
