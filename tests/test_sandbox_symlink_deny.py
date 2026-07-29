"""Tests for the bwrap symlink-deny fix.

Regression coverage for the failure mode where a deny path crossing an
absolute symlink makes bubblewrap abort at launch, after which Claude Code
silently retries every Bash command unsandboxed. See the module note in
``claude_org_runtime.settings.generator`` for the empirical characterization
this suite encodes.

The fixtures build real symlinks on disk rather than mocking ``os.path``:
the whole bug is about how the *filesystem* resolves a path chain, so a
mocked chain would not have caught it.
"""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_org_runtime import cli as runtime_cli
from claude_org_runtime.settings import generator, sandbox_doctor


@pytest.fixture()
def escaping_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``$HOME`` whose ``.aws`` is an *absolute* symlink elsewhere.

    Mirrors the real WSL2 layout that triggered the bug: ``~/.aws`` is a
    symlink to a directory outside the home tree (there, ``/mnt/c/...``)
    and the credential files exist on the far side of the link.
    """
    home = tmp_path / "home"
    home.mkdir()
    external = tmp_path / "external" / ".aws"
    external.mkdir(parents=True)
    (external / "config").write_text("not-a-real-credential\n", encoding="utf-8")
    (home / ".aws").symlink_to(external)

    # A real (non-symlinked) credential dir, to prove we only rewrite the
    # entries that actually need it.
    (home / ".ssh").mkdir()
    (home / ".ssh" / "known_hosts").write_text("", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# ---------------------------------------------------------------------------
# _absolute_symlink_in_chain
# ---------------------------------------------------------------------------


def test_absolute_symlink_in_chain_detects_link_in_ancestor(
    escaping_home: Path,
) -> None:
    hit = generator._absolute_symlink_in_chain(str(escaping_home / ".aws"))
    assert hit == str(escaping_home / ".aws")


def test_absolute_symlink_in_chain_detects_link_above_target(
    escaping_home: Path,
) -> None:
    # The offending link is an *ancestor* of the deny path, not the path
    # itself; bwrap fails all the same.
    hit = generator._absolute_symlink_in_chain(
        str(escaping_home / ".aws" / "config")
    )
    assert hit == str(escaping_home / ".aws")


def test_absolute_symlink_in_chain_ignores_clean_path(
    escaping_home: Path,
) -> None:
    assert (
        generator._absolute_symlink_in_chain(str(escaping_home / ".ssh")) is None
    )


def test_absolute_symlink_in_chain_ignores_relative_symlink(
    tmp_path: Path,
) -> None:
    """Relative links resolve inside bwrap's staging root, so they are fine.

    This is the empirically-verified boundary of the bug: an otherwise
    identical fixture with a *relative* link launches bwrap successfully,
    so canonicalizing it would be churn with no safety benefit.
    """
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "config").write_text("x", encoding="utf-8")
    (tmp_path / "link").symlink_to(Path("target"))  # relative
    assert generator._absolute_symlink_in_chain(str(tmp_path / "link")) is None


def test_absolute_symlink_in_chain_ignores_relative_path() -> None:
    assert generator._absolute_symlink_in_chain("relative/path") is None


def test_absolute_symlink_in_chain_survives_parent_traversal(
    escaping_home: Path,
) -> None:
    """``..`` after the link must not hide it.

    ``normpath`` would collapse ``.aws/..`` textually and drop the link
    component entirely, reporting a clean chain for a path the kernel
    resolves *through* the absolute symlink.
    """
    hit = generator._absolute_symlink_in_chain(
        str(escaping_home / ".aws" / ".." / "elsewhere")
    )
    assert hit == str(escaping_home / ".aws")


def test_absolute_symlink_in_chain_tolerates_redundant_separators(
    escaping_home: Path,
) -> None:
    hit = generator._absolute_symlink_in_chain(
        f"{escaping_home}//.aws/./config"
    )
    assert hit == str(escaping_home / ".aws")


# ---------------------------------------------------------------------------
# permission rule parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule,expected",
    [
        ("Read(~/.aws/*)", ("Read", "~/.aws/*")),
        ("Edit(//abs/path)", ("Edit", "//abs/path")),
        ("Bash(git push *)", ("Bash", "git push *")),
        ("not-a-rule", None),
        ("(missing-tool)", None),
        (123, None),
    ],
)
def test_split_permission_rule(rule: object, expected: object) -> None:
    assert generator._split_permission_rule(rule) == expected


def test_permission_rule_host_path_anchored_forms(escaping_home: Path) -> None:
    assert generator._permission_rule_host_path("~/.aws/*") == str(
        escaping_home / ".aws"
    ) + "/*"
    assert generator._permission_rule_host_path("//mnt/c/x") == "/mnt/c/x"


@pytest.mark.parametrize("spec", [".env", "**/credentials*", "/project/rel"])
def test_permission_rule_host_path_unanchored_is_none(spec: str) -> None:
    """Unanchored / project-relative specs never become host paths.

    Verified against the real client: a settings file whose only deny rule
    was ``Read(**/credentials*)`` started the sandbox fine, while
    ``Read(~/.aws/*)`` alone brought it down.
    """
    assert generator._permission_rule_host_path(spec) is None


# ---------------------------------------------------------------------------
# Layer 2 canonicalization
# ---------------------------------------------------------------------------


def test_canonicalize_permission_deny_rewrites_escaping_rule(
    escaping_home: Path, tmp_path: Path
) -> None:
    deny = [
        "Bash(git push *)",
        "Read(.env)",
        "Read(**/credentials*)",
        "Read(~/.ssh/*)",
        "Read(~/.aws/*)",
    ]
    out, rewrites = generator._canonicalize_permission_deny(deny)

    external = tmp_path / "external" / ".aws"
    assert out == [
        "Bash(git push *)",
        "Read(.env)",
        "Read(**/credentials*)",
        "Read(~/.ssh/*)",
        f"Read(//{str(external).lstrip('/')}/*)",
    ]
    assert len(rewrites) == 1
    assert rewrites[0].layer == "permissions.deny"
    assert rewrites[0].original == "Read(~/.aws/*)"
    assert rewrites[0].symlink == str(escaping_home / ".aws")
    assert rewrites[0].realpath == str(external)


