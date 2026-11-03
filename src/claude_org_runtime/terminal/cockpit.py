"""The broker/tmux cockpit (Issue #250): every agent as a tab in one session.

``claude-org-runtime org cockpit`` keeps a session ``org`` on the broker socket
and links each agent's window into it. Each agent keeps its own session, so the
one-agent-per-session model is unchanged. The same command runs the secretary
in the first window ("home"): ``org up`` on the first start, ``org adopt
--continue`` when the secretary exited while the daemon stayed up, nothing when
it is already running. So one command both starts the org and reconnects.
``org peek`` is the read-only mirror the tab menu opens beside the secretary.

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

from ..broker import launcher, sidecar
from .base import default_backend
from .tmux import COCKPIT_HOME_OPT, COCKPIT_SESSION, TmuxAdapter

_TARGET = f"={COCKPIT_SESSION}"
# Pane option on the pane that hosts the secretary. Set before the respawn: a
# marked pane is never killed (only respawned once it is dead).
SECRETARY_OPT = "@org_secretary"
_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh"})
_PANE_FORMAT = (f"#{{pane_id}}\t#{{{COCKPIT_HOME_OPT}}}\t#{{{SECRETARY_OPT}}}"
                "\t#{pane_dead}\t#{pane_current_command}")


def ensure_cockpit(a: TmuxAdapter) -> list[str]:
    """Create ``org`` if missing and link every agent window not yet in it,
    naming and pinning every agent tab.

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
        "list-windows", "-a", "-F",
        "#{session_name}\t#{window_id}\t#{@task}\t#{window_name}\t#{automatic-rename}",
    ).stdout
    rows = [ln.split("\t") for ln in out.splitlines() if ln.count("\t") == 4]
    present = {r[1] for r in rows if r[0] == COCKPIT_SESSION}
    linked: list[str] = []
    for sess, wid, task, name, auto in rows:
        if not task:
            continue
        in_cockpit = sess == COCKPIT_SESSION
        if in_cockpit or wid not in present:
            # Issues #259/#263: name and pin every agent tab, including ones
            # linked before; windows already right are left alone.
            if name != task or auto != "off":
                a._tmux("rename-window", "-t", wid, task, ";",
                        "set-option", "-w", "-t", wid, "automatic-rename", "off")
        if not in_cockpit and wid not in present:
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


def home_pane(a: TmuxAdapter) -> tuple[str, str]:
    """Which cockpit pane hosts the secretary, and what to do with it.

    ``running``     marked pane alive (also during a login-shell phase): leave it
    ``relaunch``    marked pane dead: respawn it (no -k needed)
    ``launch``      unmarked dead home pane: respawn it (no -k needed)
    ``launch_kill`` unmarked idle shell in home: respawn -k (kills only that shell)
    ``error``       ambiguous or nothing usable: the second item is the reason
    """
    out = a._tmux("list-panes", "-s", "-t", _TARGET, "-F", _PANE_FORMAT).stdout
    rows = [ln.split("\t") for ln in out.splitlines() if ln.count("\t") == 4]
    marked = [r for r in rows if r[2] == "1"]
    if len(marked) > 1:
        return "error", f"more than one pane carries {SECRETARY_OPT}"
    if marked:
        return ("relaunch" if marked[0][3] == "1" else "running"), marked[0][0]
    home = [r for r in rows if r[1] == "1"]
    for pid, _, _, dead, _ in home:
        if dead == "1":
            return "launch", pid
    for pid, _, _, _, cmd in home:
        if cmd in _SHELLS:
            return "launch_kill", pid
    return "error", "no idle shell in the cockpit home window"


def _flags(args: argparse.Namespace, names: dict[str, str]) -> list[str]:
    out: list[str] = []
    for attr, flag in names.items():
        val = getattr(args, attr, None)
        for v in val if isinstance(val, list) else [val]:
            if v is True:
                out.append(flag)
            elif v not in (None, False):
                out.append(f"{flag}={v}")  # '=' keeps values like '--x' intact
    return out


_UP_FLAGS = {"state_dir": "--state-dir", "backend": "--backend", "root_cwd": "--root-cwd",
             "name": "--name", "model": "--model", "permission_mode": "--permission-mode",
             "claude_arg": "--claude-arg", "reap": "--reap"}
_ADOPT_FLAGS = {"state_dir": "--state-dir", "name": "--owner", "root_cwd": "--root-cwd",
                "model": "--model", "permission_mode": "--permission-mode",
                "claude_arg": "--claude-arg"}


def secretary_command(action: str, args: argparse.Namespace, daemon_alive: bool) -> list[str]:
    """The runtime command for the home pane: the same ``org up`` as a plain
    terminal would run, or ``org adopt --continue`` for a restart. ``org up``
    on a daemon that still has the secretary registered launches nothing."""
    rt = [sys.executable, "-m", "claude_org_runtime.cli", "org"]
    if action == "relaunch" and daemon_alive:
        return rt + ["adopt", "--continue", *_flags(args, _ADOPT_FLAGS)]
    return rt + ["up", *_flags(args, _UP_FLAGS)]


