# -*- coding: utf-8 -*-
"""workspace virtualenv inheritance helpers (Issue #130).

Covers the backend-agnostic core in :mod:`claude_org_runtime.terminal.base`:
``.venv`` discovery (present / absent / cwd-vs-root_cwd fallback), the POSIX
post-profile login-shell PATH wrapper, and the platform split in
:func:`venv_pane_prep` (POSIX argv wrapper vs native-Windows ``%PATH%`` env).
"""

from __future__ import annotations

import shlex

import pytest

from claude_org_runtime.terminal import base


def _make_venv(root, *, windows: bool = False):
    """Create a fake ``.venv`` under ``root`` with the platform interpreter."""
    venv = root / ".venv"
    if windows:
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "python.exe").write_text("")
    else:
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
    return venv


# ---------------------------------------------------------------- find_workspace_venv

def test_find_venv_none_when_absent(tmp_path):
    assert base.find_workspace_venv(str(tmp_path)) is None
    # None bases are skipped, not errored.
    assert base.find_workspace_venv(None, str(tmp_path), None) is None


def test_find_venv_in_cwd(tmp_path):
    venv = _make_venv(tmp_path)
    assert base.find_workspace_venv(str(tmp_path)) == str(venv)


def test_find_venv_prefers_cwd_over_root(tmp_path):
    cwd = tmp_path / "worker"; cwd.mkdir()
    root = tmp_path / "root"; root.mkdir()
    _make_venv(cwd)
    _make_venv(root)
    # cwd/.venv wins when both exist (pane's own env takes precedence).
    assert base.find_workspace_venv(str(cwd), str(root)) == str(cwd / ".venv")


def test_find_venv_falls_back_to_root_cwd(tmp_path):
    # The usual org shape: worker worktree has no .venv, root_cwd does. Codex Major:
    # a cwd-only design would re-open the bug here, so the fallback must fire.
    cwd = tmp_path / "worker"; cwd.mkdir()
    root = tmp_path / "root"; root.mkdir()
    _make_venv(root)
    assert base.find_workspace_venv(str(cwd), str(root)) == str(root / ".venv")


def test_find_venv_ignores_dir_without_interpreter(tmp_path):
    # A bare .venv/ directory with no interpreter is not a venv -> no-op (conda etc.).
    (tmp_path / ".venv").mkdir()
    assert base.find_workspace_venv(str(tmp_path)) is None


def test_find_venv_windows_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(base.os, "name", "nt")
    venv = _make_venv(tmp_path, windows=True)
    assert base.find_workspace_venv(str(tmp_path)) == str(venv)
    # A POSIX-layout .venv (bin/python) is NOT accepted under native Windows.
    posix = tmp_path / "posix"; posix.mkdir()
    _make_venv(posix, windows=False)
    assert base.find_workspace_venv(str(posix)) is None


# ---------------------------------------------------------------- login_shell wrapper

