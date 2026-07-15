# -*- coding: utf-8 -*-
"""broker 管理外 (unmanaged) resident プロセスの pre-flight 検出・告知・回収 (Issue #142)。

org up / org down は broker daemon の制御面 (sidecar / admin RPC) を扱うが、daemon
の**外**で常駐するプロセス (例: secretary の queue watcher / attention watcher) は
daemon のライフサイクルから独立して生き残る。これらが前世代のクラッシュ後に取り残
されると、次回の org up が気付けない。本モジュールは ``<state-dir>/residents/*.json``
の **登録簿** を走査し、告知する (既定) か、ownership と identity の**両方**が照合で
きたものだけを回収 (``--reap``) する。

契約 (docs/broker-residents-registry-contract.md が正本):

- **登録簿は利用側 (claude-org) が書く**。本 runtime は **走査・照合・告知・回収のみ**
  で、レコードを書かない (回収時に自分が所有する stale レコードを削除するだけ)。利用側
  の登録実装は別 Issue のフォローアップ (本 PR ではスコープ外)。
- **登録済み resident のみが対象**。未登録・登録以前の世代は pre-flight から不可視
  (契約 constraint 1: issue の期待値調整)。
- **identity 照合に cmdline 完全一致を使わない** (constraint 2)。cmdline は表示専用。
  照合の主軸は **kernel プロセス開始時刻** (``started_at``) で PID 再利用を防ぎ、
  exe/cwd は「両側にあって食い違うときだけ」降格させる補助材料。
- **ownership は state_dir 単独でなく repo-root 指紋を含む** (constraint 3)。指紋は
  ``repo_fingerprint(root_cwd, state_dir)`` = ``sha256(hostname \\0 realpath(root_cwd)
  \\0 realpath(state_dir))``。state_dir パス再利用時の誤回収を防ぐ。
- **schema は version 必須。未知 version は kill せず告知のみ** (constraint 5)。stale
  レコードの削除は ``--reap`` 時のみ (通常 up/down は告知だけ)。

**stdlib のみ** (psutil 非依存)。プロセス identity の観測はクロスプラットフォームの
platform seam (:func:`_observe_process_identity`) 経由で、Linux は ``/proc``、macOS は
``ps``、Windows は ctypes。**観測できないプラットフォームは fail-closed** (identity
'unknown' = 決して kill しない)。分岐点はすべて seam 関数か注入引数に切り出し、テストは
グローバル ``os.name`` / ``sys.platform`` を patch しない (CI #143 の教訓)。
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from . import sidecar

RESIDENTS_DIRNAME = "residents"

# runtime が理解する登録簿 schema version。未知は kill/delete せず告知のみ (constraint 5)。
SUPPORTED_RESIDENT_VERSIONS = frozenset({1})

# kernel 開始時刻の許容誤差 (秒)。macOS ``ps lstart`` の 1 秒解像度と、登録側の kernel-
# start 記録タイミングの微差を吸収する。狭すぎると under-reap (安全側) に倒れる。契約は
# 登録側に **kernel 開始時刻の verbatim 記録** を課すので、Linux/Windows では実質 0 差。
START_TIME_TOLERANCE = 2.0

# graceful stop 後に escalate / guidance へ移るまでの待ち (秒)。
REAP_GRACE = 5.0

_POLL_INTERVAL = 0.05


# ===========================================================================
# platform seam (テストは _current_os / observe / terminate を注入する)
# ===========================================================================

def _current_os() -> str:
    """実行 OS を ``'windows'`` / ``'darwin'`` / ``'linux'`` に正規化する platform seam。

    launcher._current_os の意図的な複製 (~6 行)。residents を launcher から独立して
    テスト可能に保ち循環 import を避けるための重複 (open question に明記)。テストは
    ``residents._current_os`` **だけ**を差し替え、グローバル os/sys は触らない (CI #143)。
    """
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _hostname() -> str:
    """ownership 指紋に使うマシン識別子。**bind アドレス (daemon.json.host) ではない**。

    seam として切り出し、テストが決定的な値を注入できるようにする。登録側もこの
    ``repo_fingerprint`` を import して同じ ``socket.gethostname()`` を使う契約 (指紋の
    host にバインドアドレス ``127.0.0.1`` を混ぜると永久 under-reap になる — red-team Major)。
    """
    return socket.gethostname()


@dataclass(frozen=True)
class ProcObservation:
    """live プロセスから観測できた identity。観測できない項目は ``None`` (fail-closed)。"""
    start_time: float | None   # kernel 開始時刻 (epoch 秒)。identity 照合の主軸。
    exe: str | None            # 正規化前の実行ファイルパス (補助材料)。
    cwd: str | None            # プロセス cwd (補助材料。Windows では常に None)。


def _readlink(path: str) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _clock_ticks() -> int:
    """``SC_CLK_TCK`` (Linux の jiffies/秒)。取得不能時は 100 (Linux の慣例値)。"""
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return 100
    return ticks if isinstance(ticks, int) and ticks > 0 else 100


def _boot_time_linux(proc_root: str) -> float | None:
    """``/proc/stat`` の ``btime`` (システム起動時刻 epoch 秒)。"""
    try:
        with open(f"{proc_root}/stat", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(int(line.split()[1]))
    except (OSError, ValueError, IndexError):
        return None
    return None


def _observe_linux(pid: int, proc_root: str) -> ProcObservation | None:
    """Linux/WSL: ``/proc/<pid>/stat`` の starttime + ``/proc/stat`` btime で kernel
    開始時刻を復元し、``exe`` / ``cwd`` を readlink する。pid dir 不在は None。

    ``comm`` (field 2) は空白や括弧を含みうるので **最後の ``)``** で切って以降を空白
    split する (starttime は全体で field 22 = state から数えて index 19)。
    """
    base = f"{proc_root}/{pid}"
    try:
        with open(f"{base}/stat", "r", encoding="utf-8", errors="replace") as f:
            stat = f.read()
    except OSError:
        return None
    rparen = stat.rfind(")")
    if rparen == -1:
        return None
    rest = stat[rparen + 2:].split()  # rest[0] = field3 (state); field N -> rest[N-3]
    try:
        starttime_ticks = int(rest[19])  # field 22 = starttime
    except (IndexError, ValueError):
        return None
    btime = _boot_time_linux(proc_root)
    ticks = _clock_ticks()
    start_time: float | None = None
    if btime is not None and ticks:
        start_time = btime + starttime_ticks / ticks
    return ProcObservation(
        start_time=start_time,
        exe=_readlink(f"{base}/exe"),
        cwd=_readlink(f"{base}/cwd"),
    )


def _observe_darwin(pid: int, *, run=subprocess.run) -> ProcObservation | None:
    """macOS: ``ps -o lstart=,comm=`` で開始時刻 (1 秒解像度) と実行パスを得る。

    ``lstart`` は曜日/月名を含むので ``LC_ALL=C`` / ``LANG=C`` を強制し英語 ASCII に固定
    する (非英語 LC_TIME だと strptime が失敗し identity 'unknown' に黙って落ちる —
    red-team Minor)。``argv`` / ``cwd`` は観測しない (None)。
    """
    try:
        proc = run(
            ["ps", "-o", "lstart=", "-o", "comm=", "-p", str(pid)],
            capture_output=True, text=True,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, ValueError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    out = (getattr(proc, "stdout", "") or "").strip()
    if not out:
        return None
    parts = out.split()
    if len(parts) < 5:
        return None
    lstart = " ".join(parts[:5])  # 'Wed Jul 16 12:34:56 2024'
    comm = " ".join(parts[5:]) or None
    start_time: float | None
    try:
        start_time = time.mktime(time.strptime(lstart, "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        start_time = None
    return ProcObservation(start_time=start_time, exe=comm, cwd=None)


def _win_query(pid: int):
    """Windows ctypes 境界: ``(creation_filetime_100ns | None, image_path | None)`` か None。

    **この関数だけ**が ``WinDLL`` に触れる。:func:`_observe_windows` は本関数を注入可能な
    ``query=`` として受け、FILETIME->epoch 変換とパス整形は純関数として単体テストする
    (非 Windows ランナーで ``WinDLL('kernel32')`` を踏まないため — red-team Minor)。
    """
    try:  # pragma: no cover - Windows 専用の ctypes shim
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None  # 87 => 該当 pid 無し (dead)。それ以外でも観測不能 => None (fail-closed)。
        try:
            creation = wintypes.FILETIME()
            exit_ = wintypes.FILETIME()
            kernel_ = wintypes.FILETIME()
            user_ = wintypes.FILETIME()
            ft_100ns = None
            if kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_),
                ctypes.byref(kernel_), ctypes.byref(user_),
            ):
                ft_100ns = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            image = None
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                image = buf.value or None
            return (ft_100ns, image)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 観測失敗は None (fail-closed で決して kill しない)
        return None


def _observe_windows(pid: int, *, query=_win_query) -> ProcObservation | None:
    """Windows: creation FILETIME (100ns 単位, 1601 epoch) を Unix epoch へ変換する。

    ctypes は ``query`` に隔離し、本関数の変換ロジックは canned 値で単体テストできる。
    """
    raw = query(pid)
    if raw is None:
        return None
    ft_100ns, image = raw
    start_time: float | None = None
    if isinstance(ft_100ns, int) and ft_100ns > 0:
        # FILETIME epoch (1601-01-01) から Unix epoch (1970-01-01) へ: 11644473600 秒。
        start_time = ft_100ns / 1e7 - 11644473600.0
    return ProcObservation(start_time=start_time, exe=image, cwd=None)


def _observe_process_identity(
    pid: int, *, os_name: str, proc_root: str = "/proc",
) -> ProcObservation | None:
    """live pid の identity を観測する platform seam。観測不能は ``None`` (fail-closed)。

    **登録側 (out of scope) はこの関数を import し、登録時に
    ``residents._observe_process_identity(os.getpid(), os_name=...).start_time`` を
    ``started_at`` に verbatim 記録する契約** (runtime が後で同一 float を読むので照合差は
    決定的に 0。red-team Major の対策)。
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os_name == "linux":
        return _observe_linux(pid, proc_root)
    if os_name == "darwin":
        return _observe_darwin(pid)
    if os_name == "windows":
        return _observe_windows(pid)
    return None