def start_secretary(
    a: TmuxAdapter, args: argparse.Namespace, *, env: dict[str, str], cwd: str,
    daemon_alive: Callable[[], bool], adoptable: Callable[[], bool] = lambda: True,
) -> int:
    """Start (or leave running) the secretary in the cockpit home window.

    Call under :func:`_lock` so two invocations cannot both launch. ``env`` is
    the caller's environment: the tmux server's belongs to whoever started it.
    """
    action, pane = home_pane(a)
    if action == "error":
        print(f"org cockpit: {pane}; nothing started", file=sys.stderr)
        return 1
    if action == "running":
        print(f"org cockpit: the secretary is already running in {pane}")
        return 0
    alive = daemon_alive()
    if alive and action != "relaunch":
        # A daemon is up but no cockpit pane is marked: the secretary runs
        # elsewhere. org up would only say "already up" and leave a marked dead
        # pane, which the next run would adopt, fencing that live secretary.
        print("org cockpit: a broker daemon is already up and no secretary pane is "
              "marked in the cockpit, so the secretary runs outside it; not starting "
              "another. To move it here, exit it and run 'claude-org-runtime org "
              "adopt --continue' in the home window.", file=sys.stderr)
        return 0
    # A live daemon with no bind for the owner cannot be adopted (unknown_owner):
    # start a fresh secretary instead.
    cmd = secretary_command(action, args, alive and adoptable())
    a._tmux("set-option", "-p", "-t", pane, SECRETARY_OPT, "1")
    argv = ["respawn-pane", *(["-k"] if action == "launch_kill" else []),
            "-t", pane, "-c", cwd]
    for k, v in env.items():
        if k not in ("TMUX", "TMUX_PANE"):
            argv += ["-e", f"{k}={v}"]
    res = a._tmux(*argv, *cmd, check=False)
    if res.returncode != 0:
        # Unmark, or the live shell would count as a running secretary.
        a._tmux("set-option", "-pu", "-t", pane, SECRETARY_OPT, check=False)
        print(f"org cockpit: respawn-pane failed: {res.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"org cockpit: started the secretary in {pane} ({action})")
    return 0


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


def _daemon_alive(state_dir: str, owner: str, key: str = "ok") -> bool:
    """A broker daemon answers on ``state_dir`` (what ``org adopt --status`` asks);
    ``key="adoptable"`` also needs it to hold a live bind for ``owner``."""
    res = launcher._resolve_running_daemon(state_dir)
    if res["kind"] != "ok":
        return False
    try:
        r = launcher._admin_rpc(res["host"], res["port"], res["admin_token"],
                                "adopt_status", {"owner": owner})
    except OSError:  # URLError, refused, timeout
        return False
    return bool(r and r.get("ok") and r.get(key, True))


def _backend(args: argparse.Namespace, state_dir: str) -> str:
    # A running daemon's backend wins (a stale sidecar does not: org up replaces
    # it); otherwise what org up would pick.
    recorded = (sidecar.read_sidecar(state_dir) or {}).get("backend")
    if recorded and _daemon_alive(state_dir, args.name):
        return recorded
    return args.backend or default_backend()


def _lock(state_dir: str) -> TextIO:
    """Exclusive lock serialising inspect + mark; released when closed."""
    import fcntl  # POSIX only, like tmux itself
    os.makedirs(state_dir, exist_ok=True)
    fh = open(os.path.join(state_dir, "cockpit.lock"), "w", encoding="utf-8")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def cmd_cockpit(args: argparse.Namespace) -> int:
    state_dir = sidecar.absolutize(args.state_dir)
    # Only starting the secretary depends on the backend (org up / adopt);
    # --attach-only launches nothing and just needs tmux, as before.
    backend = "tmux" if args.attach_only else _backend(args, state_dir)
    if backend != "tmux":
        print(f"org cockpit needs the tmux backend (this org uses {backend!r})",
              file=sys.stderr)
        return 2
    a = _adapter()
    if a is None:
        return 1
    try:
        linked = ensure_cockpit(a)
        if not args.attach_only:
            with _lock(state_dir):
                rc = start_secretary(
                    a, args, env=dict(os.environ), cwd=os.getcwd(),
                    daemon_alive=lambda: _daemon_alive(state_dir, args.name),
                    adoptable=lambda: _daemon_alive(state_dir, args.name, "adoptable"),
                )
            if rc != 0:
                return rc
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
            "live agent window into it as a tab, start the secretary in the home "
            "window when it is not running (org up, or org adopt --continue when "
            "the daemon is still up), and attach. Takes the org up flags."
        ),
    )
    c.add_argument("--no-attach", action="store_true",
                   help="set the cockpit up and start the secretary, but do not attach")
    c.add_argument("--attach-only", action="store_true",
                   help=("never start the secretary; only set the cockpit up and "
                         "attach (with --no-attach: only set it up)"))
    launcher._add_up_arguments(c)
    c.set_defaults(func=cmd_cockpit)
    p = subparsers.add_parser(
        "peek",
        help="Read-only mirror of a broker tmux pane (capture-pane, about 1 s refresh).",
    )
    p.add_argument("pane", help="tmux pane id, e.g. %%5")
    p.add_argument("--interval", type=float, default=1.0,
                   help="refresh interval in seconds (default: 1.0)")
    p.set_defaults(func=cmd_peek)
