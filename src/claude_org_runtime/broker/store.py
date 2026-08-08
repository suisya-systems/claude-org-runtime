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
import re
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

# ------------------------------------------- delivery register refusal codes
# :meth:`StoreMixin.register_delivery_instance` が generation bump を拒否するときの
# コード。**latch するか否か**が意味の中心で、sidecar 側の挙動を決める (Issue #169):
#
# - ``REFUSE_BG_HOSTED`` / ``REFUSE_SUPERSEDED``: **latching**。状態は当該プロセスの
#   生涯にわたり覆らないので、sidecar は claim loop を畳んで沈黙する。
# - ``REFUSE_OBSERVER_PENDING``: **non-latching**。「まだ正統ではない」だけで、
#   daemon 側の状態 (現職 lease の失効 / 将来の明示 adopt) が変われば覆りうる。
#   sidecar は poll cadence で register を再試行する。
#
# 拒否は generation を bump しないので、再試行が現職と generation を ping-pong する
# ことはない (再試行が通るのは現職が heartbeat を止めて lease が失効した時だけ)。
#
# ``REFUSE_SUPERSEDED`` が既存の ``"unobserved"`` 文字列を保持しているのは意図的:
# 旧 sidecar はこの 1 語だけを latch 対象として知っており、latch させたい側に
# 割り当てておけば version skew でも安全側に落ちる (未知コードは旧 sidecar から見て
# 「不明な失敗」= 再試行、これは新コード側に与えたい挙動と一致する)。
REFUSE_BG_HOSTED = "suppressed_bg_hosted"
REFUSE_SUPERSEDED = "unobserved"
REFUSE_OBSERVER_PENDING = "observer_pending"
# poll 側の fence (register は通ったが、その後で世代交代された sidecar)。register の
# 拒否ではないが **観測上は同じ「claim していない sidecar」** なので stand-down 面に
# 載せる。sidecar はこれで latch せず、静かに poll し続ける (再 register は generation
# war になるためしない)。
REFUSE_STALE_SIDECAR = "stale_sidecar"
# stand-down 記録の owner あたり上限 (instance ごとに 1 枠持つため上限を置く)。
_STANDDOWN_MAX_PER_OWNER = 8
# sidecar に恒久 stand-down を指示するコード (channel_sidecar._LATCHING_REFUSALS と
# 対応する。両者の一致は tests/broker/test_channel_sidecar.py が固定する)。
LATCHING_REFUSALS = (REFUSE_BG_HOSTED, REFUSE_SUPERSEDED)

# ``ORG_BROKER_CHANNEL_OBSERVER`` への代入形を捉える (``-e K=V`` / ``env K=V`` /
# JSON ``"K": "V"``)。adapter が起動失敗時に引数列を例外文へ載せる回り込みを
# :meth:`StoreMixin.scrub_secrets` で伏せるため。値の文字集合は
# ``secrets.token_urlsafe`` の ``[A-Za-z0-9_-]``。
_OBSERVER_ASSIGN_RE = re.compile(
    r'(ORG_BROKER_CHANNEL_OBSERVER["\']?\s*[=:]\s*)(["\']?)[A-Za-z0-9_\-]+'
)