# ===========================================================================
# ownership / identity 述語
# ===========================================================================

def repo_fingerprint(root_cwd: str, state_dir: str, *, host: str | None = None) -> str:
    """ownership 指紋 (単一の正本)。登録側もこれを import して同じ値を書く契約。

    ``sha256(hostname \\0 realpath(root_cwd) \\0 realpath(state_dir))`` の先頭 16 hex に
    ``root:`` を前置。host は既定で :func:`_hostname` (= ``socket.gethostname()``)。
    state_dir パスが別の org に再利用されても、host / root_cwd が違えば指紋が食い違い
    誤回収を防ぐ (constraint 3)。
    """
    host = host if host is not None else _hostname()
    canon = "\0".join([
        host,
        os.path.normcase(os.path.realpath(root_cwd)),
        os.path.normcase(os.path.realpath(state_dir)),
    ])
    return "root:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def compute_owner_identity(root_cwd: str, state_dir: str) -> dict:
    """この org 実行の ownership 同一性 (レコードの ``owner`` と照合する側)。"""
    return {
        "state_dir": os.path.normcase(
            os.path.realpath(sidecar.absolutize(state_dir))
        ),
        "fingerprint": repo_fingerprint(root_cwd, state_dir),
    }


def ownership_match(owner: dict, my_owner: dict) -> str:
    """``'match'`` / ``'mismatch'``。指紋は host+root_cwd+state_dir を内包するので、
    別 checkout / 別ホスト / 別 state_dir が同じパスを再利用しても mismatch になる。"""
    if not isinstance(owner, dict):
        return "mismatch"
    rec_sd = os.path.normcase(os.path.realpath(owner.get("state_dir", "") or ""))
    if rec_sd != my_owner["state_dir"]:
        return "mismatch"
    return "match" if owner.get("fingerprint") == my_owner["fingerprint"] else "mismatch"


