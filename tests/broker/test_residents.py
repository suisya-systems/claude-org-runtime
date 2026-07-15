# -*- coding: utf-8 -*-
"""broker 管理外 resident の pre-flight 検出・回収 (Issue #142) のテスト。

platform seam の規律を守る: プロセス観測 (:func:`residents._observe_process_identity`)
は ``os_name`` / ``proc_root`` / ``run`` / ``query`` を注入して分岐を決定的に検証し、
グローバル ``os.name`` / ``sys.platform`` は **patch しない** (CI #143 の教訓)。terminate /
observe / alive / kill / sleep もすべて注入する (実プロセスを kill しない)。
"""

from __future__ import annotations

import io
import json
import os
import signal
from pathlib import Path

import pytest

from claude_org_runtime.broker import residents
from claude_org_runtime.broker.residents import ProcObservation


# --------------------------------------------------------------- helpers
def _owner(sd, root_cwd, *, host="H"):
    return {
        "state_dir": os.path.realpath(sd),
        "root_cwd": os.path.realpath(root_cwd),
        "fingerprint": residents.repo_fingerprint(root_cwd, sd, host=host),
    }


def _record(sd, root_cwd, *, name="w", pid=1234, started_at=1000.0,
            exe=None, cwd=None, host="H"):
    rec = {
        "version": 1, "name": name, "pid": pid, "started_at": started_at,
        "owner": _owner(sd, root_cwd, host=host),
    }
    idn = {}
    if exe is not None:
        idn["exe"] = exe
    if cwd is not None:
        idn["cwd"] = cwd
    if idn:
        rec["identity"] = idn
    return rec


def _write(rdir: Path, name: str, obj) -> Path:
    p = rdir / f"{name}.json"
    p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")
    return p


@pytest.fixture
def resident_dir(tmp_path):
    sd = tmp_path / "broker"
    (sd / residents.RESIDENTS_DIRNAME).mkdir(parents=True)
    return sd


# ============================================================ fingerprint
def test_repo_fingerprint_stable_and_shaped():
    a = residents.repo_fingerprint("/repo", "/repo/.state", host="h")
    b = residents.repo_fingerprint("/repo", "/repo/.state", host="h")
    assert a == b
    assert a.startswith("root:") and len(a) == len("root:") + 16


def test_repo_fingerprint_sensitive_to_host_root_statedir():
    base = residents.repo_fingerprint("/repo", "/s", host="h")
    assert residents.repo_fingerprint("/repo", "/s", host="other") != base
    assert residents.repo_fingerprint("/other", "/s", host="h") != base
    assert residents.repo_fingerprint("/repo", "/s2", host="h") != base


def test_hostname_seam_used(monkeypatch):
    monkeypatch.setattr(residents, "_hostname", lambda: "FIXED")
    assert residents.repo_fingerprint("/r", "/s") == \
        residents.repo_fingerprint("/r", "/s", host="FIXED")


# ============================================================ ownership
def test_ownership_match_and_mismatch(tmp_path):
    sd, root = str(tmp_path / "sd"), str(tmp_path / "root")
    my = residents.compute_owner_identity(root, sd)
    monkey = _owner(sd, root, host=residents._hostname())
    assert residents.ownership_match(monkey, my) == "match"
    # different fingerprint (foreign host) -> mismatch
    foreign = dict(monkey, fingerprint="root:" + "0" * 16)
    assert residents.ownership_match(foreign, my) == "mismatch"
    # different state_dir -> mismatch even if fingerprint present
    other_sd = dict(monkey, state_dir=os.path.realpath(str(tmp_path / "elsewhere")))
    assert residents.ownership_match(other_sd, my) == "mismatch"
    assert residents.ownership_match("not-a-dict", my) == "mismatch"


# ============================================================ identity
def test_identity_unknown_when_unobservable():
    rec = {"started_at": 1000.0}
    assert residents.identity_match(rec, None) == "unknown"
    assert residents.identity_match(rec, ProcObservation(None, None, None)) == "unknown"


def test_identity_unknown_when_started_at_not_numeric():
    obs = ProcObservation(1000.0, None, None)
    assert residents.identity_match({"started_at": "nope"}, obs) == "unknown"
    assert residents.identity_match({"started_at": True}, obs) == "unknown"  # bool rejected


def test_identity_mismatch_on_start_time_drift():
    obs = ProcObservation(1000.0 + residents.START_TIME_TOLERANCE + 1, None, None)
    assert residents.identity_match({"started_at": 1000.0}, obs) == "mismatch"