def test_canonicalize_permission_deny_preserves_glob_tail(
    escaping_home: Path, tmp_path: Path
) -> None:
    out, _ = generator._canonicalize_permission_deny(["Read(~/.aws/**/*.pem)"])
    external = tmp_path / "external" / ".aws"
    assert out == [f"Read(//{str(external).lstrip('/')}/**/*.pem)"]


def test_canonicalize_permission_deny_noop_without_symlink(
    escaping_home: Path,
) -> None:
    deny = ["Read(~/.ssh/*)", "Bash(rm -rf *)", "Read(**/*.pem)"]
    out, rewrites = generator._canonicalize_permission_deny(deny)
    assert out == deny
    assert rewrites == []


def test_canonicalize_permission_deny_ignores_non_path_tools(
    escaping_home: Path,
) -> None:
    """Only Read/Edit contribute paths to the sandbox deny set."""
    deny = [f"Bash(cat {escaping_home}/.aws/config)"]
    out, rewrites = generator._canonicalize_permission_deny(deny)
    assert out == deny and rewrites == []


# ---------------------------------------------------------------------------
# Layer 3 canonicalization
# ---------------------------------------------------------------------------


def test_canonicalize_sandbox_deny_rewrites_kept_entry(
    escaping_home: Path, tmp_path: Path
) -> None:
    entries = [str(escaping_home / ".aws" / "config"), "/plain/path"]
    out, rewrites = generator._canonicalize_sandbox_deny(
        entries, "sandbox.filesystem.denyRead"
    )
    external = tmp_path / "external" / ".aws"
    assert out == [str(external / "config"), "/plain/path"]
    assert len(rewrites) == 1
    assert rewrites[0].layer == "sandbox.filesystem.denyRead"


def test_canonicalize_sandbox_deny_leaves_structured_entries(
    escaping_home: Path,
) -> None:
    entries = [{"anchor": "home", "path": ".aws/**"}]
    out, rewrites = generator._canonicalize_sandbox_deny(
        entries, "sandbox.filesystem.denyRead"
    )
    assert out == entries and rewrites == []


# ---------------------------------------------------------------------------
# renderer integration
# ---------------------------------------------------------------------------


def _role_schema(deny: list) -> dict:
    return {
        "worker_roles": {
            "demo": {
                "permissions": {"deny": deny},
            },
        },
    }


def test_render_rewrites_layer2_deny_and_reports_metadata(
    escaping_home: Path, tmp_path: Path
) -> None:
    result = generator.render_role_with_metadata(
        _role_schema(["Read(~/.aws/*)", "Read(~/.ssh/*)"]),
        role="demo",
        worker_dir=str(tmp_path / "wd"),
        claude_org_path=str(tmp_path / "co"),
        wsl_detector=lambda: True,
    )
    external = tmp_path / "external" / ".aws"
    expected = f"Read(//{str(external).lstrip('/')}/*)"
    assert result.settings["permissions"]["deny"] == [expected, "Read(~/.ssh/*)"]
    assert len(result.sandbox.rewrites) == 1
    assert "symlink-canonicalized deny paths" in result.settings["$comment"]
    assert result.settings["$comment"].startswith(
        "platform=wsl, layer-3 entries suppressed: []"
    )