@dataclass
class ObserverLease:
    """observed live session を delivery generation に束ねる lease (Issue #129 問題 A)。

    human-facing launcher (``org up``) が起動する observed session だけが delivery
    generation を bump し ``/claim-owner`` できるようにするための **非 replay 秘密**。
    launcher はこの ``secret`` を **mcp-config ではなく子プロセス env** に注入する
    (:meth:`~claude_org_runtime.broker.store.StoreMixin.assert_observer`)。fork/resume は
    mcp-config (delivery cred 込み) を verbatim replay するが process env の秘密は
    継承しないため lease を提示できず、:meth:`register_delivery_instance` が generation
    bump を拒否する (fork による observed session の takeover を断つ)。

    **脅威モデル (過大評価しないこと)**: この lease が防ぐのは **意図しない verbatim
    replay** (fork / resume が persisted mcp-config を再生して original を fence する)
    だけである。同一 uid の敵対プロセスに対する防御ではない:

    - ``--mcp-config`` は inline JSON で argv に載るため、full token と delivery cred は
      元々 ``ps`` から読める (docs/channel-delivery-model-decision.md §4.4)。
    - Issue #165 で spawn 経路にも lease を張った結果、tmux backend では秘密が
      ``new-session -e`` で **session 環境**に入る = 同一 uid のプロセスが
      ``tmux -L claude-org-broker show-environment`` で他 pane の秘密を読める。

    つまり cred と秘密の両方を読める同一 uid のプロセスは、正しい秘密を提示して lease に
    一致し、last-register-wins をそのまま勝てる。これは #165 が作った穴ではなく (cred 側は
    以前から読めた)、lease が塞ぐ範囲の上限である。

    ``expires_at`` は 2 相のライフサイクルを持つ:
    - **armed** (``None``): assert 直後〜初回 observed register まで。**失効しない**。
      secretary の起動が遅い (段1 folder-trust プロンプト放置等で TTL 超) 場合でも lease が
      消えず、初回 register まで fork/replay 保護を保つ (register 前に wall-clock で失効
      させると保護が黙って外れる — Codex review P2)。
    - **activated** (``float``): 初回 observed register が ``now + observer_lease_seconds``
      を打ち、以後 observed sidecar の register/poll heartbeat が renew する。poll が止まった
      (session 死亡) 後に TTL 経過で失効し、dead session の stale lease が将来の観測束縛や
      recovery register を塞がないようにする。
    """

    secret: str
    # None = armed (未 activate、失効しない)。float = activated 後の失効時刻。
    expires_at: float | None


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
    # observed-session binding (Issue #129 問題 A)。owner -> 現在の observer lease。
    _observer_leases: dict[str, ObserverLease]
    # stand-down 観測面 (Issue #169)。owner -> instance -> 記録
    # ({instance, reason, latched, since, last, count, journalled_at})。sidecar 側の
    # _stood_down は子プロセス内の Event で外から見えないため、daemon 側に「誰が・
    # なぜ・いつから claim していないか」を残して delivery_dump で観測可能にする。
    _delivery_standdowns: dict[str, dict[str, dict]]
    state_dir: Path
    lease_seconds: float
    observer_lease_seconds: float
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

    def _observer_active_locked(self, owner: str, now: float) -> ObserverLease | None:
        """owner の未失効 observer lease を返す (無ければ None)。**_lock 保持中に呼ぶ**。

        observed-session binding (Issue #129 問題 A): lease が active な owner は
        「human launcher が observed live session を assert 済」= その lease 秘密を提示
        できる sidecar だけが generation を bump できる。lease 不在 / 失効時は None を
        返し、:meth:`register_delivery_instance` は従来の last-register-wins に委ねる
        (子 pane 等 launcher が束縛していない owner の push 配信を回帰させない安全側)。
        """
        lease = self._observer_leases.get(owner)
        if lease is None:
            return None
        # armed (expires_at is None) は失効しない (初回 register までの arming window)。
        # activated 後のみ wall-clock で失効させる (dead session の cleanup)。
        if lease.expires_at is not None and lease.expires_at <= now:
            return None
        return lease

    def assert_observer(self, owner: str) -> str:
        """owner の observer lease を assert / rotate し、その秘密を返す (Issue #129 問題 A)。

        human-facing launcher (``org up`` / admin-minted secretary) が observed live
        session を起動する直前に呼ぶ。返る秘密は **mcp-config ではなく子プロセス env**
        (``ORG_BROKER_CHANNEL_OBSERVER``) に載せる非 replay 信号で、その session の
        channel sidecar だけが register 時に提示できる。fork/resume は mcp-config を
        verbatim replay しても process env の秘密を継承しないため lease を提示できず、
        :meth:`register_delivery_instance` が generation bump を拒否する (takeover を断つ)。
        呼ぶたびに秘密を rotate する: 新しい launcher 起動が旧 observed session を
        supersede し、旧 session の秘密は以後 unobserved になる。expires_at は
        observed sidecar の register / poll heartbeat が renew する。
        """
        secret = secrets.token_urlsafe(32)
        with self._lock:
            # armed で置く (expires_at=None): 初回 observed register が TTL 計時を開始する
            # まで失効させない (slow startup で保護が黙って外れるのを防ぐ — Codex P2)。
            self._observer_leases[owner] = ObserverLease(secret=secret, expires_at=None)
        self._journal("observer_lease_asserted", owner=owner)
        return secret

    def clear_observer(self, owner: str, secret: str) -> bool:
        """自分が張った observer lease を落とす (Issue #165)。落ちれば True。

        :meth:`assert_observer` の対 (spawn 経路の失敗巻き戻し)。lease を張った直後に
        pane spawn が失敗すると、誰も秘密を提示できない **armed lease** (失効しない)
        だけが owner に残る。次の spawn は rotate するので実害は小さいが、その間に
        同 agent_id へ mint された channel token の sidecar が
        ``observer_pending`` で claim できなくなるため、発行元が巻き戻す。

        **compare-and-delete** にするのが要: 巻き戻しは失敗経路で走り、そこでは既に
        name 予約と token が解放されている。その隙に同名の別 caller が新しい lease を
        張れるので、無条件 pop だと **他人が今張った lease を消してしまう** (その
        session は mute されないが fork 保護だけが黙って外れる)。自分が受け取った秘密と
        一致する時だけ落とす。
        """
        with self._lock:
            lease = self._observer_leases.get(owner)
            if lease is None or lease.secret != secret:
                return False
            del self._observer_leases[owner]
        self._journal("observer_lease_cleared", owner=owner)
        return True

    def scrub_secrets(self, text: str) -> str:
        """診断文字列から live な observer 秘密を伏せる (Issue #165)。

        spawn 経路は秘密を adapter の ``env`` に載せるが、adapter は起動失敗時に
        **引数列をそのまま例外文に載せる** (tmux は ``-e KEY=VALUE``、wezterm は
        argv 前置の ``env KEY=VALUE``)。その文字列は tools/call のエラーとして
        呼び元エージェントへ返り、traceback ごと ``queue.jsonl`` にも書かれる
        (queue.jsonl は admin.token と違い 0600 ではない)。``_tool_error_message``
        が「引数は載せない」と宣言している scrub-policy を、例外文経由の回り込みに
        対しても効かせる。

        2 段で伏せる。**live 値の一致だけでは足りない**のが要点で、spawn の失敗経路は
        例外が診断層へ届く前に :meth:`clear_observer` で lease を落とすため、その時点で
        秘密は「live ではない」= 値一致では捕まらない。

        1. ``ORG_BROKER_CHANNEL_OBSERVER`` への代入形 (``-e K=V`` / ``env K=V`` /
           JSON の ``"K": "V"``) を、値の生死に依らず伏せる。
        2. 加えて live な lease 秘密の一致も伏せる (前置の無い剥き出しの値まで届く)。

        秘密「らしき」語を推測する汎用パターンは置かない (誤爆で診断が読めなくなる方が
        高くつく)。他の秘匿値 (full token / delivery cred) が ``--mcp-config`` 経由で
        同じ例外文に載る問題は **本 PR 以前からの既知の露出** で、ここでは触らない。
        """
        text = _OBSERVER_ASSIGN_RE.sub(r"\1\2[REDACTED_OBSERVER_SECRET]", text)
        with self._lock:
            secrets_now = [l.secret for l in self._observer_leases.values()]
        for secret in secrets_now:
            if secret and secret in text:
                text = text.replace(secret, "[REDACTED_OBSERVER_SECRET]")
        return text

    def _note_standdown_locked(
        self, owner: str, instance_id: str, reason: str, now: float,
    ) -> tuple[dict, bool]:
        """register 拒否を owner 単位で記録する (Issue #169 の観測面)。

        **_lock 保持中に呼ぶ** (I/O はしない)。sidecar 側の stand-down は子プロセス内の
        :class:`threading.Event` で外から見えないため、「どの instance が・なぜ・
        いつから claim していないか」を daemon 側に残し :meth:`delivery_dump` で
        晒す。返り値は ``(記録, journal すべきか)``。

        記録は **(owner, instance) 単位**で持つ。owner に 1 枠だけだと、複数の instance
        が交互に再試行した瞬間に互いを上書きし、``since`` が毎秒 now に戻って「1 時間
        黙っている pane」が「0 秒前から」に見える。さらに latch した正統 instance の
        記録が、粘っている fork の記録に消される (一番見たい 1 行が消える)。

        journal は **状態が変わった時だけ** 出す。non-latching な拒否 (
        ``observer_pending``) や fence された poll は毎秒繰り返されるので、毎回 journal
        すると queue.jsonl が毎秒太る。同一 ``(instance, reason)`` の反復は ``count`` /
        ``last`` を進めるだけにし、遷移にも duplicate 検知と同じ lease window の cooldown
        を owner 単位で掛ける。継続状態の観測は delivery_dump が担う。
        """
        per_owner = self._delivery_standdowns.setdefault(owner, {})
        prev = per_owner.get(instance_id)
        if prev is not None and prev["reason"] == reason:
            prev["last"] = now
            prev["count"] += 1
            return prev, False
        # owner 単位の journal cooldown (instance が交互に来ても発散させない)。
        last_journal = max((r["journalled_at"] for r in per_owner.values()),
                           default=0.0)
        emit = now - last_journal > self.lease_seconds
        rec = {
            "instance": instance_id,
            "reason": reason,
            "latched": reason in LATCHING_REFUSALS,
            # 同じ instance が reason を遷移しても「いつから黙っているか」は保つ。
            "since": prev["since"] if prev is not None else now,
            "last": now,
            "count": (prev["count"] + 1) if prev is not None else 1,
            "journalled_at": now if emit else last_journal,
        }
        per_owner[instance_id] = rec
        # 無制限成長を防ぐ。捨てるのは **latch していない古い記録から** (latch した
        # 記録 = そのプロセスが二度と claim しないという、一番残す価値のある事実)。
        while len(per_owner) > _STANDDOWN_MAX_PER_OWNER:
            victim = min(per_owner,
                         key=lambda i: (per_owner[i]["latched"], per_owner[i]["last"]))
            del per_owner[victim]
        return rec, emit

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
    def register_delivery_instance(
        self, token: str, instance_id: str, *,
        observer: str | None = None, bg_hosted: bool = False,
    ) -> dict:
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

        **Issue #129 問題 B (Phase 1) — 明示 bg-hosted marker suppress**: ``bg_hosted``
        (sidecar が明示 marker env を受け取った時だけ True) の register は generation を
        bump せず claim も発行しない。``delivery_suppressed_bg_hosted`` を journal して
        観測性を残し、sidecar に stand-down (claim loop 不起動) を指示する。bg 判定は
        **明示 marker のみ** で行い heuristic (isatty / process tree) は使わない
        (foreground 誤判定で push が止まる事故側に倒れるため。不明時は foreground 扱い)。

        **Issue #129 問題 A (Phase 2) — observed-session binding**: owner に active な
        observer lease がある (human launcher が :meth:`assert_observer` 済) 場合、
        ``observer`` 秘密が一致する sidecar だけが generation を bump できる。秘密を
        提示できない register (= mcp-config を replay しただけの fork/resume で process
        env の秘密を持たない sidecar) は generation を bump せず拒否する (observed live
        session の takeover を断つ)。lease 不在 / 失効の owner は従来の
        last-register-wins に委ねる (子 pane 等の push 配信を回帰させない)。

        **Issue #169 — 拒否の 2 分割 (latch するもの / しないもの)**: 上の拒否を
        「二度と claim するな」と「まだ正統でないだけ」に分ける。判定は *daemon が
        実際に知りうること* だけに基づく — すなわち **caller が秘密を提示したか**:

        - 秘密を提示したが現 lease と不一致 -> かつてこの owner の秘密を持っていた
          session が :meth:`assert_observer` の rotate で supersede された。再試行で
          覆る状態ではないので ``unobserved`` (:data:`LATCHING_REFUSALS`) を返し
          sidecar を恒久 stand-down させる。「fence された旧 session が粘って claim を
          取り戻す」のを防ぐという latch 本来の目的はここに残る。
        - 秘密を未提示 -> fork replay か、adopt を経ていない正統な手動起動かを daemon
          は **区別できない**。区別を表現する機構は明示 adopt 経路 (#166) の担当なので
          ここで推測はしない。代わりに latch もせず ``observer_pending`` を返し、
          sidecar に poll cadence での再試行を許す。拒否は generation を bump せず
          in-flight 行も動かさないため、再試行が現職と generation を ping-pong する
          ことはない (Issue #129 の fence はそのまま効いている)。再試行が通るのは
          **現職が poll heartbeat を止めて lease が TTL 失効した時だけ** で、その
          heartbeat は現世代 instance の poll のみが打つ (:meth:`poll_claims` の fence
          後、docs/channel-delivery-model-decision.md §8.1「現世代 instance を見る」)。
          fence された instance の poll は lease を延命できないので、「粘れば勝てる」
          にはならない。
        """
        journal: tuple[str, dict] | None = None
        with self._lock:
            owner = self._delivery_owner_locked(token)
            if owner is None:
                return {"ok": False, "error": "unauthorized"}
            now = time.time()
            if bg_hosted:
                # Phase 1: 明示 bg-hosted marker -> register/claim 抑止 (generation 不変)。
                rec, emit = self._note_standdown_locked(
                    owner, instance_id, REFUSE_BG_HOSTED, now)
                if emit:
                    journal = ("delivery_suppressed_bg_hosted",
                               {"owner": owner, "instance": instance_id})
                result: dict = {"ok": False, "error": REFUSE_BG_HOSTED,
                                "owner": owner}
            else:
                lease = self._observer_active_locked(owner, now)
                if lease is not None and observer != lease.secret:
                    # Phase 2: observer lease が active だが秘密不一致 (未提示含む)。
                    # generation は bump しない。**latch させるか否かをここで分ける**
                    # (Issue #169):
                    if observer:
                        # 秘密を提示したのに現 lease と一致しない = この caller は
                        # かつて秘密を持っていた = rotate で supersede された session。
                        # 再試行では絶対に覆らないので latch させる (fenced な旧
                        # session が claim を取り戻そうと粘れない、という latch 本来の
                        # 目的はここに残る)。
                        code = REFUSE_SUPERSEDED
                        event = "delivery_register_superseded"
                    else:
                        # 秘密を一切提示していない。これが fork replay なのか、adopt を
                        # 経ていない正統なセッションなのかは **daemon には区別できない**
                        # (区別を表現する機構は明示 adopt 経路 = #166)。区別できないもの
                        # を推測しない代わりに latch もしない: 現職の lease が生きている
                        # 限り拒否し続け、現職が heartbeat を止めて lease が失効した時
                        # だけ通る。拒否は generation を bump しないので、再試行が現職と
                        # generation を ping-pong することはない。
                        code = REFUSE_OBSERVER_PENDING
                        event = "delivery_register_unobserved"
                    rec, emit = self._note_standdown_locked(
                        owner, instance_id, code, now)
                    if emit:
                        journal = (event, {"owner": owner, "instance": instance_id,
                                           "latched": rec["latched"]})
                    result = {"ok": False, "error": code, "owner": owner}
                else:
                    gen = self._generation_of(owner) + 1
                    self._delivery_generations[owner] = gen
                    self._delivery_instances[owner] = instance_id
                    # 旧世代の CLAIMED 行を差し戻す (新 generation != claim_generation)。
                    for row in self._rows.values():
                        if (row.state == CLAIMED and row.to_id == owner
                                and row.claim_generation != gen):
                            row.state = UNDELIVERED
                            row.owner = None
                    # register が通った instance の記録だけ落とす (Issue #169)。
                    # **owner ごと消さない**のが要点: 他 instance の記録は「この owner
                    # には黙っている sidecar が別にいる」という、まさに今から効く事実
                    # (二重 sidecar のシグナル)。takeover の瞬間に観測面を白紙に戻すと、
                    # 「なぜ静かなのか」を一番知りたい時に何も残らない。
                    per_owner = self._delivery_standdowns.get(owner)
                    if per_owner is not None:
                        per_owner.pop(instance_id, None)
                        if not per_owner:
                            del self._delivery_standdowns[owner]
                    observed = lease is not None
                    if observed:
                        # observed sidecar の register で lease を activate (armed->TTL 計時
                        # 開始) / renew する。以後 poll heartbeat が renew し続ける。
                        lease.expires_at = now + self.observer_lease_seconds
                    journal = ("delivery_generation_registered",
                               {"owner": owner, "generation": gen,
                                "instance": instance_id, "observed": observed})
                    result = {"ok": True, "owner": owner, "generation": gen,
                              "instance_id": instance_id}
        if journal is not None:
            self._journal(journal[0], **journal[1])
        return result

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
            cur_instance = self._delivery_instances.get(owner)
            if (cur_gen == 0 or generation != cur_gen
                    or instance_id != cur_instance):
                # 未登録 (cur_gen==0) / 旧世代 / 別 instance の sidecar。claim を発行
                # しない (fence)。**instance_id も照合する** のが要: stale sidecar は
                # stale_sidecar 応答で現世代番号を知りうるため、generation だけの照合は
                # 現世代番号を replay されると破れる。現 instance_id は応答に載せず daemon
                # だけが持つ (register 済の唯一の claimer 識別子) ので、これを一致条件に
                # 加えることで daemon 側で真に単一 claimer を強制する (Codex review P2)。
                #
                # ここも stand-down 面に載せる (Issue #169): **黙っている sidecar の
                # 多数派はこちら** — register には成功したが後から世代交代された
                # instance で、以後は claim せず poll だけ続ける。register 拒否だけを
                # 記録すると、一番よく起きる mute が観測面から丸ごと抜ける。
                _rec, emit_sd = self._note_standdown_locked(
                    owner, instance_id, REFUSE_STALE_SIDECAR, now)
                if emit_sd:
                    dup_journal.append((
                        "delivery_poll_fenced",
                        {"owner": owner, "instance": instance_id,
                         "generation": cur_gen},
                    ))
                result: dict = {"error": "stale_sidecar", "rows": [],
                                "generation": cur_gen}
            else:
                # 現世代 instance の poll は observed session が live な heartbeat。
                # observer lease があれば renew する (Issue #129: session 継続中は束縛を
                # 維持し、poll が止まった dead session のみ TTL 経過で失効させる)。
                lease = self._observer_active_locked(owner, now)
                if lease is not None:
                    lease.expires_at = now + self.observer_lease_seconds
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
            cur_instance = self._delivery_instances.get(owner)
            row = self._rows.get(rid)
            if row is None:
                result: dict = {"ok": False, "error": "unknown_row"}
            elif row.to_id != owner:
                result = {"ok": False, "error": "not_owner"}
            elif (cur_gen == 0 or generation != cur_gen
                    or instance_id != cur_instance):
                # stale sidecar (superseded / 未登録 / 別 instance)。拒否する。
                # instance_id も照合する (poll と同じ理由: 現世代番号 replay 防止。P2)。
                # 再 eligible 化は **世代番号が真に古い (generation != cur_gen) 呼び手の
                # 自分の claim だけ** に限る: 同世代・別 instance の呼び手 (現世代番号を
                # replay した stale) が現 instance の live claim (claim_generation ==
                # cur_gen) を剥がしてはならない。register 側の即差し戻しが主で、これは
                # lease 遅延回避の保険 (既に UNDELIVERED なら no-op で冪等)。
                if (generation != cur_gen and row.state == CLAIMED
                        and row.owner == owner
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
            # observed-session binding も落とす (Issue #129): 残ると同名 respawn 後に
            # 旧 observer lease を継承し、新 session の sidecar が unobserved 扱いで
            # claim できなくなる (誤束縛)。
            self._observer_leases.pop(owner, None)
            # stand-down 記録も落とす (Issue #169): 旧 session の「黙っている」記録が
            # 同名 respawn 後の観測面に残ると、新 pane が muted だと誤読される。
            self._delivery_standdowns.pop(owner, None)
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
                # observed-session binding 診断 (Issue #129): owner ごとの observer lease
                # 失効時刻 (active な束縛の有無と残 TTL の切り分け)。秘密自体は晒さない。
                "observers": {o: l.expires_at
                              for o, l in self._observer_leases.items()},
                # stand-down 観測面 (Issue #169): claim していない sidecar は子プロセス
                # 内で沈黙するだけで外から見えないため、「どの owner の どの instance が
                # ・なぜ・いつから claim していないか」を owner -> instance -> 記録 で
                # 出す。``latched`` True は当該プロセスが二度と claim しないこと、False
                # は再試行中 (現職 lease の失効 / pane の消滅 / adopt で覆る) を意味する。
                # 同一 owner に 2 件以上並ぶこと自体が二重 sidecar のシグナルになる。
                "standdowns": {o: {i: dict(r) for i, r in per.items()}
                               for o, per in self._delivery_standdowns.items()},
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