def test_identity_match_within_tolerance():
    obs = ProcObservation(1000.0 + residents.START_TIME_TOLERANCE / 2, None, None)
    assert residents.identity_match({"started_at": 1000.0}, obs) == "match"


def test_identity_exe_demotes_only_when_both_present_and_differ(tmp_path):
    a = tmp_path / "a"; a.write_text("x")
    b = tmp_path / "b"; b.write_text("x")
    obs = ProcObservation(1000.0, str(b), None)
    rec = {"started_at": 1000.0, "identity": {"exe": str(a)}}
    assert residents.identity_match(rec, obs) == "mismatch"
    # obs.exe absent -> benign
    assert residents.identity_match(rec, ProcObservation(1000.0, None, None)) == "match"


def test_identity_strips_deleted_suffix(tmp_path):
    exe = tmp_path / "python"; exe.write_text("x")
    obs = ProcObservation(1000.0, f"{exe} (deleted)", None)
    rec = {"started_at": 1000.0, "identity": {"exe": str(exe)}}
    assert residents.identity_match(rec, obs) == "match"


def test_identity_cwd_none_is_benign_windows():
    # Windows obs.cwd is None -> cwd check skipped even if record has cwd.
    obs = ProcObservation(1000.0, None, None)
    rec = {"started_at": 1000.0, "identity": {"cwd": "/somewhere"}}
    assert residents.identity_match(rec, obs) == "match"


# ============================================= observe: linux (fake /proc)
def _fake_proc(proc_root: Path, pid: int, *, starttime_ticks: int, btime: int,
               comm="(py thon)", exe=None, cwd=None):
    (proc_root / str(pid)).mkdir(parents=True)
    # Real /proc/<pid>/stat: "pid (comm) state ppid ...". comm is wrapped in the FIRST
    # '(' and LAST ')' and may contain spaces/parens; observe splits AFTER the last ')'.
    # Fields after comm start at field 3 (state); starttime is field 22 => post[19].
    post = ["0"] * 30
    post[0] = "S"                      # field 3 = state
    post[19] = str(starttime_ticks)    # field 22 = starttime
    line = f"{pid} {comm} " + " ".join(post)
    (proc_root / str(pid) / "stat").write_text(line, encoding="utf-8")
    (proc_root / "stat").write_text(f"cpu 1 2 3\nbtime {btime}\nprocesses 9\n",
                                    encoding="utf-8")
    if exe:
        os.symlink(exe, proc_root / str(pid) / "exe")
    if cwd:
        os.symlink(cwd, proc_root / str(pid) / "cwd")