def test_render_leaves_clean_deny_untouched_and_emits_no_comment(
    escaping_home: Path, tmp_path: Path
) -> None:
    result = generator.render_role_with_metadata(
        _role_schema(["Read(~/.ssh/*)", "Bash(git push *)"]),
        role="demo",
        worker_dir=str(tmp_path / "wd"),
        claude_org_path=str(tmp_path / "co"),
    )
    assert result.settings["permissions"]["deny"] == [
        "Read(~/.ssh/*)",
        "Bash(git push *)",
    ]
    assert result.sandbox.rewrites == []
    assert "$comment" not in result.settings


def test_render_metadata_jsonable_includes_rewrites(
    escaping_home: Path, tmp_path: Path
) -> None:
    result = generator.render_role_with_metadata(
        _role_schema(["Read(~/.aws/*)"]),
        role="demo",
        worker_dir=str(tmp_path / "wd"),
        claude_org_path=str(tmp_path / "co"),
    )
    payload = result.sandbox.to_jsonable()
    assert payload["rewrites"][0]["original"] == "Read(~/.aws/*)"
    assert payload["rewrites"][0]["symlink"] == str(escaping_home / ".aws")
    # must stay JSON-serializable for `settings show --json`
    json.dumps(payload)


def test_settings_show_explain_surfaces_rewrites(
    escaping_home: Path, tmp_path: Path
) -> None:
    result = generator.render_role_with_metadata(
        _role_schema(["Read(~/.aws/*)"]),
        role="demo",
        worker_dir=str(tmp_path / "wd"),
        claude_org_path=str(tmp_path / "co"),
    )
    text = generator._format_show_output(
        result, "demo", explain=True, as_json=False
    )
    assert "rewrites (1):" in text
    assert "absolute symlink" in text


# ---------------------------------------------------------------------------
# sandbox doctor
# ---------------------------------------------------------------------------


def _settings_with_deny(deny: list, sandbox_deny: list | None = None) -> dict:
    return {
        "permissions": {"deny": deny},
        "sandbox": {
            "enabled": True,
            "filesystem": {"denyRead": sandbox_deny or [], "denyWrite": []},
        },
    }


def test_collect_deny_targets_spans_both_layers(escaping_home: Path) -> None:
    settings = _settings_with_deny(
        ["Read(~/.aws/*)", "Bash(git push *)", "Read(**/credentials*)"],
        [str(escaping_home / ".ssh")],
    )
    targets = sandbox_doctor.collect_deny_targets(settings)
    layers = {t.layer for t in targets}
    assert layers == {"permissions.deny", "sandbox.filesystem.denyRead"}
    # Bash rules and unanchored globs contribute no host path.
    assert len(targets) == 2


def test_collect_deny_targets_expands_tilde_in_layer3(
    escaping_home: Path,
) -> None:
    settings = _settings_with_deny([], ["~/.ssh"])
    targets = sandbox_doctor.collect_deny_targets(settings)
    assert targets[0].path == str(escaping_home / ".ssh")


def test_analyze_targets_flags_symlink_escape(
    escaping_home: Path, tmp_path: Path
) -> None:
    settings = _settings_with_deny(["Read(~/.aws/*)", "Read(~/.ssh/*)"])
    findings = sandbox_doctor.analyze_targets(
        sandbox_doctor.collect_deny_targets(settings)
    )
    bad = [f for f in findings if f.status == sandbox_doctor.STATUS_SYMLINK_ESCAPE]
    assert len(bad) == 1
    assert bad[0].source == "Read(~/.aws/*)"
    assert bad[0].suggestion == f"{tmp_path / 'external' / '.aws'}/*"


def test_diagnose_reports_not_ok_on_escape(escaping_home: Path) -> None:
    report = sandbox_doctor.diagnose(
        _settings_with_deny(["Read(~/.aws/*)"]), probe_bwrap=False
    )
    assert not report.ok
    assert len(report.failures) == 1
    assert report.canary_status == sandbox_doctor.CANARY_SKIPPED


def test_diagnose_ok_on_clean_settings(escaping_home: Path) -> None:
    report = sandbox_doctor.diagnose(
        _settings_with_deny(["Read(~/.ssh/*)"]), probe_bwrap=False
    )
    assert report.ok
    assert report.failures == []