def identity_match(record: dict, obs: ProcObservation | None) -> str:
    """``'match'`` / ``'mismatch'`` / ``'unknown'``。

    主軸は kernel 開始時刻。観測不能 (obs None / start_time None) や登録側 started_at が
    非数値なら ``'unknown'`` = fail-closed (kill 対象にしない)。開始時刻が許容外なら PID
    再利用とみなし ``'mismatch'``。exe/cwd は**両側にあって食い違うときだけ** mismatch に
    降格させる補助材料。cmdline は照合に一切使わない (constraint 2)。
    """
    if obs is None or obs.start_time is None:
        return "unknown"
    reg = record.get("started_at")
    if not isinstance(reg, (int, float)) or isinstance(reg, bool):
        return "unknown"
    if abs(obs.start_time - reg) > START_TIME_TOLERANCE:
        return "mismatch"  # PID 再利用 (別プロセスが後から起動)
    idn = record.get("identity") or {}
    exe = idn.get("exe")
    if exe and obs.exe:
        obs_exe = os.path.normcase(os.path.realpath(obs.exe.removesuffix(" (deleted)")))
        if obs_exe != os.path.normcase(os.path.realpath(exe)):
            return "mismatch"
    cwd = idn.get("cwd")
    if cwd and obs.cwd:
        if os.path.realpath(obs.cwd) != os.path.realpath(cwd):
            return "mismatch"
    return "match"


