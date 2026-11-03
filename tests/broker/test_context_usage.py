"""Issue #262: the cockpit's per-agent context-window usage (``@ctx``).

The value comes from Claude Code's own transcript (the same source renga's
sidebar uses): the newest ``~/.claude/projects/<encoded cwd>/*.jsonl``, last
assistant ``usage`` = input + cache_read + cache_creation tokens, divided by
the model's window. Unknown usage must publish nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from .test_agent_state import OptionAdapter, _broker, _poll_once

CWD = "/home/u/work/org/workers/foo bar/日本"
ENCODED = "-home-u-work-org-workers-foo-bar---"  # non [A-Za-z0-9.] -> "-"


def _line(model="claude-sonnet-5-5", inp=1, cr=0, cc=0, **extra) -> str:
    msg = {"model": model, "usage": {"input_tokens": inp, "output_tokens": 9,
                                     "cache_read_input_tokens": cr,
                                     "cache_creation_input_tokens": cc}}
    return json.dumps({"type": "assistant", "message": msg, **extra}, ensure_ascii=False)


def _transcript(home: Path, cwd: str, *lines: str, name="s.jsonl", mtime=None) -> Path:
    from claude_org_runtime.broker.context_usage import project_dir_name
    d = home / ".claude" / "projects" / project_dir_name(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_project_dir_name_matches_claude_code() -> None:
    from claude_org_runtime.broker.context_usage import project_dir_name
    assert project_dir_name("/home/u/a.b_c") == "-home-u-a.b-c"
    assert project_dir_name(CWD) == ENCODED


@pytest.mark.parametrize("model,limit", [
    ("claude-sonnet-5-5", 200_000),
    ("claude-haiku-5-5", 200_000),
    ("claude-opus-5-5", 1_000_000),
    ("claude-opus-4-6", 1_000_000),
    ("claude-opus-4-1", 500_000),
    ("claude-fable-5-1", 1_000_000),
    ("claude-sonnet-4-5[1m]", 1_000_000),
    ("something-new", 200_000),
    (None, 200_000),
])
def test_context_limit(model, limit) -> None:
    from claude_org_runtime.broker.context_usage import context_limit
    assert context_limit(model) == limit


def test_percent_uses_last_assistant_usage(tmp_path) -> None:
    from claude_org_runtime.broker.context_usage import context_percent
    _transcript(tmp_path, CWD, _line(inp=10, cr=1000), "not json",
                json.dumps({"type": "user"}),
                _line(inp=2, cr=60_000, cc=40_000))  # 100_002 / 200k
    assert context_percent(CWD, home=tmp_path) == "50%"


def test_percent_skips_sidechain_and_synthetic(tmp_path) -> None:
    from claude_org_runtime.broker.context_usage import context_percent
    _transcript(tmp_path, CWD, _line(inp=20_000),
                _line(inp=190_000, isSidechain=True),
                _line(model="<synthetic>", inp=0))
    assert context_percent(CWD, home=tmp_path) == "10%"


def test_percent_picks_newest_transcript_and_clamps(tmp_path) -> None:
    from claude_org_runtime.broker.context_usage import context_percent
    _transcript(tmp_path, CWD, _line(inp=20_000), name="old.jsonl", mtime=1_000)
    _transcript(tmp_path, CWD, _line(inp=900_000), name="new.jsonl", mtime=2_000)
    assert context_percent(CWD, home=tmp_path) == "100%"


def test_percent_reads_only_the_tail_of_a_big_transcript(tmp_path) -> None:
    from claude_org_runtime.broker.context_usage import context_percent
    filler = [_line(inp=1) + " " * 4000] * 400  # ~1.6 MB of older turns
    _transcript(tmp_path, CWD, *filler, _line(inp=100_000))
    assert context_percent(CWD, home=tmp_path) == "50%"


@pytest.mark.parametrize("cwd", [None, "", "/no/such/dir"])
def test_percent_unknown_is_none(tmp_path, cwd) -> None:
    from claude_org_runtime.broker.context_usage import context_percent
    assert context_percent(cwd, home=tmp_path) is None


def test_percent_without_usage_is_none(tmp_path) -> None:
    from claude_org_runtime.broker.context_usage import context_percent
    _transcript(tmp_path, CWD, json.dumps({"type": "user"}), _line(inp=0))
    assert context_percent(CWD, home=tmp_path) is None


# ---------------------------------------------------------------- broker poll
def _ctx_writes(a: OptionAdapter) -> list[tuple]:
    return [(p, o["ctx"]) for p, o in a.options if "ctx" in o]


def test_poller_publishes_ctx_on_change_and_clears_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    home = tmp_path / "home"
    a = OptionAdapter()
    b = _broker(tmp_path, a)
    b._register_pane("%1", "fix-1", "fix-1", "worker", CWD, "claude", None)
    b._register_pane("%2", "nocwd", "nocwd", "worker", None, "claude", None)
    p = _transcript(home, CWD, _line(inp=20_000))
    _poll_once(b)
    assert _ctx_writes(a) == [("%1", "10%")]  # %2 has no cwd: nothing
    _poll_once(b)
    assert _ctx_writes(a) == [("%1", "10%")]  # unchanged: no write
    _transcript(home, CWD, _line(inp=100_000), name=p.name)
    _poll_once(b)
    assert _ctx_writes(a)[-1] == ("%1", "50%")
    p.unlink()
    _poll_once(b)
    assert _ctx_writes(a)[-1] == ("%1", "")  # went unknown: cleared, not stale


def test_poller_publishes_ctx_for_codex_never(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    a = OptionAdapter()
    b = _broker(tmp_path, a)
    b._register_pane("%1", "cx", "cx", "worker", CWD, "codex", None)
    _transcript(tmp_path, CWD, _line(inp=20_000))
    _poll_once(b)
    assert _ctx_writes(a) == []  # the transcript format is Claude's


class SecretaryAdapter(OptionAdapter):
    def secretary_pane_id(self):
        return "%9"


def test_poller_publishes_secretary_ctx_to_the_home_pane(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    a = SecretaryAdapter()
    b = _broker(tmp_path, a)
    tok = b.issue_token("secretary", "secretary", "secretary", kind="claude", cwd=CWD)
    b.register_logical_pane(tok)
    _transcript(tmp_path / "home", CWD, _line(inp=20_000))
    _poll_once(b)
    assert _ctx_writes(a) == [("%9", "10%")]
    assert not any("state" in o for _, o in a.options)  # logical: no @state


def test_secretary_without_cockpit_pane_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    a = OptionAdapter()  # no secretary_pane_id (not tmux)
    b = _broker(tmp_path, a)
    tok = b.issue_token("secretary", "secretary", "secretary", kind="claude", cwd=CWD)
    b.register_logical_pane(tok)
    _transcript(tmp_path / "home", CWD, _line(inp=20_000))
    _poll_once(b)
    assert a.options == []


def test_secretary_pane_id_reads_the_marked_pane(tmp_path) -> None:
    from claude_org_runtime.terminal.tmux import TmuxAdapter
    a = TmuxAdapter.__new__(TmuxAdapter)

    class R:
        returncode = 0
        stdout = "%1\t\n%4\t1\n%5\t\n"

    a._tmux = lambda *args, **kw: R()  # type: ignore[method-assign]
    assert a.secretary_pane_id() == "%4"
    R.stdout = "%1\t\n"
    assert a.secretary_pane_id() is None


def test_root_pane_without_kind_still_gets_ctx_on_the_home_pane(tmp_path, monkeypatch) -> None:
    # `org run` registers the root via issue_root_token, which sets no kind.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    a = SecretaryAdapter()
    b = _broker(tmp_path, a)
    tok = b.issue_token("manual-test", "manual-test", "secretary", cwd=CWD, auth_role="secretary")
    b.register_logical_pane(tok)
    _transcript(tmp_path / "home", CWD, _line(inp=20_000))
    _poll_once(b)
    assert _ctx_writes(a) == [("%9", "10%")]