def test_login_shell_wrapper_form(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    wrapped = base.login_shell_venv_wrapper(["claude", "--flag"], "/v/.venv/bin")
    assert wrapped[0] == "/bin/bash"
    assert wrapped[1] == "-lc"
    # -l sources the profile (which rebuilds PATH) BEFORE -c runs our prepend.
    assert wrapped[2] == 'export PATH=/v/.venv/bin:"$PATH"; exec "$@"'
    # cosmetic $0 label, then the original argv exec'd via "$@".
    assert wrapped[3] == "claude-org-runtime-venv"
    assert wrapped[4:] == ["claude", "--flag"]


def test_wrapper_shell_uses_posix_family_shell(monkeypatch):
    # POSIX-family $SHELL is borrowed verbatim (its login profile is the one we
    # supersede). Self-review MAJOR: a non-POSIX $SHELL is NOT usable.
    for sh in ("/bin/bash", "/usr/bin/zsh", "/bin/dash", "/bin/sh", "/bin/ksh"):
        monkeypatch.setenv("SHELL", sh)
        assert base._venv_wrapper_shell() == sh


def test_wrapper_shell_falls_back_to_sh_for_non_posix(monkeypatch):
    # fish / csh / tcsh cannot parse `export ...; exec "$@"` -> use /bin/sh so the
    # pane still launches (instead of a hard failure) and the venv still activates.
    for sh in ("/usr/bin/fish", "/bin/csh", "/usr/bin/tcsh", "/usr/bin/nu"):
        monkeypatch.setenv("SHELL", sh)
        assert base._venv_wrapper_shell() == "/bin/sh"
        wrapped = base.login_shell_venv_wrapper(["claude"], "/v/.venv/bin")
        assert wrapped[0] == "/bin/sh"


def test_wrapper_cds_to_run_cwd_before_exec(monkeypatch):
    # Self-review MINOR: -l sources a login profile that may `cd`; the wrapper
    # cd's back to the pane cwd after profile init, before exec.
    monkeypatch.setenv("SHELL", "/bin/bash")
    wrapped = base.login_shell_venv_wrapper(
        ["claude"], "/v/.venv/bin", run_cwd="/work/tree"
    )
    # ordering: cd (restore cwd) -> export PATH (prepend venv) -> exec original argv
    assert wrapped[2] == 'cd /work/tree; export PATH=/v/.venv/bin:"$PATH"; exec "$@"'


def test_wrapper_omits_cd_without_run_cwd(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    wrapped = base.login_shell_venv_wrapper(["claude"], "/v/.venv/bin")
    assert "cd " not in wrapped[2]


def test_wrapper_end_to_end_prepends_path_and_restores_cwd(tmp_path, monkeypatch):
    # Hermetic end-to-end proof against a real login shell: after profile init the
    # venv bin is FIRST on PATH and the process runs in run_cwd (both Blocker 2 and
    # the cd-restore fix), regardless of what /etc/profile does.
    import subprocess

    monkeypatch.setenv("SHELL", "/bin/sh")
    venv = _make_venv(tmp_path)
    run_dir = tmp_path / "work"; run_dir.mkdir()
    wrapped = base.login_shell_venv_wrapper(
        ["sh", "-c", 'pwd; printf "%s" "$PATH"'],
        str(venv / "bin"), run_cwd=str(run_dir),
    )
    out = subprocess.run(
        wrapped, capture_output=True, text=True,
        env={**__import__("os").environ, "VIRTUAL_ENV": str(venv)},
    )
    lines = out.stdout.splitlines()
    assert lines[0] == str(run_dir)                       # cd restored the pane cwd
    assert lines[1].split(":")[0] == f"{venv}/bin"        # venv/bin prepended first


def test_login_shell_wrapper_quotes_bin_dir_with_spaces(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    wrapped = base.login_shell_venv_wrapper(["claude"], "/pa th/.venv/bin")
    assert f"export PATH={shlex.quote('/pa th/.venv/bin')}:\"$PATH\"" in wrapped[2]


def test_login_shell_wrapper_defaults_shell(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    wrapped = base.login_shell_venv_wrapper(["claude"], "/v/.venv/bin")
    assert wrapped[0] == "/bin/sh"


# ---------------------------------------------------------------- venv_pane_prep

def test_pane_prep_noop_without_venv(tmp_path):
    argv = ["claude", "--flag"]
    out_argv, env = base.venv_pane_prep(argv, str(tmp_path), None)
    assert out_argv == argv and env == {}


def test_pane_prep_posix_wraps_argv_and_sets_virtual_env(tmp_path, monkeypatch):
    monkeypatch.setattr(base.os, "name", "posix")
    monkeypatch.setenv("SHELL", "/bin/bash")
    venv = _make_venv(tmp_path)
    out_argv, env = base.venv_pane_prep(["claude", "--flag"], str(tmp_path), None)
    # VIRTUAL_ENV rides the env dict; PATH rides the wrapped argv (post-profile).
    assert env == {"VIRTUAL_ENV": str(venv)}
    assert out_argv[0] == "/bin/bash" and out_argv[1] == "-lc"
    # run_cwd = the pane cwd -> cd restores it after profile, then PATH prepend.
    assert out_argv[2] == \
        f'cd {tmp_path}; export PATH={venv}/bin:"$PATH"; exec "$@"'
    assert out_argv[-2:] == ["claude", "--flag"]


def test_pane_prep_windows_uses_env_path_percent(tmp_path, monkeypatch):
    monkeypatch.setattr(base.os, "name", "nt")
    venv = _make_venv(tmp_path, windows=True)
    out_argv, env = base.venv_pane_prep(["claude"], str(tmp_path), None)
    # No argv wrapping on Windows (cmd has no PATH-rebuilding profile); PATH rides
    # the env dict with %PATH% so wezterm's cmd `set` wrapper expands it at launch.
    assert out_argv == ["claude"]
    assert env["VIRTUAL_ENV"] == str(venv)
    # bin dir is <venv>/Scripts under nt; joined with ';%PATH%' for the cmd `set`.
    assert env["PATH"] == f"{base.venv_bin_dir(str(venv))};%PATH%"


def test_pane_prep_fallback_to_root_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(base.os, "name", "posix")
    monkeypatch.setenv("SHELL", "/bin/bash")
    cwd = tmp_path / "worker"; cwd.mkdir()
    root = tmp_path / "root"; root.mkdir()
    venv = _make_venv(root)
    out_argv, env = base.venv_pane_prep(["claude"], str(cwd), str(root))
    assert env == {"VIRTUAL_ENV": str(venv)}
    # pane runs in its own cwd (worker worktree), just borrows root's .venv/bin.
    assert out_argv[2] == \
        f'cd {cwd}; export PATH={venv}/bin:"$PATH"; exec "$@"'