# ===========================================================================
# 登録簿 schema (jsonschema draft 2020-12)
# ===========================================================================

RESIDENT_SCHEMA_V1 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "claude-org broker resident registration (v1)",
    "type": "object",
    "required": ["version", "name", "pid", "started_at", "owner"],
    "additionalProperties": True,
    "properties": {
        "version": {"type": "integer"},
        "name": {"type": "string", "minLength": 1},
        "pid": {"type": "integer", "minimum": 1},
        "started_at": {"type": "number"},
        "owner": {
            "type": "object",
            "required": ["state_dir", "root_cwd", "fingerprint"],
            "additionalProperties": True,
            "properties": {
                "state_dir": {"type": "string", "minLength": 1},
                "root_cwd": {"type": "string", "minLength": 1},
                "fingerprint": {"type": "string", "pattern": "^root:[0-9a-f]{16}$"},
                "instance_id": {"type": ["string", "null"]},
            },
        },
        "identity": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "exe": {"type": ["string", "null"]},
                "cwd": {"type": ["string", "null"]},
                "token": {"type": ["string", "null"]},
            },
        },
        "cmdline": {"type": ["array", "string", "null"]},
        "role": {"type": ["string", "null"]},
        "registered_at": {"type": ["number", "null"]},
    },
}


# レコードの分類 (decision matrix の行)。
ROW_UNKNOWN_VERSION = "unknown_version"  # row0
ROW_MALFORMED = "malformed"              # row0b
ROW_STALE_OWNED = "stale_owned"          # row1
ROW_STALE_FOREIGN = "stale_foreign"      # row2
ROW_LIVE_REAPABLE = "live_reapable"      # row3
ROW_RECYCLED = "recycled"                # row4
ROW_UNVERIFIABLE = "unverifiable"        # row5
ROW_FOREIGN_LIVE = "foreign_live"        # row6


