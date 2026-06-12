# -*- coding: utf-8 -*-
"""daemon sidecar 契約 + journal オフセットスライス (broker 制御面の土台)。

設計 SoT: runtime#63 org up/down launcher の事前 Codex design review
(tmp/codex-review-runtime-broker-control-plane.md)。本モジュールは org up/down
(タスク 2) が薄い wrapper として実装できるよう、走行中 daemon の **発見可能な
メタデータ** と **journal の run スライス** を提供する。

二つの sidecar ファイルを ``<state-dir>/`` に置く:

- ``daemon.json`` — 発見用メタデータ (pid / host / port / state_dir(絶対) /
  backend / started_at / journal_offset)。**秘密を含まない**。停止時に削除する。
- ``admin.token`` — admin HTTP RPC (token mint / shutdown) の認証 token。
  **秘密**なので 0600 で書き、daemon.json とは別ファイルにする (平文 journal 禁止・
  発見用メタと秘密を混ぜない。Codex review Blocker/Major 対応)。停止時に削除する。

``journal_offset`` は run 開始時点の ``queue.jsonl`` のバイト長。down (タスク 2) は
このオフセット以降のスライス**のみ**を見て ``broker_stopped`` を確認する。全履歴
grep は過去 run の残留で偽陽性になる (Codex review Major) ため、append-only journal の
オフセット判定 (既知の正準) で当該 run のイベントだけを切り出す。

パスは入口で絶対化する (Windows ``isabs`` の罠を避けるため ``posixpath.isabs`` を
併用する。#61 修正の先例)。
"""

from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path

SIDECAR_NAME = "daemon.json"
ADMIN_TOKEN_NAME = "admin.token"
JOURNAL_NAME = "queue.jsonl"


def is_absolute(path: str) -> bool:
    """posix / native (Windows) の双方で absolute 判定する (#61 の先例)。

    本コードベースの canonical なパス表記は posix 形 (``/repo`` 等) で、Windows
    daemon (ntpath) は drive letter の無い ``/repo`` を absolute と見なさない。
    posix 判定を併用しないと posix-absolute を relative と誤認する。
    """
    return posixpath.isabs(path) or os.path.isabs(path)


def absolutize(path: str | os.PathLike[str]) -> str:
    """sidecar 入口でパスを絶対化する。absolute は as-is、relative は daemon の
    起動 cwd 基準で絶対化する (黙って相対のまま記録しない)。"""
    s = os.fspath(path)
    if is_absolute(s):
        return s
    return os.path.abspath(s)


def journal_offset(state_dir: str | os.PathLike[str]) -> int:
    """run 開始時点の ``queue.jsonl`` のバイト長を返す (down の run スライス起点)。

    ファイル未作成 (初回 run) は 0。journal は常に行末 ``\\n`` で完結する
    append-only なので、返るオフセットは必ず行境界 = 有効なバイト境界になる
    (:func:`read_journal_since` の binary seek が UTF-8 multibyte を割らない前提)。
    """
    p = Path(state_dir) / JOURNAL_NAME
    try:
        return p.stat().st_size
    except FileNotFoundError:
        return 0


def write_sidecar(
    state_dir: str | os.PathLike[str],
    *,
    pid: int,
    host: str,
    port: int,
    backend: str | None,
    started_at: float,
    journal_offset: int,
) -> Path:
    """daemon.json を atomic に書く (発見用メタデータ)。秘密は含めない。

    ``backend`` は **解決済み** backend 名 (``--backend`` 省略時は
    ``default_backend()`` の結果)。``--no-nudge`` で adapter を持たない場合は
    ``None`` (= terminal backend 無し)。健全性判定 (タスク 2) が「同 backend」を
    照合できるよう、要求値 (``args.backend`` の ``None``) ではなく実値を記録する。
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "host": host,
        "port": port,
        "state_dir": absolutize(state_dir),
        "backend": backend,
        "started_at": started_at,
        "journal_offset": journal_offset,
    }
    path = state_dir / SIDECAR_NAME
    tmp = state_dir / (SIDECAR_NAME + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # 同一 dir 内 rename = atomic publish (部分書きを晒さない)
    return path


def write_admin_token(state_dir: str | os.PathLike[str], token: str) -> Path:
    """admin.token を 0600 で書く (admin RPC の認証 token)。

    ``O_CREAT`` 時に mode 0600 を渡し、既存ファイルにも best-effort で chmod する。
    **既知制限 (Windows)**: NTFS では POSIX パーミッションが効かず、Python は
    read-only ビットのみ反映する (group/other read を本当には落とせない)。
    localhost-only daemon の前提と、token を平文 journal に出さない方針で補う。
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / ADMIN_TOKEN_NAME
    tmp = state_dir / (ADMIN_TOKEN_NAME + ".tmp")
    # 0600 の temp に書いてから atomic rename で公開する。in-place の O_TRUNC 更新だと
    # 起動監視側が書込途中に空文字列/部分書きを拾い、直後の /admin が 401 になる
    # フレークを生む (daemon.json と同じ atomic publish に揃える。Codex review Major)。
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
    finally:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)  # 同一 dir 内 rename = atomic publish (torn read 回避)
    try:
        os.chmod(path, 0o600)  # rename 後の最終ファイルにも best-effort で確実化
    except OSError:
        pass
    return path


def read_sidecar(state_dir: str | os.PathLike[str]) -> dict | None:
    """daemon.json を読む (無い / 壊れていれば None)。"""
    path = Path(state_dir) / SIDECAR_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_admin_token(state_dir: str | os.PathLike[str]) -> str | None:
    """admin.token を読む (無い / 空なら None)。

    空文字列も None 扱いにする (atomic publish 前の理論上の torn read や、外部が
    truncate したファイルを「公開済み token」と誤認しないため)。
    """
    path = Path(state_dir) / ADMIN_TOKEN_NAME
    try:
        tok = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return tok or None


def remove_sidecar(state_dir: str | os.PathLike[str]) -> None:
    """daemon.json と admin.token を削除する (停止時のクリーンアップ。冪等)。"""
    for name in (SIDECAR_NAME, ADMIN_TOKEN_NAME):
        try:
            (Path(state_dir) / name).unlink()
        except FileNotFoundError:
            pass


def read_journal_since(
    state_dir: str | os.PathLike[str], offset: int
) -> list[dict]:
    """``queue.jsonl`` を ``offset`` バイト以降だけ読み、当該 run のイベントを返す。

    offset は行境界 (= 有効なバイト境界) なので binary seek + UTF-8 decode で
    multibyte を割らない。壊れた行は読み飛ばす (best-effort)。全履歴 grep の
    偽陽性 (過去 run の ``broker_stopped`` 残留) を構造的に避けるのが目的。
    """
    p = Path(state_dir) / JOURNAL_NAME
    try:
        with p.open("rb") as f:
            f.seek(max(0, offset))
            data = f.read().decode("utf-8")
    except FileNotFoundError:
        return []
    out: list[dict] = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
