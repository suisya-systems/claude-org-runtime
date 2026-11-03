"""The broker/tmux cockpit (Issue #250): every agent as a tab in one session.

``claude-org-runtime org cockpit`` keeps a session ``org`` on the broker socket
and links each agent's window into it. Each agent keeps its own session, so the
one-agent-per-session model is unchanged. ``org peek`` is the read-only mirror
the tab menu opens beside the secretary.

Membership is keyed on the ``@task`` window option the broker writes, not on
session names (they change across restarts). New spawns are linked by
:meth:`TmuxAdapter.spawn`. A window leaves the cockpit by itself when its last
pane is killed: the runtime closes agents with ``kill-pane`` only (never by
killing a session or a window, see the invariant test in test_tmux_org.py).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from typing import Callable, TextIO

from .tmux import COCKPIT_HOME_OPT, COCKPIT_SESSION, TmuxAdapter

_TARGET = f"={COCKPIT_SESSION}"


def ensure_cockpit(a: TmuxAdapter) -> list[str]:
    """Create ``org`` if missing and link every agent window not yet in it.

    The first window is "home", where the secretary runs (marked with
    ``@cockpit_home`` so :meth:`TmuxAdapter.list_panes` never reports it).
    Returns the window ids linked by this call.
    """
    existed = a._tmux("has-session", "-t", _TARGET, check=False).returncode == 0
    if not existed:
        # A concurrent creator wins the race; the session then exists anyway.
        a._tmux(
            "new-session", "-d", "-s", COCKPIT_SESSION, "-n", "home", ";",
            *_home_options(f"{_TARGET}:"), check=False,
        )
    a.apply_org_config()
    out = a._tmux(
        "list-windows", "-a", "-F", "#{session_name}\t#{window_id}\t#{@task}",
    ).stdout
    rows = [ln.split("\t") for ln in out.splitlines() if ln.count("\t") == 2]
    present = {wid for sess, wid, _ in rows if sess == COCKPIT_SESSION}
    linked: list[str] = []
    for sess, wid, task in rows:
        if sess != COCKPIT_SESSION and task and wid not in present:
            a._tmux("link-window", "-d", "-s", wid, "-t", f"{_TARGET}:")
            present.add(wid)
            linked.append(wid)
    if existed:
        _repair_home(a)
    return linked


def _home_options(target: str) -> list[str]:
    # remain-on-exit: home must outlive its shell. The tab menu's Peek splits
    # the first cockpit window, which must never become an agent's window.
    # Pane scope (the home shell, the window's only pane at this point), so
    # peek splits still close when their mirror exits.
    return ["set-option", "-w", "-t", target, COCKPIT_HOME_OPT, "1", ";",
            "set-option", "-w", "-t", target, "automatic-rename", "off", ";",
            "set-option", "-p", "-t", target, "remain-on-exit", "on"]


def _repair_home(a: TmuxAdapter) -> None:
    """Recreate home as the first window if it was ever closed."""
    marks = a._tmux("list-windows", "-t", _TARGET, "-F", f"#{{{COCKPIT_HOME_OPT}}}").stdout
    if "1" in marks.split():
        return
    wid = a._tmux("new-window", "-b", "-d", "-t", f"{_TARGET}:^", "-n", "home",
                  "-P", "-F", "#{window_id}").stdout.strip()
    a._tmux(*_home_options(wid))


def attach_argv(a: TmuxAdapter, env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Command (and environment) that puts this terminal on the cockpit.

    Inside the broker socket already: ``switch-client``. Inside another tmux:
    attach nested (TMUX unset; the org prefix is C-Space, so it does not clash
    with a default C-b outer tmux).
    """
    inside = env.get("TMUX", "")
    if inside and os.path.basename(inside.split(",")[0]) == a.socket:
        return [a.exe, "-L", a.socket, "switch-client", "-t", _TARGET], env
    env = {k: v for k, v in env.items() if k != "TMUX"}
    return [a.exe, "-L", a.socket, "attach-session", "-t", _TARGET], env


def peek(
    a: TmuxAdapter, pane: str, *, interval: float = 1.0, out: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Mirror ``pane`` read-only, redrawn every ``interval`` s, until Ctrl-C or
    until the pane is gone. Never writes to the pane and never links its window
    (viewing a window from another client resizes the agent's window)."""
    out = out or sys.stdout
    restore = _quiet_stdin()
    out.write("\x1b[?7l\x1b[?25l")  # no autowrap (clip long rows), hide cursor
    try:
        while True:
            try:
                screen = a.get_text(pane, escapes=True)
            except RuntimeError:
                out.write(f"\x1b[0m\r\n[peek] pane {pane} is gone\r\n")
                return 0
            rows = max(shutil.get_terminal_size().lines - 1, 1)
            body = screen.rstrip("\n").split("\n")[-rows:]
            out.write(
                f"\x1b[H\x1b[0m\x1b[7m peek {pane} - read-only, Ctrl-C closes \x1b[0m\x1b[K"
                + "".join(f"\r\n{ln}\x1b[0m\x1b[K" for ln in body) + "\x1b[J"
            )
            out.flush()
            sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        out.write("\x1b[0m\x1b[?7h\x1b[?25h")
        out.flush()
        restore()


def _quiet_stdin() -> Callable[[], None]:
    """Stop typed keys from echoing over the mirror; returns the undo."""
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except Exception:  # not a tty, or not POSIX
        return lambda: None
    return lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# ---------------------------------------------------------------------- CLI
def _adapter() -> TmuxAdapter | None:
    try:
        return TmuxAdapter()
    except FileNotFoundError:
        print("org cockpit needs tmux on PATH (broker tmux backend)", file=sys.stderr)
        return None


def cmd_cockpit(args: argparse.Namespace) -> int:
    a = _adapter()
    if a is None:
        return 1
    try:
        linked = ensure_cockpit(a)
    except RuntimeError as e:
        print(f"org cockpit: {e}", file=sys.stderr)
        return 1
    if args.no_attach:
        print(f"cockpit '{COCKPIT_SESSION}' ready on socket {a.socket}; "
              f"linked {len(linked)} new agent window(s)")
        return 0
    argv, env = attach_argv(a, dict(os.environ))
    os.execvpe(argv[0], argv, env)
    return 0  # pragma: no cover  (exec does not return)


def cmd_peek(args: argparse.Namespace) -> int:
    a = _adapter()
    return 1 if a is None else peek(a, args.pane, interval=args.interval)


def add_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """``claude-org-runtime org cockpit`` / ``org peek``. ASCII-only help."""
    c = subparsers.add_parser(
        "cockpit",
        help=(
            "tmux backend: create the 'org' cockpit session if missing, link every "
            "live agent window into it as a tab, and attach."
        ),
    )
    c.add_argument("--no-attach", action="store_true",
                   help="set the cockpit up but do not attach (for scripts)")
    c.set_defaults(func=cmd_cockpit)
    p = subparsers.add_parser(
        "peek",
        help="Read-only mirror of a broker tmux pane (capture-pane, about 1 s refresh).",
    )
    p.add_argument("pane", help="tmux pane id, e.g. %%5")
    p.add_argument("--interval", type=float, default=1.0,
                   help="refresh interval in seconds (default: 1.0)")
    p.set_defaults(func=cmd_peek)