def _load_record(path: Path):
    """``(state, record|None, version|None)`` を返す。version は jsonschema **より前**に
    判定する (未知 version の未来レコードを 'malformed' と誤標識しない — red-team Minor)。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ("malformed", None, None)
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return ("malformed", None, None)
    if not isinstance(record, dict):
        return ("malformed", None, None)
    version = record.get("version")
    if version not in SUPPORTED_RESIDENT_VERSIONS:
        return ("unknown_version", record, version)
    try:
        jsonschema.validate(record, RESIDENT_SCHEMA_V1)
    except jsonschema.ValidationError:
        return ("malformed", record, version)
    return ("ok", record, version)


def _classify_row(load_state: str, ownership: str, alive: bool, identity: str) -> str:
    """decision matrix。TERMINATE は
    ``known & alive & ownership==match & identity==match`` の 1 行だけで到達可能。"""
    if load_state == "malformed":
        return ROW_MALFORMED
    if load_state == "unknown_version":
        return ROW_UNKNOWN_VERSION
    if not alive:
        return ROW_STALE_OWNED if ownership == "match" else ROW_STALE_FOREIGN
    if ownership != "match":
        return ROW_FOREIGN_LIVE
    if identity == "match":
        return ROW_LIVE_REAPABLE
    if identity == "mismatch":
        return ROW_RECYCLED
    return ROW_UNVERIFIABLE


# ===========================================================================
# 回収 (terminate) seam
# ===========================================================================

def _wait_dead(pid: int, grace: float, alive, sleep) -> bool:
    """graceful stop 後に pid が消えるまで poll する。poll 回数で上限を切るので、注入
    ``sleep`` を no-op にすればテストは wall-clock に依存せず決定的に速い。"""
    if not alive(pid):
        return True
    polls = max(1, int(grace / _POLL_INTERVAL))
    for _ in range(polls):
        sleep(_POLL_INTERVAL)
        if not alive(pid):
            return True
    return False


def _terminate_process(
    pid: int, *, os_name: str, expected_start_time: float | None,
    grace: float = REAP_GRACE, proc_root: str = "/proc",
    observe=_observe_process_identity, alive=sidecar.pid_alive,
    kill=os.kill, run=subprocess.run, sleep=time.sleep,
) -> str:
    """所有かつ identity 一致した live resident を停止する。**Windows は SIGTERM の直訳を
    しない** (constraint)。

    返り値: ``'terminated'`` / ``'escalated'`` (SIGKILL 昇格) / ``'still_alive'`` /
    ``'guidance'`` (Windows: 手動 taskkill /F へ委ねる) / ``'recycled'`` (kill 直前の再観測
    で開始時刻がずれた = PID 再利用 → kill しない) / ``'gone'`` (既に消えていた)。

    **TOCTOU**: scan と kill の間に pid が死んで再利用される窓を閉じるため、signal の直前に
    開始時刻を再観測し、ずれていたら ``'recycled'`` を返して kill を中止する。
    """
    if expected_start_time is not None:
        obs = observe(pid, os_name=os_name, proc_root=proc_root)
        if obs is None:
            return "gone"
        if obs.start_time is None or abs(obs.start_time - expected_start_time) > START_TIME_TOLERANCE:
            return "recycled"

    if os_name == "windows":
        try:
            run(["taskkill", "/PID", str(pid)], capture_output=True, text=True)
        except (OSError, ValueError):
            pass
        return "terminated" if _wait_dead(pid, grace, alive, sleep) else "guidance"

    # POSIX: SIGTERM -> grace -> SIGKILL。
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    except OSError:
        return "still_alive"
    if _wait_dead(pid, grace, alive, sleep):
        return "terminated"
    try:
        kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except OSError:
        return "still_alive"
    return "escalated" if _wait_dead(pid, grace, alive, sleep) else "still_alive"


def _delete_registration(path: Path, expected_pid, expected_started_at) -> bool:
    """stale レコードを削除する。**削除直前に再読込**し、pid + started_at が分類時と同一の
    ときだけ unlink する (scan と unlink の間に登録側が同名で live 登録を差し替える窓を
    閉じる — red-team Minor)。内容が変わっていれば削除しない。"""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        record = None
    if isinstance(record, dict):
        if record.get("pid") != expected_pid or record.get("started_at") != expected_started_at:
            return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ===========================================================================
# sweep entry point
# ===========================================================================

@dataclass
class ResidentSweepReport:
    scanned: int = 0
    announced: int = 0
    terminated: int = 0
    deleted: int = 0
    skipped: int = 0
    foreign: int = 0
    unknown_version: int = 0
    malformed: int = 0
    rows: list = field(default_factory=list)  # 各レコードの row (テスト assert 用)


def _manual_stop_cmd(pid, os_name: str) -> str:
    if os_name == "windows":
        return f"taskkill /F /PID {pid}"
    return f"kill -TERM {pid}"


def preflight_residents(
    state_dir: str, root_cwd: str | None, *, reap: bool = False,
    prefix: str = "org up", os_name: str | None = None, stream=sys.stderr,
    observe=_observe_process_identity, alive=sidecar.pid_alive,
    terminate=_terminate_process,
) -> ResidentSweepReport:
    """``<state-dir>/residents/*.json`` を走査し、告知 (既定) か回収 (``reap``) する。

    登録簿 dir が不在 / 空なら **何も出力しない** (Phase 1 は登録側未実装 = 常に空。毎回の
    ノイズを避ける)。ownership==match のレコードだけを触る (削除/kill)。foreign は生死を
    問わず一切触らない (別 org の登録簿を消して復帰と競合しないため)。戻り値はテスト用で、
    呼び元は無視してよい。
    """
    os_name = os_name or _current_os()
    report = ResidentSweepReport()
    residents_dir = Path(state_dir) / RESIDENTS_DIRNAME
    try:
        paths = sorted(residents_dir.glob("*.json"))
    except OSError:
        paths = []
    if not paths:
        return report

    my_owner = compute_owner_identity(root_cwd or os.getcwd(), state_dir)
    print(
        f"{prefix}: pre-flight: {len(paths)} broker-unmanaged resident(s) "
        f"under {residents_dir}:",
        file=stream,
    )

    for path in paths:
        report.scanned += 1
        load_state, record, version = _load_record(path)
        record = record if isinstance(record, dict) else {}
        name = record.get("name") or path.stem
        pid = record.get("pid")

        ownership = "mismatch"
        is_alive = False
        identity = "unknown"
        if load_state == "ok":
            ownership = ownership_match(record.get("owner") or {}, my_owner)
            is_alive = bool(alive(pid)) if isinstance(pid, int) and pid > 0 else False
            if is_alive and ownership == "match":
                obs = observe(pid, os_name=os_name)
                identity = identity_match(record, obs)

        row = _classify_row(load_state, ownership, is_alive, identity)
        report.rows.append(row)
        report.announced += 1
        _handle_row(
            row, path=path, name=name, pid=pid, version=version, os_name=os_name,
            record=record, reap=reap, prefix=prefix, stream=stream,
            terminate=terminate, report=report,
        )
    return report


def _emit(stream, prefix, msg) -> None:
    print(f"{prefix}: {msg}", file=stream)


def _handle_row(
    row, *, path, name, pid, version, os_name, record, reap, prefix, stream,
    terminate, report: ResidentSweepReport,
) -> None:
    """1 レコードの告知 (既定) / 行動 (reap)。文字列は ASCII のみ (cp932 コンソール安全)。"""
    started_at = record.get("started_at")

    if row == ROW_UNKNOWN_VERSION:
        report.unknown_version += 1
        if reap:
            _emit(stream, prefix, f"reap: skipped name={name} (unknown schema version {version}; not touching).")
        else:
            _emit(stream, prefix, f"pre-flight:   name={name} unknown schema version {version}; ignoring (not touching).")
        return

    if row == ROW_MALFORMED:
        report.malformed += 1
        if reap:
            _emit(stream, prefix, f"reap: skipped {path.name} (malformed; not touching).")
        else:
            _emit(stream, prefix, f"pre-flight:   {path.name} malformed registration; ignoring (not touching).")
        return

    if row == ROW_STALE_FOREIGN:
        report.foreign += 1
        _emit(stream, prefix, f"pre-flight:   name={name} pid={pid} STALE and owned by a different org instance; leaving it alone.")
        return

    if row == ROW_FOREIGN_LIVE:
        report.foreign += 1
        if reap:
            _emit(stream, prefix, f"reap: skipped name={name} pid={pid} (owned by a different org instance).")
        else:
            _emit(stream, prefix, f"pre-flight:   name={name} pid={pid} owned by a different org instance; leaving it alone.")
        return

    if row == ROW_UNVERIFIABLE:
        if reap:
            report.skipped += 1
            _emit(stream, prefix, f"reap: skipped name={name} pid={pid} (identity unverifiable on {os_name}); stop manually: {_manual_stop_cmd(pid, os_name)}")
        else:
            _emit(stream, prefix, f"pre-flight:   name={name} pid={pid} identity could not be verified on this platform ({os_name}); will not be reaped.")
        return

    if row == ROW_STALE_OWNED:
        if reap:
            if _delete_registration(path, pid, started_at):
                report.deleted += 1
                _emit(stream, prefix, f"reap: removed stale registration name={name} pid={pid} (process not running).")
            else:
                _emit(stream, prefix, f"reap: registration name={name} changed during sweep; left in place.")
        else:
            _emit(stream, prefix, f"pre-flight:   name={name} pid={pid} registration STALE (process not running); rerun with --reap to remove it.")
        return

    if row == ROW_RECYCLED:
        if reap:
            if _delete_registration(path, pid, started_at):
                report.deleted += 1
            _emit(stream, prefix, f"reap: removed stale registration name={name} pid={pid} (PID recycled by an unrelated process; not killed).")
        else:
            _emit(stream, prefix, f"pre-flight:   name={name} pid={pid} PID reused by a different process (identity mismatch); registration STALE.")
        return

    if row == ROW_LIVE_REAPABLE:
        if not reap:
            _emit(stream, prefix, f"pre-flight:   name={name} pid={pid} LIVE, broker-unmanaged (owner+identity verified); rerun with --reap to terminate.")
            return
        result = terminate(
            pid, os_name=os_name,
            expected_start_time=started_at if isinstance(started_at, (int, float)) else None,
        )
        if result in ("terminated", "escalated", "recycled", "gone"):
            deleted = _delete_registration(path, pid, started_at)
            if deleted:
                report.deleted += 1
            if result == "terminated":
                report.terminated += 1
                _emit(stream, prefix, f"reap: terminated name={name} pid={pid} (SIGTERM); registration removed.")
            elif result == "escalated":
                report.terminated += 1
                _emit(stream, prefix, f"reap: name={name} pid={pid} did not exit after SIGTERM; escalated to SIGKILL; registration removed.")
            elif result == "recycled":
                _emit(stream, prefix, f"reap: name={name} pid={pid} exited during reap and the PID was recycled; not killed; registration removed.")
            else:  # gone
                _emit(stream, prefix, f"reap: name={name} pid={pid} exited before termination; registration removed.")
        elif result == "guidance":
            report.skipped += 1
            _emit(stream, prefix, f"reap: name={name} pid={pid} matched but auto-reap is not performed on windows; stop manually: taskkill /F /PID {pid}")
        else:  # still_alive
            report.skipped += 1
            _emit(stream, prefix, f"reap: name={name} pid={pid} still alive after SIGKILL; leaving registration; stop manually: kill -KILL {pid}")
        return