def test_observe_linux_computes_start_time(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    monkeypatch.setattr(residents, "_clock_ticks", lambda: 100)
    # comm with an inner space AND inner parens, wrapped by the first '(' / last ')'.
    _fake_proc(proc, 4242, starttime_ticks=500, btime=1_000_000,
               comm="(py (th on))")
    obs = residents._observe_process_identity(4242, os_name="linux",
                                              proc_root=str(proc))
    assert obs is not None
    # btime + starttime/ticks = 1_000_000 + 500/100 = 1_000_005.0
    assert obs.start_time == pytest.approx(1_000_005.0)


def test_observe_linux_missing_pid_returns_none(tmp_path):
    proc = tmp_path / "proc"; proc.mkdir()
    assert residents._observe_process_identity(999999, os_name="linux",
                                               proc_root=str(proc)) is None


def test_observe_linux_reads_exe_cwd(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    real_exe = tmp_path / "python3"; real_exe.write_text("x")
    real_cwd = tmp_path / "work"; real_cwd.mkdir()
    monkeypatch.setattr(residents, "_clock_ticks", lambda: 100)
    _fake_proc(proc, 7, starttime_ticks=0, btime=10, exe=str(real_exe),
               cwd=str(real_cwd))
    obs = residents._observe_process_identity(7, os_name="linux", proc_root=str(proc))
    assert obs.exe == str(real_exe) and obs.cwd == str(real_cwd)


# ============================================= observe: darwin (inject run)
class _Proc:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_observe_darwin_parses_lstart(monkeypatch):
    def fake_run(argv, **kw):
        assert kw["env"]["LC_ALL"] == "C"  # locale pinned
        return _Proc(0, "Wed Jul 16 12:00:00 2025 /usr/bin/python3\n")
    obs = residents._observe_darwin(123, run=fake_run)
    assert obs is not None and obs.start_time is not None
    assert obs.exe == "/usr/bin/python3"


def test_observe_darwin_nonzero_exit_is_none():
    obs = residents._observe_darwin(1, run=lambda *a, **k: _Proc(1, ""))
    assert obs is None


# ============================================= observe: windows (inject query)
def test_observe_windows_converts_filetime():
    # FILETIME (100ns ticks since 1601) for Unix epoch 1609459200 (2021-01-01T00:00Z):
    #   ft = (epoch + 11644473600) * 1e7
    epoch = 1609459200.0
    ft = int((epoch + 11644473600.0) * 1e7)
    obs = residents._observe_windows(55, query=lambda pid: (ft, "C:\\py.exe"))
    assert obs.exe == "C:\\py.exe"
    assert obs.start_time == pytest.approx(epoch, abs=1.0)


def test_observe_windows_dead_pid_none():
    assert residents._observe_windows(55, query=lambda pid: None) is None


def test_observe_unknown_os_is_none():
    assert residents._observe_process_identity(1, os_name="plan9") is None


# ============================================= terminate seam
def test_terminate_posix_sigterm_clean(monkeypatch):
    killed = []
    # alive True once (before signal check inside _wait_dead uses first call), then dead.
    states = iter([False])  # _wait_dead first checks alive(pid); False => already gone
    r = residents._terminate_process(
        321, os_name="linux", expected_start_time=None,
        observe=lambda *a, **k: ProcObservation(1.0, None, None),
        alive=lambda p: next(states, False),
        kill=lambda p, s: killed.append((p, s)), sleep=lambda *_: None,
    )
    assert r == "terminated"
    assert killed == [(321, signal.SIGTERM)]


def test_terminate_posix_escalates_to_sigkill():
    killed = []
    r = residents._terminate_process(
        321, os_name="linux", expected_start_time=None,
        observe=lambda *a, **k: ProcObservation(1.0, None, None),
        alive=lambda p: True,  # never dies -> SIGTERM, grace, SIGKILL, still alive
        kill=lambda p, s: killed.append(s), sleep=lambda *_: None, grace=0.1,
    )
    assert signal.SIGTERM in killed and signal.SIGKILL in killed
    assert r == "still_alive"


def test_terminate_toctou_recycled_aborts_kill():
    killed = []
    # re-observed start time drifts far from expected -> recycled, no kill.
    r = residents._terminate_process(
        321, os_name="linux", expected_start_time=1000.0,
        observe=lambda *a, **k: ProcObservation(9999.0, None, None),
        alive=lambda p: True, kill=lambda p, s: killed.append(s),
        sleep=lambda *_: None,
    )
    assert r == "recycled" and killed == []


def test_terminate_gone_when_observe_none():
    r = residents._terminate_process(
        321, os_name="linux", expected_start_time=1000.0,
        observe=lambda *a, **k: None, alive=lambda p: True,
        kill=lambda p, s: pytest.fail("must not kill"), sleep=lambda *_: None,
    )
    assert r == "gone"


def test_terminate_windows_guidance_when_still_alive():
    runs = []
    r = residents._terminate_process(
        321, os_name="windows", expected_start_time=None,
        observe=lambda *a, **k: ProcObservation(1.0, None, None),
        alive=lambda p: True, run=lambda argv, **k: runs.append(argv),
        sleep=lambda *_: None, grace=0.1,
    )
    assert runs and runs[0][0] == "taskkill" and "/F" not in runs[0]  # graceful only
    assert r == "guidance"


# ============================================= classify matrix
@pytest.mark.parametrize("load,own,alive,ident,expected", [
    ("malformed", "match", True, "match", residents.ROW_MALFORMED),
    ("unknown_version", "match", True, "match", residents.ROW_UNKNOWN_VERSION),
    ("ok", "match", False, "unknown", residents.ROW_STALE_OWNED),
    ("ok", "mismatch", False, "unknown", residents.ROW_STALE_FOREIGN),
    ("ok", "match", True, "match", residents.ROW_LIVE_REAPABLE),
    ("ok", "match", True, "mismatch", residents.ROW_RECYCLED),
    ("ok", "match", True, "unknown", residents.ROW_UNVERIFIABLE),
    ("ok", "mismatch", True, "match", residents.ROW_FOREIGN_LIVE),
])
def test_classify_row(load, own, alive, ident, expected):
    assert residents._classify_row(load, own, alive, ident) == expected


# ============================================= load_record / version policy
def test_load_record_version_before_schema(resident_dir):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    # version 99 with an OTHERWISE schema-invalid body must be 'unknown_version',
    # not 'malformed' (version checked BEFORE jsonschema).
    p = _write(rdir, "future", {"version": 99})
    state, rec, ver = residents._load_record(p)
    assert state == "unknown_version" and ver == 99


def test_load_record_malformed_json(resident_dir):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    p = _write(rdir, "bad", "{not json")
    assert residents._load_record(p)[0] == "malformed"


def test_load_record_missing_required_is_malformed(resident_dir):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    p = _write(rdir, "partial", {"version": 1, "name": "x"})  # missing pid/started_at/owner
    assert residents._load_record(p)[0] == "malformed"


def test_load_record_ok(resident_dir):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    p = _write(rdir, "w", _record(str(resident_dir), str(resident_dir)))
    assert residents._load_record(p)[0] == "ok"


# ============================================= delete TOCTOU
def test_delete_registration_skips_when_content_changed(resident_dir):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    p = _write(rdir, "w", {"pid": 5, "started_at": 1.0})
    # classified with pid=5/started_at=1.0 but file now holds a NEW live registration.
    _write(rdir, "w", {"pid": 6, "started_at": 2.0})
    assert residents._delete_registration(p, 5, 1.0) is False
    assert p.exists()


def test_delete_registration_removes_when_unchanged(resident_dir):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    p = _write(rdir, "w", {"pid": 5, "started_at": 1.0})
    assert residents._delete_registration(p, 5, 1.0) is True
    assert not p.exists()


def test_delete_registration_skips_when_reread_torn_or_gone(resident_dir):
    """再読込が破損/非dict/消滅なら削除しない (fail-closed; codex P2)。"""
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    # torn / malformed re-read -> must NOT unlink.
    torn = _write(rdir, "torn", "{partial")
    assert residents._delete_registration(torn, 5, 1.0) is False
    assert torn.exists()
    # non-dict JSON -> must NOT unlink.
    arr = _write(rdir, "arr", [1, 2, 3])
    assert residents._delete_registration(arr, 5, 1.0) is False
    assert arr.exists()
    # already gone -> returns False (nothing deleted), no crash.
    assert residents._delete_registration(rdir / "missing.json", 5, 1.0) is False


# ============================================= preflight_residents (sweep)
def test_preflight_empty_dir_is_silent(tmp_path):
    buf = io.StringIO()
    rep = residents.preflight_residents(str(tmp_path), str(tmp_path), stream=buf)
    assert buf.getvalue() == "" and rep.scanned == 0


def test_preflight_default_announces_does_not_touch(resident_dir, monkeypatch):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    root = str(resident_dir)
    monkeypatch.setattr(residents, "_hostname", lambda: "H")
    _write(rdir, "live", _record(str(resident_dir), root, name="live", pid=10,
                                 started_at=1000.0))
    buf = io.StringIO()
    rep = residents.preflight_residents(
        str(resident_dir), root, reap=False, stream=buf, os_name="linux",
        alive=lambda p: True,
        observe=lambda p, **k: ProcObservation(1000.0, None, None),
    )
    out = buf.getvalue()
    assert "LIVE, broker-unmanaged" in out and "--reap" in out
    assert rep.rows == ["live_reapable"]
    assert (rdir / "live.json").exists()  # default never deletes


def test_preflight_reap_terminates_owned_live_and_deletes(resident_dir, monkeypatch):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    root = str(resident_dir)
    monkeypatch.setattr(residents, "_hostname", lambda: "H")
    _write(rdir, "live", _record(str(resident_dir), root, name="live", pid=10,
                                 started_at=1000.0))
    calls = []
    buf = io.StringIO()
    rep = residents.preflight_residents(
        str(resident_dir), root, reap=True, stream=buf, os_name="linux",
        alive=lambda p: True,
        observe=lambda p, **k: ProcObservation(1000.0, None, None),
        terminate=lambda pid, **k: calls.append(pid) or "terminated",
    )
    assert calls == [10] and rep.terminated == 1 and rep.deleted == 1
    assert not (rdir / "live.json").exists()


def test_preflight_reap_message_honest_when_delete_fails(resident_dir, monkeypatch):
    """terminate 成功後に登録簿が差し替わって削除できなかった場合、"removed" と誤報せず
    "left in place" と述べる (codex P3)。プロセス停止の事実は別に述べる。"""
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    root = str(resident_dir)
    monkeypatch.setattr(residents, "_hostname", lambda: "H")
    p = _write(rdir, "live", _record(str(resident_dir), root, name="live", pid=10,
                                     started_at=1000.0))

    def term_then_swap(pid, **k):
        # terminate 中に登録側が同名で live 登録を差し替えた状況を模す。
        _write(rdir, "live", _record(str(resident_dir), root, name="live", pid=99,
                                     started_at=2000.0))
        return "terminated"

    buf = io.StringIO()
    rep = residents.preflight_residents(
        str(resident_dir), root, reap=True, stream=buf, os_name="linux",
        alive=lambda p: True,
        observe=lambda p, **k: ProcObservation(1000.0, None, None),
        terminate=term_then_swap,
    )
    out = buf.getvalue()
    assert "left in place" in out and "registration removed" not in out
    assert rep.deleted == 0 and p.exists()             # 差し替わった登録は残す


def test_preflight_reap_leaves_foreign_and_unknown(resident_dir, monkeypatch):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    root = str(resident_dir)
    monkeypatch.setattr(residents, "_hostname", lambda: "H")
    foreign = _record(str(resident_dir), root, name="foreign", pid=11)
    foreign["owner"]["fingerprint"] = "root:" + "0" * 16
    _write(rdir, "foreign", foreign)
    _write(rdir, "future", {"version": 99, "name": "future", "pid": 1,
                            "started_at": 1.0, "owner": foreign["owner"]})
    _write(rdir, "bad", "{broken")
    buf = io.StringIO()
    rep = residents.preflight_residents(
        str(resident_dir), root, reap=True, stream=buf, os_name="linux",
        alive=lambda p: True,
        terminate=lambda *a, **k: pytest.fail("must not terminate foreign/unknown"),
    )
    assert (rdir / "foreign.json").exists()
    assert (rdir / "future.json").exists()
    assert (rdir / "bad.json").exists()
    assert rep.foreign == 1 and rep.unknown_version == 1 and rep.malformed == 1


def test_preflight_reap_deletes_stale_owned_dead(resident_dir, monkeypatch):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    root = str(resident_dir)
    monkeypatch.setattr(residents, "_hostname", lambda: "H")
    _write(rdir, "dead", _record(str(resident_dir), root, name="dead", pid=12))
    buf = io.StringIO()
    rep = residents.preflight_residents(
        str(resident_dir), root, reap=True, stream=buf, os_name="linux",
        alive=lambda p: False,  # dead
        terminate=lambda *a, **k: pytest.fail("must not terminate a dead pid"),
    )
    assert not (rdir / "dead.json").exists() and rep.deleted == 1
    assert rep.rows == ["stale_owned"]


def test_preflight_recycled_deletes_not_kills(resident_dir, monkeypatch):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    root = str(resident_dir)
    monkeypatch.setattr(residents, "_hostname", lambda: "H")
    _write(rdir, "recy", _record(str(resident_dir), root, name="recy", pid=13,
                                 started_at=1000.0))
    buf = io.StringIO()
    rep = residents.preflight_residents(
        str(resident_dir), root, reap=True, stream=buf, os_name="linux",
        alive=lambda p: True,
        # observed start time drifts -> identity mismatch -> recycled row.
        observe=lambda p, **k: ProcObservation(9999.0, None, None),
        terminate=lambda *a, **k: pytest.fail("recycled must not be killed"),
    )
    assert rep.rows == ["recycled"] and not (rdir / "recy.json").exists()


def test_preflight_unverifiable_skipped_under_reap(resident_dir, monkeypatch):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    root = str(resident_dir)
    monkeypatch.setattr(residents, "_hostname", lambda: "H")
    _write(rdir, "unv", _record(str(resident_dir), root, name="unv", pid=14,
                                started_at=1000.0))
    buf = io.StringIO()
    rep = residents.preflight_residents(
        str(resident_dir), root, reap=True, stream=buf, os_name="darwin",
        alive=lambda p: True,
        observe=lambda p, **k: None,  # unobservable -> identity unknown
        terminate=lambda *a, **k: pytest.fail("unverifiable must not be killed"),
    )
    assert rep.rows == ["unverifiable"] and rep.skipped == 1
    assert (rdir / "unv.json").exists()
    assert "stop manually" in buf.getvalue()


def test_current_os_returns_known_token():
    assert residents._current_os() in {"linux", "darwin", "windows"}


def test_preflight_output_is_ascii(resident_dir, monkeypatch):
    rdir = resident_dir / residents.RESIDENTS_DIRNAME
    root = str(resident_dir)
    monkeypatch.setattr(residents, "_hostname", lambda: "H")
    _write(rdir, "live", _record(str(resident_dir), root, name="live", pid=10,
                                 started_at=1000.0))
    _write(rdir, "bad", "{broken")
    buf = io.StringIO()
    residents.preflight_residents(
        str(resident_dir), root, reap=False, stream=buf, os_name="linux",
        alive=lambda p: True,
        observe=lambda p, **k: ProcObservation(1000.0, None, None),
    )
    buf.getvalue().encode("ascii")  # raises if any non-ASCII leaked into announces
