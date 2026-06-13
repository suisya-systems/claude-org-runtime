# -*- coding: utf-8 -*-
"""queue store + journal — daemon 所有の三状態配送ライフサイクル (push 一次配送)。

設計 SoT: docs/design/broker-native-roles.md §9.3 (配送ライフサイクル) / §9.4
(delivery-scoped token) / Set D 2.3 (drain semantics の amend)。canonical 実装:
claude-org-transport-lab spike/k1_daemon.py (PR #24 merge 28a4cb2、tool-less
channel-only idle-wake が実機 PASS) の三状態モデルを、既存の broker queue store
(spike/broker.py 由来の agent_id 別 inbox) へ **加算移植** したもの。

**三状態ライフサイクル (§9.3)**: 各メッセージは 1 行 (:class:`QueueRow`) として
``UNDELIVERED -> CLAIMED(lease,owner,epoch) -> DELIVERED`` を遷移する。

- ``UNDELIVERED``: 投入済み・未配達 (``send_message`` が投入)。
- ``CLAIMED``: ある drainer (channel sidecar) がリースで占有中。``owner`` =
  delivery-scoped credential の owner、``claim_epoch`` = mode-epoch、``lease_until``
  = 期限。lease 失効 (sidecar 死亡) は :meth:`_reap_locked` が ``UNDELIVERED`` へ戻す。
- ``DELIVERED``: 配達確定 (``/confirm-delivered`` 受領)。二度と再配達しない。

**配達保証 = at-least-once + 冪等表示** (§9.3): ``DELIVERED`` は再配達しない
(confirmed 上は at-most-once)。lease reap された ``CLAIMED`` 行は再 eligible 化
(全体では at-least-once)。喪失より重複に倒す idle-wake 用途の正準選択。

**pull フォールバック (§9.3 / §9.6)**: :meth:`drain` (= ``check_messages``) は
**claim-respecting view** をドレインする — ``UNDELIVERED``-and-unclaimed (lease 失効で
reclaim 済を含む) の行のみを返して即 ``DELIVERED`` 化する。live な sidecar claim とは
二重配達せず、並行 ``check_messages`` も二重ドレインしない。single-drainer 性は
per-agent mode boolean ではなく **行レベル claim 所有権** が担保する。

並行性契約 (移植元の検証済みロジック、巻き戻さない):
- ``_lock`` は binds / rows / delivery-mode を一括ガードする単一の **非再入** Lock。
- **lock 内では I/O を行わない**。``_journal`` は自身が ``_lock`` を取るため、lock
  スコープの中から呼ぶと**自己デッドロック**する (spike は RLock + 無ロック journal
  だが本 runtime は非再入 Lock + ロック付き journal の既存契約を維持する)。よって
  :meth:`_reap_locked` 等の状態変更メソッドは **journal すべきイベントを return** し、
  呼び元が lock 解放後に :meth:`_journal` する (DELETE デッドロック回避契約と同型)。
- queue 書込先は ``state_dir / "queue.jsonl"`` (append-only JSONL journal)。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tokens import AgentBind

# ---------------------------------------------------------------- row states
UNDELIVERED = "UNDELIVERED"
CLAIMED = "CLAIMED"
DELIVERED = "DELIVERED"

# ----------------------------------------------------------- delivery modes
PUSH = "PUSH"
PULL = "PULL"


@dataclass
class QueueRow:
    """1 メッセージの配送行 (§9.3 三状態ライフサイクル)。

    ``entry`` は ``check_messages`` / channel push が運ぶ既存のワイヤ形
    (``{from_id, from_name, sent_at, message}``)。lifecycle フィールド
    (state / lease / owner / epoch) を加算して daemon 所有の配送状態を持たせる。
    """

    id: str
    to_id: str                       # 宛先 agent_id (配送解決の単位)
    entry: dict                      # 既存ワイヤ形 {from_id, from_name, sent_at, message}
    state: str = UNDELIVERED
    lease_until: float = 0.0
    owner: str | None = None         # CLAIMED 中の drainer (delivery cred の owner)
    claim_epoch: int = -1            # claim 時の mode-epoch (fencing 用)
    reclaim_count: int = 0           # lease reap で UNDELIVERED へ戻った回数
    enqueued_at: float = 0.0


class StoreMixin:
    """queue store + journal + 三状態配送ライフサイクル。

    Broker.__init__ が ``_lock`` / ``_rows`` / ``_binds`` / ``_delivery_modes`` /
    ``_epochs`` / ``state_dir`` / ``lease_seconds`` / ``reclaim_warn_threshold`` を
    確立する前提で動く。
    """

    # 型注釈のみ (実体は Broker.__init__)。mixin の自己文書化。
    _lock: threading.Lock
    _binds: dict[str, "AgentBind"]
    _rows: dict[str, QueueRow]
    _delivery_modes: dict[str, str]   # agent_id -> PUSH/PULL (既定 PUSH)
    _epochs: dict[str, int]           # agent_id -> mode-epoch (既定 0)
    state_dir: Path
    lease_seconds: float
    reclaim_warn_threshold: int

    def _journal(self, event: str, **fields) -> None:
        rec = {"ts": time.time(), "event": event, **fields}
        path = self.state_dir / "queue.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # --------------------------------------------------------- per-agent mode
    def _mode_of(self, agent_id: str) -> str:
        """agent の delivery_mode (既定 PUSH)。**caller が _lock を保持中に呼ぶ**。"""
        return self._delivery_modes.get(agent_id, PUSH)

    def _epoch_of(self, agent_id: str) -> int:
        """agent の mode-epoch (既定 0)。**caller が _lock を保持中に呼ぶ**。"""
        return self._epochs.get(agent_id, 0)

    # --------------------------------------------------------------- reaping
    def _reap_locked(self) -> list[tuple[str, int]]:
        """lease 失効した ``CLAIMED`` 行を ``UNDELIVERED`` へ戻す (sidecar 死亡回復)。

        **caller が _lock を保持中に呼ぶ**。I/O はしない (lock 内 no-I/O 契約)。
        journal すべき ``(id, reclaim_count)`` のリストを return し、呼び元が lock
        解放後に :meth:`_journal` する (非再入 Lock の自己デッドロック回避)。
        """
        now = time.time()
        reaped: list[tuple[str, int]] = []
        for row in self._rows.values():
            if row.state == CLAIMED and row.lease_until < now:
                row.state = UNDELIVERED
                row.owner = None
                row.reclaim_count += 1
                reaped.append((row.id, row.reclaim_count))
        return reaped

    def _journal_reaped(self, reaped: list[tuple[str, int]]) -> None:
        """reap 結果を lock 解放後に journal する (flapping は閾値超で印字)。"""
        for rid, reclaim in reaped:
            self._journal("lease_reaped", id=rid, reclaim=reclaim)
            if reclaim >= self.reclaim_warn_threshold:
                # §9.3 flapping/starvation 緩和: 同一行が閾値超で reclaim されたら
                # 印字する (当該行は UNDELIVERED へ戻っており pull 経路で拾われる)。
                self._journal("reclaim_threshold_exceeded", id=rid, reclaim=reclaim)

    # --------------------------------------------------------------- enqueue
    def enqueue(self, from_bind: "AgentBind", to_id: str, message: str) -> dict:
        """queue store 投入 (UNDELIVERED 行を作る) + フォールバック nudge trigger。

        帰属は token 由来 (自己申告不可)。宛先の registered 確認と行 append を
        **同一ロックスコープ**で原子的に行う (DELETE 後の登録解除済み session への
        enqueue を並行時にも防ぐ既存契約)。I/O (_journal) と PTY 注入
        (_trigger_nudge) はロック外に出し非再入 Lock の自己デッドロックを避ける。
        """
        entry = {
            "from_id": from_bind.agent_id,
            "from_name": from_bind.name,
            "sent_at": time.time(),
            "message": message,
        }
        with self._lock:
            target: "AgentBind | None" = None
            for b in self._binds.values():
                # registered な full bind のみ配送先にする (未接続 / DELETE 済み /
                # delivery-scoped credential は配送先にしない)。
                if b.revoked or not b.registered:
                    continue
                if b.agent_id == to_id or b.name == to_id:
                    target = b
                    break
            if target is None:
                return {"ok": False, "error": f"[peer_not_found] no agent '{to_id}'"}
            rid = secrets.token_hex(8)
            self._rows[rid] = QueueRow(
                id=rid, to_id=target.agent_id, entry=entry,
                enqueued_at=entry["sent_at"],
            )
        self._journal(
            "message_enqueued",
            from_id=from_bind.agent_id,
            to_id=target.agent_id,
            chars=len(message),
        )
        self._trigger_nudge(target)
        return {"ok": True, "delivered_to": target.agent_id}

    # ---------------------------------------------------------- drain (pull)
    def drain(self, bind: "AgentBind") -> list[dict]:
        """``check_messages`` 本体 = claim-respecting view のドレイン (§9.3)。

        ``UNDELIVERED``-and-unclaimed (lease 失効で reclaim 済を含む) の行のみを
        宛先順に返し、即 ``DELIVERED`` 化する。live な sidecar claim (まだ lease 中
        の ``CLAIMED``) は返さない = push と二重配達しない。両 mode で同一挙動
        (single-drainer 性は行レベル claim 所有権が担保し、mode boolean に依らない)。
        """
        with self._lock:
            reaped = self._reap_locked()
            out: list[dict] = []
            for row in self._rows.values():
                if row.state == UNDELIVERED and row.to_id == bind.agent_id:
                    row.state = DELIVERED
                    out.append(row.entry)
        self._journal_reaped(reaped)
        if out:
            self._journal("queue_drained", agent_id=bind.agent_id, count=len(out))
        return out

    # ----------------------------------------------------------- poll-claims
    def poll_claims(self, owner: str) -> dict:
        """delivery-scoped credential で owner 宛 ``UNDELIVERED`` 行を claim して返す。

        §9.3 claim-with-lease: 各行を ``CLAIMED(lease=now+T, owner, epoch=現 mode-epoch)``
        にして返す。PUSH->PULL flip 後 (mode != PUSH) は **新規 claim の発行を拒否**
        する (claim-issuance ゲート)。返す各行は ``{id, entry, epoch}``。
        """
        with self._lock:
            mode = self._mode_of(owner)
            epoch = self._epoch_of(owner)
            if mode != PUSH:
                return {"error": "push_disabled", "rows": [], "epoch": epoch}
            reaped = self._reap_locked()
            now = time.time()
            claimed: list[dict] = []
            for row in self._rows.values():
                if row.state == UNDELIVERED and row.to_id == owner:
                    row.state = CLAIMED
                    row.lease_until = now + self.lease_seconds
                    row.owner = owner
                    row.claim_epoch = epoch
                    claimed.append(
                        {"id": row.id, "entry": row.entry, "epoch": epoch}
                    )
        self._journal_reaped(reaped)
        if claimed:
            self._journal(
                "claimed", owner=owner,
                ids=[c["id"] for c in claimed], epoch=epoch,
            )
        return {"rows": claimed, "epoch": epoch}

    # ------------------------------------------------------- confirm-delivered
    def confirm_delivered(self, owner: str, rid: str, epoch: int) -> dict:
        """emit が resolve した行を ``DELIVERED`` に確定する (id で冪等、§9.3)。

        confirm は **live な claim** に紐づくことを daemon が強制する: 未 claim /
        lease reap 後 / 別 owner・別 epoch の claim は確定できない。stale epoch
        (mode flip があった) は当該行を再 eligible 化して拒否する (mode-epoch fencing)。
        """
        journal: tuple[str, dict] | None = None
        with self._lock:
            reaped = self._reap_locked()
            cur_epoch = self._epoch_of(owner)
            row = self._rows.get(rid)
            if row is None:
                result: dict = {"ok": False, "error": "unknown_row"}
            elif row.to_id != owner:
                result = {"ok": False, "error": "not_owner"}
            elif epoch != cur_epoch:
                # stale epoch (PUSH<->PULL flip があった) -> 再 eligible にして拒否。
                if row.state == CLAIMED:
                    row.state = UNDELIVERED
                    row.owner = None
                journal = ("confirm_stale_epoch",
                           {"id": rid, "row_epoch": epoch, "cur": cur_epoch})
                result = {"ok": False, "error": "stale_epoch", "epoch": cur_epoch}
            elif row.state == DELIVERED:
                result = {"ok": True, "idempotent": True}   # 冪等
            elif (row.state != CLAIMED or row.owner != owner
                    or row.claim_epoch != epoch):
                result = {"ok": False, "error": "not_claimed",
                          "state": row.state, "owner": row.owner}
            else:
                row.state = DELIVERED
                journal = ("delivered", {"id": rid, "owner": owner})
                result = {"ok": True}
        self._journal_reaped(reaped)
        if journal is not None:
            self._journal(journal[0], **journal[1])
        return result

    # -------------------------------------------------------------- mode flip
    def flip_mode(self, owner: str, mode: str) -> dict:
        """agent の delivery_mode を flip し mode-epoch を進める (§9.3 fencing)。

        flip 時に当該 agent の in-flight ``CLAIMED`` 行を ``UNDELIVERED`` へ戻す
        (原子的 flip: 旧 epoch の stale な confirm は :meth:`confirm_delivered` が
        拒否する)。``mode`` は ``PUSH`` / ``PULL`` のみ。
        """
        if mode not in (PUSH, PULL):
            return {"ok": False, "error": f"[invalid_mode] {mode!r} not in (PUSH, PULL)"}
        journal: tuple[str, dict] | None = None
        with self._lock:
            old = self._mode_of(owner)
            epoch = self._epoch_of(owner)
            if mode != old:
                self._delivery_modes[owner] = mode
                epoch += 1
                self._epochs[owner] = epoch
                for row in self._rows.values():
                    if row.state == CLAIMED and row.to_id == owner:
                        row.state = UNDELIVERED
                        row.owner = None
                journal = ("mode_flip",
                           {"owner": owner, "old": old, "new": mode, "epoch": epoch})
            result = {"ok": True, "owner": owner,
                      "mode": self._mode_of(owner), "epoch": self._epoch_of(owner)}
        if journal is not None:
            self._journal(journal[0], **journal[1])
        return result

    def reset_delivery_state(self, owner: str) -> None:
        """agent の delivery_mode / epoch を既定に戻す (切戻し §5.5 第 6 ステップ)。

        per-pane channel sidecar の reap に伴い当該 agent の配送状態をリセットする。
        in-flight ``CLAIMED`` 行は ``UNDELIVERED`` へ戻して pull 経路に委ねる
        (delivery cred の revoke は :class:`~claude_org_runtime.broker.tokens.
        TokenMixin.revoke_delivery_creds` が別途行う)。
        """
        with self._lock:
            self._delivery_modes.pop(owner, None)
            self._epochs.pop(owner, None)
            for row in self._rows.values():
                if row.state == CLAIMED and row.to_id == owner:
                    row.state = UNDELIVERED
                    row.owner = None

    # --------------------------------------------------------------- dump
    def delivery_dump(self) -> dict:
        """配送ライフサイクルの横断スナップショット (admin/診断用)。

        owner/state を晒すため admin scope に限定する想定 (§9.4 least-privilege:
        delivery-scoped cred からは到達不能)。
        """
        with self._lock:
            reaped = self._reap_locked()
            by_state: dict[str, int] = {}
            for row in self._rows.values():
                by_state[row.state] = by_state.get(row.state, 0) + 1
            snapshot = {
                "by_state": by_state,
                "modes": dict(self._delivery_modes),
                "epochs": dict(self._epochs),
                "rows": [
                    {"id": r.id, "to_id": r.to_id, "state": r.state,
                     "owner": r.owner, "reclaim": r.reclaim_count}
                    for r in self._rows.values()
                ],
            }
        self._journal_reaped(reaped)
        return snapshot

    if TYPE_CHECKING:  # server が供給する配達トリガ (型チェッカ向け宣言)
        def _trigger_nudge(self, target: "AgentBind") -> None: ...
