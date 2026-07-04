# -*- coding: utf-8 -*-
"""queue store + journal — daemon 所有の三状態配送ライフサイクル (push 一次配送)。

設計 SoT: docs/design/broker-native-roles.md §9.3 (配送ライフサイクル) / §9.4
(delivery-scoped token) / Set D 2.3 (drain semantics の amend)。現行 canonical は
本モジュール。歴史的 origin:
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
    claim_generation: int = -1       # claim 時の delivery generation (session fencing 用)
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
    # session-scoped delivery fencing (Issue #125)。owner -> 現世代 / 現世代 instance。
    _delivery_generations: dict[str, int]   # owner -> current delivery generation (既定 0)
    _delivery_instances: dict[str, str]     # owner -> current-generation sidecar instance id
    # duplicate-claimer 検知: owner -> {instance_id: last poll ts} と emit cooldown。
    _delivery_poll_seen: dict[str, dict[str, float]]
    _duplicate_emit_at: dict[tuple[str, str, str], float]  # (owner, iA, iB) -> last emit ts
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

    def _generation_of(self, owner: str) -> int:
        """owner の delivery generation (既定 0 = 未登録)。**_lock 保持中に呼ぶ**。

        session-scoped fencing (Issue #125): channel sidecar は起動時に
        :meth:`register_delivery_instance` で generation を +1 し自分を現世代に登録
        する。0 は「まだどの sidecar も register していない」= claim 不可を表す。
        """
        return self._delivery_generations.get(owner, 0)

    def _note_poll_locked(
        self, owner: str, instance_id: str, now: float
    ) -> list[tuple[str, dict]]:
        """poll した sidecar instance を記録し duplicate claimer を検知する。

        **_lock 保持中に呼ぶ** (I/O はしない)。lease window 内に owner へ複数の
        distinct instance が poll したら duplicate とみなし、``duplicate_sidecar_detected``
        の journal イベントタプルを return する (呼び元が lock 解放後に journal)。
        毎 poll のスパムを避けるため instance pair ごと cooldown (= lease window) を
        置く (Codex review Minor #10)。stale 世代の poll も記録する: fence で claim は
        拒否されても「二重 sidecar が生きている」運用シグナルは残す (Major #5)。
        """
        window = self.lease_seconds
        # emit cooldown / seen map を lease window で prune (無制限成長を防ぐ)。
        for k in [k for k, ts in self._duplicate_emit_at.items() if now - ts > window]:
            del self._duplicate_emit_at[k]
        seen = self._delivery_poll_seen.setdefault(owner, {})
        for iid in [i for i, ts in seen.items() if now - ts > window]:
            del seen[iid]
        others = [i for i in seen if i != instance_id]
        seen[instance_id] = now
        journal: list[tuple[str, dict]] = []
        for other in others:
            lo, hi = sorted((instance_id, other))
            key = (owner, lo, hi)
            last = self._duplicate_emit_at.get(key, 0.0)
            if now - last > window:
                self._duplicate_emit_at[key] = now
                journal.append((
                    "duplicate_sidecar_detected",
                    {"owner": owner, "instances": [lo, hi]},
                ))
        return journal

    def _delivery_owner_locked(self, token: str) -> str | None:
        """delivery cred token を owner へ解決し **liveness を検証** する。

        **_lock 保持中に呼ぶ**。revoked / 非 delivery scope / 未知 token は None。
        これを claim/confirm の row mutation と **同一 _lock スコープ** で行うことで、
        delivery cred の revoke (close_pane の revoke_delivery_creds が _lock 下で
        ``revoked=True`` にする) を claim 発行に対する **原子的な fence** にする
        (Codex review Major: get_bind の一度きり検査では revoke 後に in-flight request
        が遅延再開すると owner だけで claim でき、revoke が fence にならない TOCTOU)。
        """
        bind = self._binds.get(token)
        if bind is None or bind.revoked or bind.scope != "delivery":
            return None
        return bind.agent_id

    def _owner_registered_locked(self, owner: str) -> bool:
        """owner に live (registered) な full bind があるか。**_lock 保持中に呼ぶ**。

        push 配送は **live session にのみ** emit する。MCP initialize 前 / do_DELETE 後の
        owner には claim を発行しないことで、死にかけ session へ emit->confirm して
        ``DELIVERED``-but-lost にする配送喪失窓を閉じる (§9.3 claim-issuance ゲートの
        precondition)。enqueue の「registered な宛先にのみ」と同じ live 判定。
        """
        for b in self._binds.values():
            if (b.agent_id == owner and b.scope == "full"
                    and b.registered and not b.revoked):
                return True
        return False

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
        # NOTE: 行の可視化 (上の lock 内) と message_enqueued の journal はこの順 (lock
        # 解放後に journal) が **非再入 Lock + 自己ロック _journal の契約上必須** (lock 内
        # で _journal すると自己デッドロック)。そのため並行 poll_claims が行を claim して
        # "claimed" を先に journal しうる = audit log 上で claimed が enqueue を追い越す
        # 順序窓が開く。これは **診断専用で良性**: journal の唯一の consumer は
        # broker_started/broker_stopped のオフセットスライス (launcher) のみで、_rows は
        # in-memory・journal replay で再構築しない (crash recovery なし)。将来 journal
        # replay で状態再構築を入れる場合は順序保証を別途設計すること。
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

    # ------------------------------------------------------ delivery register
    def register_delivery_instance(self, token: str, instance_id: str) -> dict:
        """channel sidecar instance を登録し owner の delivery generation を +1 する。

        session-scoped fencing (Issue #125): session fork/resume で **同一 delivery
        cred** を持つ sidecar が二重に生きうる (cred は replay で同一なので token だけ
        では新旧を識別できない — Codex review Blocker #1)。sidecar は起動時に本 endpoint
        を叩き、daemon は owner の generation を単調 +1 して呼び手の ``instance_id`` を
        現世代の claimer として記録する。以後の :meth:`poll_claims` /
        :meth:`confirm_delivered` は **現世代のみ** 許可し、旧世代 (fork 元 / 古い session)
        の sidecar を fence する。

        ``token`` は delivery cred で owner を **_lock 下で**解決する (revoke fence と
        同型)。旧世代の in-flight ``CLAIMED`` 行は ``UNDELIVERED`` へ即差し戻す
        (:meth:`flip_mode` の原子的 flip と同型 — lease 失効を待たず新 sidecar / pull で
        再配達させる。Codex review Blocker #3)。register 応答で generation を返し、
        sidecar はこれを以後の poll/confirm に載せる。
        """
        journal: tuple[str, dict] | None = None
        owner: str | None = None
        with self._lock:
            owner = self._delivery_owner_locked(token)
            if owner is None:
                return {"ok": False, "error": "unauthorized"}
            gen = self._generation_of(owner) + 1
            self._delivery_generations[owner] = gen
            self._delivery_instances[owner] = instance_id
            # 旧世代の CLAIMED 行を差し戻す (新 generation != claim_generation の全て)。
            for row in self._rows.values():
                if (row.state == CLAIMED and row.to_id == owner
                        and row.claim_generation != gen):
                    row.state = UNDELIVERED
                    row.owner = None
            journal = ("delivery_generation_registered",
                       {"owner": owner, "generation": gen, "instance": instance_id})
        if journal is not None:
            self._journal(journal[0], **journal[1])
        return {"ok": True, "owner": owner, "generation": gen,
                "instance_id": instance_id}

    # ----------------------------------------------------------- poll-claims
    def poll_claims(self, token: str, generation: int, instance_id: str) -> dict:
        """delivery-scoped credential で owner 宛 ``UNDELIVERED`` 行を claim して返す。

        ``token`` は **delivery cred** で、owner は token から **_lock 下で**解決+検証
        する (revoke を claim 発行に対する原子的 fence にする。Codex review Major)。
        ``generation`` / ``instance_id`` は :meth:`register_delivery_instance` の応答で
        得た session-scoped fencing 値で、**現世代のみ** claim を許可する (旧 session /
        fork 元の sidecar は ``stale_sidecar`` で拒否 — Issue #125)。§9.3 claim-with-lease:
        各行を ``CLAIMED(lease=now+T, owner, epoch=現 mode-epoch, generation)`` にして
        返す。PUSH->PULL flip 後 (mode != PUSH) は **新規 claim の発行を拒否** する。
        返す各行は ``{id, entry, epoch}``。
        """
        reaped: list[tuple[str, int]] = []
        dup_journal: list[tuple[str, dict]] = []
        claimed: list[dict] = []
        claimed_epoch = 0
        owner: str | None = None
        with self._lock:
            owner = self._delivery_owner_locked(token)
            if owner is None:
                return {"error": "unauthorized", "rows": []}
            now = time.time()
            # 記録 + duplicate 検知は fence 判定より前に行う (stale 世代の poll でも
            # 「二重 sidecar が生きている」シグナルを残す — Major #5 / #10)。
            dup_journal = self._note_poll_locked(owner, instance_id, now)
            cur_gen = self._generation_of(owner)
            if cur_gen == 0 or generation != cur_gen:
                # 未登録 (cur_gen==0) / 旧世代の sidecar。claim を発行しない (fence)。
                result: dict = {"error": "stale_sidecar", "rows": [],
                                "generation": cur_gen}
            else:
                mode = self._mode_of(owner)
                epoch = self._epoch_of(owner)
                if mode != PUSH:
                    result = {"error": "push_disabled", "rows": [], "epoch": epoch}
                elif not self._owner_registered_locked(owner):
                    # 受信側 session が live でない (initialize 前 / do_DELETE 後)。claim を
                    # 発行せず行を UNDELIVERED のまま残す: re-initialize で registered に
                    # 戻れば次 poll で claim され、check_messages も同行を拾える。死にかけ
                    # session への emit->confirm 喪失窓を閉じる (Codex Major)。
                    result = {"error": "owner_unregistered", "rows": [], "epoch": epoch}
                else:
                    reaped = self._reap_locked()
                    for row in self._rows.values():
                        if row.state == UNDELIVERED and row.to_id == owner:
                            row.state = CLAIMED
                            row.lease_until = now + self.lease_seconds
                            row.owner = owner
                            row.claim_epoch = epoch
                            row.claim_generation = generation
                            claimed.append(
                                {"id": row.id, "entry": row.entry, "epoch": epoch}
                            )
                    claimed_epoch = epoch
                    result = {"rows": claimed, "epoch": epoch}
        self._journal_reaped(reaped)
        for ev, fields in dup_journal:
            self._journal(ev, **fields)
        if claimed:
            self._journal(
                "claimed", owner=owner,
                ids=[c["id"] for c in claimed], epoch=claimed_epoch,
            )
        return result

    # ------------------------------------------------------- confirm-delivered
    def confirm_delivered(
        self, token: str, rid: str, epoch: int, generation: int, instance_id: str
    ) -> dict:
        """emit が resolve した行を ``DELIVERED`` に確定する (id で冪等、§9.3)。

        ``token`` は **delivery cred** で、owner は token から **_lock 下で**解決+検証
        する (revoke を confirm に対する原子的 fence にする。Codex review Major)。
        confirm は **live な claim** に紐づくことを daemon が強制する: 未 claim /
        lease reap 後 / 別 owner・別 epoch・別 generation の claim は確定できない。
        stale generation (旧 session / fork 元の sidecar) は当該 claim を再 eligible 化
        して ``stale_sidecar`` で拒否する (session fencing — 旧 sidecar が register 前に
        claim した行を後から confirm できないようにする。Codex review Blocker #2)。
        stale epoch (mode flip) は従来どおり mode-epoch fencing で拒否する。
        """
        journal: tuple[str, dict] | None = None
        with self._lock:
            owner = self._delivery_owner_locked(token)
            if owner is None:
                return {"ok": False, "error": "unauthorized"}
            reaped = self._reap_locked()
            cur_epoch = self._epoch_of(owner)
            cur_gen = self._generation_of(owner)
            row = self._rows.get(rid)
            if row is None:
                result: dict = {"ok": False, "error": "unknown_row"}
            elif row.to_id != owner:
                result = {"ok": False, "error": "not_owner"}
            elif cur_gen == 0 or generation != cur_gen:
                # stale sidecar (superseded by newer generation / 未登録)。拒否し、
                # **この stale sidecar 自身の live claim だけ** を再 eligible 化する
                # (現世代 sidecar の claim は claim_generation で守られ剥がれない)。
                # register 側の差し戻しと二重でも冪等 (既に UNDELIVERED なら no-op)。
                if (row.state == CLAIMED and row.owner == owner
                        and row.claim_generation == generation):
                    row.state = UNDELIVERED
                    row.owner = None
                journal = ("confirm_stale_sidecar",
                           {"id": rid, "row_generation": generation, "cur": cur_gen})
                result = {"ok": False, "error": "stale_sidecar", "generation": cur_gen}
            elif epoch != cur_epoch:
                # stale epoch (PUSH<->PULL flip があった) -> 拒否。再 eligible 化は
                # **この stale confirm に対応する claim だけ** に限る: 行が既に新しい
                # epoch で再 claim されている (claim_epoch != epoch) 場合に剥がすと、
                # 現 sidecar の live claim を壊して不要な再配送を誘発する (Codex review
                # Major)。owner / claim_epoch が stale confirm と一致する CLAIMED 行のみ
                # UNDELIVERED へ戻す (= 古い claim だけを fence する)。
                if (row.state == CLAIMED and row.owner == owner
                        and row.claim_epoch == epoch):
                    row.state = UNDELIVERED
                    row.owner = None
                journal = ("confirm_stale_epoch",
                           {"id": rid, "row_epoch": epoch, "cur": cur_epoch})
                result = {"ok": False, "error": "stale_epoch", "epoch": cur_epoch}
            elif row.state == DELIVERED:
                result = {"ok": True, "idempotent": True}   # 冪等
            elif (row.state != CLAIMED or row.owner != owner
                    or row.claim_epoch != epoch
                    or row.claim_generation != generation):
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

    def discard_agent_rows(self, owner: str) -> int:
        """owner 宛の全 queue 行を破棄する (pane close = agent 死亡時の queue purge)。

        切戻し §5.5 (5)「.state/broker の未読・bind が残らないこと」の row 版。pane が
        閉じると当該 bind は revoke されるが、revoked bind は uniqueness 判定から
        除外されるため同じ ``agent_id``/``name`` を **再利用** して再 spawn できる。その
        とき未配達のまま残った旧セッション宛の行を新しい同名 agent が drain/claim すると
        **クロスセッションの誤配送**になる (Codex review Major)。close 時に owner 宛の行を
        全削除してこの leak を閉じる。破棄件数を返す。

        **do_DELETE (session close) では呼ばない**: あちらは bind を revoke せず
        ``registered=False`` にするだけで、同一 agent が後で re-initialize して自分の
        queue を読み続ける正規ケース (= 行は本人のもの。purge は誤り)。
        """
        with self._lock:
            doomed = [rid for rid, r in self._rows.items() if r.to_id == owner]
            for rid in doomed:
                del self._rows[rid]
        if doomed:
            self._journal("agent_rows_discarded", owner=owner, count=len(doomed))
        return len(doomed)

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
            # session-scoped fencing state も落とす (Issue #125 Major #8): 残ると同名
            # respawn 後に誤 fence (旧 generation を継承) / 誤 duplicate 検知になる。
            self._delivery_generations.pop(owner, None)
            self._delivery_instances.pop(owner, None)
            self._delivery_poll_seen.pop(owner, None)
            for k in [k for k in self._duplicate_emit_at if k[0] == owner]:
                del self._duplicate_emit_at[k]
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
                # session-scoped fencing 診断 (Issue #125 Minor #9): owner ごとの
                # 現世代と active instance を出す (二重 sidecar / stale fence の切り分け)。
                "generations": dict(self._delivery_generations),
                "instances": dict(self._delivery_instances),
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