def test_canary_failure_marks_report_not_ok(escaping_home: Path) -> None:
    """A canary failure fails the report even when static analysis is clean.

    This is the point of having a live probe: it catches unbindable deny
    paths whose cause is not a symlink.
    """

    def failing_runner(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            cmd, 1, "", "bwrap: Can't create file at /x: No such file or directory\n"
        )

    report = sandbox_doctor.diagnose(
        _settings_with_deny(["Read(~/.ssh/*)"]),
        probe_bwrap=True,
        runner=failing_runner,
    )
    assert report.failures == []
    assert report.canary_status == sandbox_doctor.CANARY_FAIL
    assert "Can't create file" in report.canary_detail
    assert not report.ok


def test_canary_pass(escaping_home: Path) -> None:
    def ok_runner(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, "", "")

    report = sandbox_doctor.diagnose(
        _settings_with_deny(["Read(~/.ssh/*)"]),
        probe_bwrap=True,
        runner=ok_runner,
    )
    assert report.canary_status == sandbox_doctor.CANARY_PASS
    assert report.ok


def test_canary_skipped_when_bwrap_missing(
    escaping_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_doctor.shutil, "which", lambda name: None)
    status, detail = sandbox_doctor.run_bwrap_canary([])
    assert status == sandbox_doctor.CANARY_SKIPPED
    assert "bwrap not found" in detail


def test_format_report_names_the_silent_fallback(escaping_home: Path) -> None:
    """The report must state that failure is silent, not just that it failed."""
    report = sandbox_doctor.diagnose(
        _settings_with_deny(["Read(~/.aws/*)"]), probe_bwrap=False
    )
    text = sandbox_doctor.format_report(report)
    assert "unsandboxed" in text
    assert "suggested rewrite:" in text


def test_format_report_ascii_only(escaping_home: Path) -> None:
    """CLI output must survive a cp932 console (see CLAUDE.md)."""
    report = sandbox_doctor.diagnose(
        _settings_with_deny(["Read(~/.aws/*)"]), probe_bwrap=False
    )
    sandbox_doctor.format_report(report, verbose=True).encode("cp932")
    sandbox_doctor.build_parser().format_help().encode("cp932")


# ---------------------------------------------------------------------------
# doctor CLI
# ---------------------------------------------------------------------------


def _write_settings(tmp_path: Path, settings: dict) -> Path:
    path = tmp_path / "settings.local.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    return path


def test_doctor_cli_exit_1_on_escape(
    escaping_home: Path, tmp_path: Path
) -> None:
    path = _write_settings(tmp_path, _settings_with_deny(["Read(~/.aws/*)"]))
    args = SimpleNamespace(
        settings=path, json=False, verbose=False, probe_bwrap=False
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = sandbox_doctor.run(args)
    assert rc == 1
    assert "FAIL" in buf.getvalue()


def test_doctor_cli_exit_0_on_clean(escaping_home: Path, tmp_path: Path) -> None:
    path = _write_settings(tmp_path, _settings_with_deny(["Read(~/.ssh/*)"]))
    args = SimpleNamespace(
        settings=path, json=False, verbose=False, probe_bwrap=False
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = sandbox_doctor.run(args)
    assert rc == 0


def test_doctor_cli_json_output(escaping_home: Path, tmp_path: Path) -> None:
    path = _write_settings(tmp_path, _settings_with_deny(["Read(~/.aws/*)"]))
    args = SimpleNamespace(
        settings=path, json=True, verbose=False, probe_bwrap=False
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = sandbox_doctor.run(args)
    payload = json.loads(buf.getvalue())
    assert rc == 1
    assert payload["ok"] is False
    assert payload["findings"][0]["status"] == sandbox_doctor.STATUS_SYMLINK_ESCAPE


def test_doctor_cli_missing_file(tmp_path: Path) -> None:
    args = SimpleNamespace(
        settings=tmp_path / "nope.json",
        json=False,
        verbose=False,
        probe_bwrap=False,
    )
    assert sandbox_doctor.run(args) == 2


def test_doctor_cli_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    args = SimpleNamespace(
        settings=path, json=False, verbose=False, probe_bwrap=False
    )
    assert sandbox_doctor.run(args) == 2


def test_doctor_cli_non_object_root(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[]", encoding="utf-8")
    args = SimpleNamespace(
        settings=path, json=False, verbose=False, probe_bwrap=False
    )
    assert sandbox_doctor.run(args) == 2


def test_unified_cli_wires_sandbox_doctor(
    escaping_home: Path, tmp_path: Path
) -> None:
    path = _write_settings(tmp_path, _settings_with_deny(["Read(~/.aws/*)"]))
    parser = runtime_cli.build_parser()
    args = parser.parse_args(
        ["sandbox", "doctor", "--settings", str(path), "--no-probe-bwrap"]
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 1
