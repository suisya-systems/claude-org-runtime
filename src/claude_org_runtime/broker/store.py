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

# ------------------------------------------------- adopt の in-flight policy
# 明示 adopt (#166) が旧 host の in-flight ``CLAIMED`` 行をどう扱うか。**既定を
# 選ぶのではなく operator に選ばせる**のが要点 (事前 Codex design review Major):
# 旧 host が既に emit 済なら requeue は重複 action になり、戻さなければ会話末尾が
# 失われる。どちらが正しいかは message の冪等性次第で daemon には判定できない。
#
# - ``requeue``: ``UNDELIVERED`` へ戻す (**既定**)。register 時の既存挙動と同じで、
#   「沈黙より重複」という本配送路の既存方針 (docs §7.8: 配達は at-least-once で
#   duplicate-action 窓は元から開いている) と整合する。
# - ``drop``: ``DELIVERED`` にして二度と配らない。at-most-once が要る運用向けで、
#   会話末尾を失う代わりに重複 action を確実に断つ。
#
# どちらを選んでも件数と policy を adopt 応答と journal の両方に残す (選択の帰結を
# 後から追えないまま「静かに片方に倒れていた」状態を作らない)。
ADOPT_INFLIGHT_REQUEUE = "requeue"
ADOPT_INFLIGHT_DROP = "drop"
ADOPT_INFLIGHT_POLICIES = (ADOPT_INFLIGHT_REQUEUE, ADOPT_INFLIGHT_DROP)

# adopt の既定 arming deadline (秒)。この間に adopting sidecar が新秘密を提示して
# register しなければ adopt は **失敗** と判定し、lease を落として
# ``delivery_adopt_expired`` を journal する (:meth:`StoreMixin.adopt_delivery`)。
DEFAULT_ADOPT_ARMING_SECONDS = 300.0

# ---------------------------------------------------- observer lease の状態
# :meth:`StoreMixin._observer_state_locked` が返す状態 (:class:`ObserverLease` 参照)。
# **fence するのは NONE / ARMING_EXPIRED 以外のすべて**。
OBSERVER_NONE = "none"                      # lease 無し = 従来の last-register-wins
OBSERVER_ARMED = "armed"                    # assert 済・未 activate (TTL では失効しない)
OBSERVER_ACTIVE = "active"                  # activate 済・heartbeat 継続中
OBSERVER_STALE = "stale"                    # activate 済・heartbeat 途絶 (fence は維持)
OBSERVER_ARMING_EXPIRED = "arming_expired"  # 一度も activate されないまま期限切れ
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

    ``expires_at`` は 3 状態のライフサイクルを持つ (:meth:`StoreMixin._observer_state_locked`):

    - **armed** (``None``): assert 直後〜初回 observed register まで。**TTL では失効しない**。
      起動が遅い (段1 folder-trust プロンプト放置等で TTL 超) 場合でも lease が消えず、
      初回 register まで fork/replay 保護を保つ (register 前に wall-clock で失効させると
      保護が黙って外れる — Codex review P2)。
    - **active** (未来の ``float``): 初回 observed register が ``now + observer_lease_seconds``
      を打ち、以後 observed sidecar の poll heartbeat が renew する。
    - **stale** (過去の ``float``): heartbeat が TTL 分途切れた。**last-register-wins には
      戻さない** (Issue #169)。以前はここで秘密無し register が通ったが、heartbeat の停止は
      死亡を意味しない — pane の Ctrl+Z (SIGTSTP はプロセスグループ全体)、ラップトップ
      suspend、MCP サーバー再起動の長期化、NTP の wall-clock ステップでも起きる。しかも
      現職は **生涯 1 回しか register しない** (generation war 防止) ので、一度この扉が
      開くとそこに居るのは 1 秒ごとに叩き続ける fork だけで、現職は取り返せない。よって
      stale でも fence を維持し、扉は **外部の行為** (pane の close/reap = broker が実際に
      観測した死、再 spawn の re-assert、将来の adopt #166) だけが開ける。現職が戻って
      poll を再開すれば lease は再び active に戻る。

    ``arming_until`` は **armed 相にだけ効く期限** (Issue #165)。spawn 経路は秘密を adapter
    の env 経路で子へ渡すが、その到達は backend ごとに実装が違い (tmux は ``-e``、wezterm は
    argv 書き換え、herdr protocol 17 は ``pane.split`` 経由のシェル継承)、リポジトリ外の
    挙動に依存する。届かない環境では **その pane 自身の sidecar が秘密を提示できない** ため、
    無期限 armed だと組織全体の push が恒久的に無音になる。一度も observed register が来ない
    まま期限を過ぎた lease は落として今日の last-register-wins に戻す: 誰も秘密を提示できて
    いない = **守るべき現職が存在しない**ので、ここで戻しても fence を奪われる被害者はいない。
    ``None`` は無期限 armed (launcher / secretary 経路。段1 は人間の承認待ちが入る)。

    **脅威モデル**: 上の docstring 冒頭を参照。
    """

    secret: str
    # None = armed (未 activate)。float = activate 後の失効時刻 (過去なら stale)。
    expires_at: float | None
    # armed 相の活性化期限 (None = 無期限)。spawn 経路と adopt 経路が設定する。
    arming_until: float | None = None
    # この lease を張った adopt 操作の ID (:class:`PendingAdoption`)。明示 adopt
    # (#166) 以外の assert では None。register が秘密一致で通った時に、どの adopt が
    # 完了したのかを **秘密を journal に出さずに** 特定するための紐付け。
    adoption_id: str | None = None


@dataclass
class PendingAdoption:
    """進行中の明示 adopt 操作 (#166)。owner ごとに高々 1 件。

    **「秘密の発行に成功した」を操作の成功にしない** ための記録 (事前 Codex design
    review Major)。adopt RPC は observer lease を rotate すると同時に delivery
    generation を bump し現世代 instance を消す = **旧 sidecar をその場で fence** する
    ので、RPC が返った時点で当該 owner には claimer が 1 つも居ない。この窓を閉じる
    のは「adopting session の sidecar が新秘密を提示して register を完了する」ことだけ
    であり、それが起きるまでこのレコードが残る。

    ``armed_until`` を過ぎても完了しなければ adopt は **失敗** で、
    :meth:`StoreMixin._sweep_adoptions_locked` が lease ごと落として
    ``delivery_adopt_expired`` を journal する (attention watcher が operator へ能動
    通知する。失敗が沈黙しないことが adopt の設計要件そのもの)。
    """

    adoption_id: str
    owner: str
    # 発行した observer 秘密。**compare-and-clear 専用** で、応答以外のどこにも
    # 出さない (journal / dump には載せない)。
    secret: str
    armed_until: float
    in_flight_policy: str
    in_flight_rows: int
    # adopt が installed した generation (fence 後の現世代)。
    fenced_generation: int
    # fence **前** の (generation, instance)。失効時の **原状復帰** に使う。
    #
    # 復帰が要るのは、fence された旧 sidecar が二度と register し直さないため
    # (``channel_sidecar.py``: ``stale_sidecar`` は latch しないが、再 register は
    # ``_current_generation() is None`` の時だけで、一度成功した instance はそこを
    # 通らない)。lease を落とすだけでは「adopt に失敗したので保護は外したが、
    # 配達できる sidecar は 1 つも居ない」状態が恒久的に残り、**arming deadline が
    # 防ぐはずだった恒久無音そのもの**になる。
    previous_generation: int
    fenced_instance: str | None
    started_at: float


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
    # 明示 adopt (#166)。owner -> 進行中の adopt 操作 (高々 1 件)。
    _pending_adoptions: dict[str, PendingAdoption]
    # stand-down 観測面 (Issue #169)。owner -> instance -> 記録
    # ({instance, reason, latched, since, last, count, journalled_at})。sidecar 側の
    # _stood_down は子プロセス内の Event で外から見えないため、daemon 側に「誰が・
    # なぜ・いつから claim していないか」を残して delivery_dump で観測可能にする。
    _delivery_standdowns: dict[str, dict[str, dict]]
    state_dir: Path
    lease_seconds: float
    observer_lease_seconds: float
    adopt_arming_seconds: float
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

    def _observer_state_locked(
        self, owner: str, now: float
    ) -> tuple[ObserverLease | None, str]:
        """owner の observer lease と **その状態** を返す。**_lock 保持中に呼ぶ**。

        状態は :data:`OBSERVER_NONE` / :data:`OBSERVER_ARMED` /
        :data:`OBSERVER_ACTIVE` / :data:`OBSERVER_STALE` /
        :data:`OBSERVER_ARMING_EXPIRED` のいずれか (:class:`ObserverLease` 参照)。

        **fence するのは NONE と ARMING_EXPIRED 以外のすべて** (Issue #169): 以前は
        「失効した lease = 無い lease」として last-register-wins に戻していたが、
        heartbeat の停止は死亡ではないので、その扉は fork にしか使えなかった。
        """
        lease = self._observer_leases.get(owner)
        if lease is None:
            return None, OBSERVER_NONE
        if lease.expires_at is None:
            if lease.arming_until is not None and lease.arming_until <= now:
                # 一度も observed register が来ないまま活性化期限を過ぎた。誰も秘密を
                # 提示できていない = 守るべき現職がいないので、今日の挙動へ戻す。
                return lease, OBSERVER_ARMING_EXPIRED
            return lease, OBSERVER_ARMED
        if lease.expires_at <= now:
            return lease, OBSERVER_STALE
        return lease, OBSERVER_ACTIVE

    def assert_observer(self, owner: str, arming_seconds: float | None = None) -> str:
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

        ``arming_seconds`` (Issue #165) は **armed 相にだけ効く活性化期限**。
        ``None`` (既定) は無期限 armed で、人間の承認待ちが入りうる launcher /
        secretary 経路が使う。spawn 経路は有限値を渡す: 秘密が子へ届かない backend /
        ホストに当たった時、無期限 armed だと **その owner の push が恒久的に無音** に
        なるため、一度も observed register が来なければ今日の last-register-wins へ
        戻す (:class:`ObserverLease` 参照)。
        """
        with self._lock:
            secret = self._rotate_observer_locked(owner, arming_seconds)
        self._journal("observer_lease_asserted", owner=owner,
                      arming_seconds=arming_seconds)
        return secret

    def _rotate_observer_locked(
        self, owner: str, arming_seconds: float | None,
        adoption_id: str | None = None,
    ) -> str:
        """observer lease を rotate して新しい秘密を返す。**_lock 保持中に呼ぶ**。

        :meth:`assert_observer` の lock 内本体を切り出したもの。明示 adopt (#166) は
        rotate と delivery generation の fence を **同一 lock スコープ**で行う必要が
        あり (rotate だけでは旧 claimer は止まらない — 事前 Codex design review
        Blocker)、自らロックを取る :meth:`assert_observer` を再入呼び出しできない
        (``_lock`` は非再入)。journal は呼び元が lock 解放後に行う。

        armed で置く (expires_at=None): 初回 observed register が TTL 計時を開始する
        まで失効させない (slow startup で保護が黙って外れるのを防ぐ — Codex P2)。
        """
        secret = secrets.token_urlsafe(32)
        self._observer_leases[owner] = ObserverLease(
            secret=secret, expires_at=None,
            arming_until=None if arming_seconds is None
            else time.time() + arming_seconds,
            adoption_id=adoption_id,
        )
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

    # ------------------------------------------------------- adopt (#166)
    def _adopt_owner_check_locked(
        self, owner: str
    ) -> tuple[str | None, str, str]:
        """adopt 対象 owner の実在を検証する。**_lock 保持中に呼ぶ**。

        返り値は ``(エラー文 or None, full token, delivery cred)``。token を **同じ
        lock スコープで**返すのが要点: adopting session に渡す ``--mcp-config`` は
        「今 検証したその bind」から組む必要があり、検証と別スコープで引き直すと、
        その隙の revoke で「検証は通ったが渡した cred は死んでいる」adopt が成立する。

        rotate / fence と **同一 lock スコープ**で行うのが要点 (事前 Codex design
        review Major (e)): typo や存在しない owner への adopt を「成功」にすると、
        operator は handover したつもりで、実際には誰も居ない owner の lease を張って
        (= 将来その名前で起動する session を ``observer_pending`` で塞いで) 終わる。

        検証は 2 点:

        - **full bind が存在する** (revoked でない)。``registered`` は **問わない**:
          adopt の主用途は「session が死んだ / 手起動で resume した」owner の引き継ぎ
          であり、そこでは MCP initialize 済 (= ``registered``) ではありえない。
          ``registered`` まで要求すると本来の用途がまるごと弾かれる。
        - **delivery credential を保持している** (revoked でない)。adopting session の
          channel sidecar はこの cred で ``/claim-owner`` を叩くので、無ければ adopt
          しても配送は始まらない。
        """
        full_token = ""
        delivery_cred = ""
        for b in self._binds.values():
            if b.revoked or b.agent_id != owner:
                continue
            if b.scope == "full" and not full_token:
                full_token = b.token
            elif b.scope == "delivery" and not delivery_cred:
                delivery_cred = b.token
        if not full_token:
            return (f"[unknown_owner] no live agent bind for owner {owner!r}",
                    "", "")
        if not delivery_cred:
            return (f"[no_delivery_credential] owner {owner!r} holds no delivery "
                    "credential; nothing to adopt (mint or spawn it with channel "
                    "first)", "", "")
        return None, full_token, delivery_cred

    def _sweep_adoptions_locked(self, now: float) -> list[tuple[str, dict]]:
        """arming deadline を過ぎた未完了 adopt を失敗確定させる。**_lock 保持中に呼ぶ**。

        journal すべき ``(event, fields)`` を return し、呼び元が lock 解放後に
        :meth:`_journal` する (非再入 Lock の自己デッドロック回避。本 store の既存契約)。

        **これが adopt の完了条件の後半**である (事前 Codex design review Major (d))。
        adopt は旧 sidecar をその場で fence するので、adopting session が起動に失敗
        すると当該 owner には claimer が 1 つも居ない状態が残る。期限内に register が
        来なければ (a) 張った lease を落として今日の last-register-wins へ戻し、
        (b) ``delivery_adopt_expired`` を journal する。(b) が要点で、attention watcher
        がこれを operator へ能動通知する — **adopt の失敗が沈黙しない**ことが本機能の
        設計要件そのものであり、「秘密の発行に成功した」を操作の成功にしない根拠になる。

        なお本 daemon には周期タスクが無い (reaping も含め全て RPC 契機の lazy 実行)
        ので、本 sweep も delivery 面の各入口から呼ぶ。fence された旧 sidecar が
        poll cadence (~1s) で叩き続けるため、実運用では期限直後に発火する。
        """
        journal: list[tuple[str, dict]] = []
        for owner, pending in list(self._pending_adoptions.items()):
            if pending.armed_until > now:
                continue
            del self._pending_adoptions[owner]
            # 自分が張った lease だけを落とす (compare-and-delete)。期限後に別経路
            # (再 spawn の re-assert / 後続 adopt) が張り直した lease を巻き添えに
            # しない — それは今まさに有効な保護なので消してはならない。
            lease = self._observer_leases.get(owner)
            dropped = False
            if lease is not None and lease.secret == pending.secret:
                del self._observer_leases[owner]
                dropped = True
            # **原状復帰** (compare-and-restore)。adopt が installed した fence
            # (現世代 == fenced_generation かつ現世代 instance が空) がそのまま
            # 残っている時だけ、fence 前の (generation, instance) へ戻す。誰かが
            # 既に register していれば現世代は正統なので触らない。
            #
            # これが無いと、失敗した adopt は「lease は外れたが claimer は誰も
            # 居ない」owner を残す。旧 sidecar は stale_sidecar を受け続けるだけで
            # 再 register しないので、pane を閉じるまで push が戻らない。
            # **queue は復帰させない**: ``in_flight="drop"`` で ``DELIVERED`` にした
            # 行は戻さない (policy の帰結であって fence の副作用ではない)。戻すのは
            # 配達経路であって配達済みの判断ではない。
            restored = False
            if (self._generation_of(owner) == pending.fenced_generation
                    and owner not in self._delivery_instances):
                self._delivery_generations[owner] = pending.previous_generation
                if pending.fenced_instance is not None:
                    self._delivery_instances[owner] = pending.fenced_instance
                restored = True
            journal.append(("delivery_adopt_expired", {
                "owner": owner,
                "adoption_id": pending.adoption_id,
                "armed_seconds": round(pending.armed_until - pending.started_at, 3),
                "lease_dropped": dropped,
                "generation": pending.fenced_generation,
                "restored": restored,
                "restored_generation": (pending.previous_generation if restored
                                        else None),
            }))
        return journal

    def _sweep_adoptions(self) -> None:
        """:meth:`_sweep_adoptions_locked` の自己ロック版 (lock を持たない入口用)。

        ``enqueue`` / ``drain`` のように adopt と原子性を共有する必要がない入口から
        呼ぶ。sidecar が 1 つも poll していない owner (= session ごと死んでいて adopt
        したが起動に失敗した、まさに通知したいケース) でも、その owner 宛に message が
        届いた時点で期限切れが検知される。
        """
        with self._lock:
            journal = self._sweep_adoptions_locked(time.time())
        for event_name, fields in journal:
            self._journal(event_name, **fields)

    def adopt_delivery(
        self, owner: str, *,
        in_flight: str = ADOPT_INFLIGHT_REQUEUE,
        force: bool = False,
        arming_seconds: float | None = None,
    ) -> dict:
        """配達所有権を新しい session へ明示的に引き継ぐ (#166 の本体)。

        **rotate だけでは handover にならない** (事前 Codex design review Blocker):
        :meth:`assert_observer` は秘密を差し替えるだけで、旧 ``(generation,
        instance_id)`` はそのまま現世代に残り、:meth:`poll_claims` は observer 秘密を
        再検証しないので、旧 session は新 sidecar が register するまで claim/confirm を
        続けられる。そのため本メソッドは **同一 lock スコープ**で次を原子的に行う:

        1. owner の実在検証 (:meth:`_adopt_owner_check_locked`)。
        2. observer lease の rotate (新秘密 + 有限 arming deadline + adoption_id)。
        3. **fence**: delivery generation を +1 し、現世代 instance を **消す**。
           ``_delivery_instances`` に entry が無い owner は :meth:`poll_claims` の
           ``instance_id != cur_instance`` 条件で **全 instance が弾かれる** ので、
           この瞬間から adopting sidecar が register するまで **誰も配達できない**。
           これが handover 境界であり、RPC の成功そのものではない。
        4. in-flight ``CLAIMED`` 行に選択された policy を適用し、件数を記録する。

        戻り値の ``observer_secret`` は呼び元 (``org adopt``) が **adopting claude
        プロセスの env** へ載せる非 replay 秘密である。走行中プロセスの env は外から
        書き換えられないため、adopt は必然的に「秘密付きで新しいプロセスを起こす
        launcher」になる (sidecar 側に動的 handoff 経路を足す案は、mcp-config は replay
        されるが process env はされない、という lease の存在根拠そのものを壊すので
        採らない)。

        ``force`` が False のとき、期限内の未完了 adopt が既にあれば
        ``[adopt_in_flight]`` で **拒否** する。rotate は last-rotate-wins なので、
        黙って許すと先行 CLI は「成功」を受け取った後で既に無効な秘密を持つ session を
        起動し、それは ``unobserved`` で恒久沈黙する。競合を暗黙の敗北にせず、
        ``force`` という明示の選択にする。
        """
        if in_flight not in ADOPT_INFLIGHT_POLICIES:
            return {"ok": False, "error": (
                f"[invalid_in_flight] in_flight must be one of "
                f"{list(ADOPT_INFLIGHT_POLICIES)}, got {in_flight!r}")}
        if arming_seconds is None:
            # Broker の tunable を既定にする (**モジュール定数を直接読まない**):
            # 読むと Broker(adopt_arming_seconds=...) が黙って効かなくなり、期限まわりの
            # テストが 300 秒待ちの vacuous pass になる。
            arming_seconds = self.adopt_arming_seconds
        if not isinstance(arming_seconds, (int, float)) or arming_seconds <= 0:
            return {"ok": False, "error": (
                "[invalid_arming_seconds] arming_seconds must be a positive number")}
        journal: list[tuple[str, dict]] = []
        with self._lock:
            now = time.time()
            journal.extend(self._sweep_adoptions_locked(now))
            err, full_token, delivery_cred = self._adopt_owner_check_locked(owner)
            if err is not None:
                result: dict = {"ok": False, "error": err}
            else:
                pending = self._pending_adoptions.get(owner)
                if pending is not None and not force:
                    result = {"ok": False, "error": (
                        f"[adopt_in_flight] adoption {pending.adoption_id} for owner "
                        f"{owner!r} is still armed for "
                        f"{max(0.0, pending.armed_until - now):.1f}s; "
                        "wait for it to land or expire, or pass force to supersede it"),
                        "adoption_id": pending.adoption_id,
                        "armed_seconds_remaining": max(0.0, pending.armed_until - now)}
                else:
                    # 失効時の原状復帰に使う fence **前**の状態。
                    prev_gen = self._generation_of(owner)
                    prev_instance = self._delivery_instances.get(owner)
                    if pending is not None:
                        # force による明示 supersede。**先行 adopt が敗けたことを残す**
                        # (先行 CLI が起動した session はこの後 unobserved で沈黙する
                        # ので、その原因が journal から辿れないと診断不能になる)。
                        journal.append(("delivery_adopt_superseded", {
                            "owner": owner,
                            "adoption_id": pending.adoption_id,
                        }))
                        # **現状ではなく先行 adopt の原状を引き継ぐ**。ここで現状を
                        # 読むと、それは既に先行 adopt が fence した後の状態
                        # (generation は上がり instance は空) なので、この adopt も
                        # 失効した時の「復帰」が中間状態の復元になり、元の sidecar は
                        # stale のまま = 無音が残る。復帰先は常に **最初に fence する
                        # 前の現職** でなければならない (Codex review P2)。
                        prev_gen = pending.previous_generation
                        prev_instance = pending.fenced_instance
                    adoption_id = secrets.token_hex(8)
                    secret = self._rotate_observer_locked(
                        owner, arming_seconds, adoption_id=adoption_id)
                    gen = self._generation_of(owner) + 1
                    self._delivery_generations[owner] = gen
                    # **現世代 instance を消す** = 旧 sidecar は次 poll で stale_sidecar。
                    self._delivery_instances.pop(owner, None)
                    moved = 0
                    for row in self._rows.values():
                        if row.state != CLAIMED or row.to_id != owner:
                            continue
                        moved += 1
                        row.owner = None
                        if in_flight == ADOPT_INFLIGHT_REQUEUE:
                            row.state = UNDELIVERED
                        else:
                            row.state = DELIVERED
                    armed_until = now + arming_seconds
                    self._pending_adoptions[owner] = PendingAdoption(
                        adoption_id=adoption_id, owner=owner, secret=secret,
                        armed_until=armed_until, in_flight_policy=in_flight,
                        in_flight_rows=moved, fenced_generation=gen,
                        previous_generation=prev_gen, fenced_instance=prev_instance,
                        started_at=now,
                    )
                    # 旧 pane の bookkeeping から owner を切り離す (Codex review P1)。
                    # adopt 後の旧 pane は配達所有権を持たない抜け殻で、operator には
                    # 「都合のよい時に閉じてよい」と案内する。その close / reap は
                    # _cleanup_pane を通り、owner の **token と delivery cred を revoke
                    # し delivery state を reset し未配達行を捨てる** ので、切り離さないと
                    # 案内どおりに閉じた瞬間に adopt 済み session が丸ごと死ぬ。
                    detached = self._detach_owner_panes_locked(owner)
                    journal.append(("delivery_adopt_started", {
                        "owner": owner, "adoption_id": adoption_id,
                        "generation": gen, "in_flight_policy": in_flight,
                        "in_flight_rows": moved, "arming_seconds": arming_seconds,
                        "forced": force, "detached_panes": detached,
                    }))
                    result = {
                        "ok": True, "owner": owner, "adoption_id": adoption_id,
                        "observer_secret": secret, "generation": gen,
                        "in_flight_policy": in_flight, "in_flight_rows": moved,
                        "arming_seconds": arming_seconds, "armed_until": armed_until,
                        # 切り離した旧 pane。operator に「これは閉じてよい」と
                        # 具体的に言えるようにする (抜け殻を残す方が事故のもと)。
                        "detached_panes": detached,
                        # 検証と同一 lock スコープで取った owner の資格情報。呼び元
                        # (server の admin ハンドラ) が --mcp-config へ畳んで **必ず
                        # pop する** 内部フィールドで、ワイヤには出さない。
                        "_owner_token": full_token,
                        "_delivery_cred": delivery_cred,
                    }
        for event_name, fields in journal:
            self._journal(event_name, **fields)
        return result

    def adopt_status(self, owner: str) -> dict:
        """owner の adopt 進行状況を返す (**秘密は含めない**)。

        ``org adopt`` が exec 直前の preflight に使う: 自分の adoption_id がまだ現職で
        あることを確認してから claude を起動する (並行 adopt に負けた CLI が、既に無効な
        秘密を持つ session を黙って起動するのを防ぐ)。到達不能な残余レースは残るが、
        その場合も adopting sidecar が ``unobserved`` を受けて latch し、
        ``delivery_register_superseded`` が journal に残る。
        """
        journal: list[tuple[str, dict]] = []
        with self._lock:
            now = time.time()
            journal.extend(self._sweep_adoptions_locked(now))
            pending = self._pending_adoptions.get(owner)
            _lease, state = self._observer_state_locked(owner, now)
            result = {
                "ok": True, "owner": owner,
                "generation": self._generation_of(owner),
                "instance_id": self._delivery_instances.get(owner),
                "observer_state": state,
                "pending": None if pending is None else {
                    "adoption_id": pending.adoption_id,
                    "armed_seconds_remaining": max(0.0, pending.armed_until - now),
                    "in_flight_policy": pending.in_flight_policy,
                    "in_flight_rows": pending.in_flight_rows,
                    "fenced_generation": pending.fenced_generation,
                },
            }
        for event_name, fields in journal:
            self._journal(event_name, **fields)
        return result

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
            # 進行中 adopt の秘密も伏せる (#166): adopt が発行した秘密は lease と
            # pending の両方に載る。lease が後続 rotate で差し替わっても、その秘密を
            # env に載せて起動された session の失敗例外はまだ流れうるので、pending が
            # 生きている間は値一致側でも捕まえる。
            secrets_now.extend(p.secret for p in self._pending_adoptions.values())
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
        self._sweep_adoptions()   # #166: 期限切れ adopt の失敗確定 (lock 外の入口)
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
        self._sweep_adoptions()   # #166: 期限切れ adopt の失敗確定 (lock 外の入口)
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
          ことはない (Issue #129 の fence はそのまま効いている)。

        **再試行が通る条件は「時間が経ったこと」ではない**: lease は stale (TTL 切れ)
        でも fence し続ける (:class:`ObserverLease`)。扉を開けるのは **外部の行為**
        だけ — pane の close/reap (:meth:`reset_delivery_state`。broker が実際に観測した
        死)、再 spawn / 再 mint による :meth:`assert_observer` の rotate、armed のまま
        期限切れになった lease の失効 (誰も秘密を提示できていない = 守るべき現職が
        いない)、そして将来の明示 adopt (#166)。heartbeat の停止を死亡の証拠として
        扱わないのが要点で、それは Ctrl+Z / suspend / MCP 再起動 / NTP ステップでも
        起きるうえ、現職は生涯 1 回しか register しないため、一度開いた扉に居るのは
        1 秒ごとに叩いている fork だけになる。
        """
        journal: list[tuple[str, dict]] = []
        with self._lock:
            owner = self._delivery_owner_locked(token)
            if owner is None:
                return {"ok": False, "error": "unauthorized"}
            now = time.time()
            journal.extend(self._sweep_adoptions_locked(now))
            if bg_hosted:
                # Phase 1: 明示 bg-hosted marker -> register/claim 抑止 (generation 不変)。
                rec, emit = self._note_standdown_locked(
                    owner, instance_id, REFUSE_BG_HOSTED, now)
                if emit:
                    journal.append(("delivery_suppressed_bg_hosted",
                                    {"owner": owner, "instance": instance_id}))
                result: dict = {"ok": False, "error": REFUSE_BG_HOSTED,
                                "owner": owner}
            else:
                lease, state = self._observer_state_locked(owner, now)
                if state == OBSERVER_ARMING_EXPIRED:
                    # 一度も observed register が来ないまま活性化期限を過ぎた lease は
                    # 落として今日の last-register-wins に戻す (Issue #165)。秘密が子へ
                    # 届かない backend / ホストで組織全体が恒久無音になるのを防ぐ安全弁。
                    # ここで戻しても fence を奪われる現職は存在しない (誰も秘密を提示
                    # できていないことが、この状態の定義そのもの)。**保護が外れる瞬間
                    # なので必ず journal に残す** (黙って外れるのが一番悪い)。
                    del self._observer_leases[owner]
                    lease = None
                    journal.append(("observer_arming_expired",
                                    {"owner": owner, "instance": instance_id}))
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
                        # を推測しない代わりに latch もしない: lease がある限り拒否し
                        # 続け、**外部の行為** (pane の close/reap、再 spawn の re-assert、
                        # 将来の adopt) が lease を落とした時に初めて通る。拒否は
                        # generation を bump しないので、再試行が現職と generation を
                        # ping-pong することはない。
                        code = REFUSE_OBSERVER_PENDING
                        event = "delivery_register_unobserved"
                    rec, emit = self._note_standdown_locked(
                        owner, instance_id, code, now)
                    if emit:
                        journal.append(
                            (event, {"owner": owner, "instance": instance_id,
                                     "state": state, "latched": rec["latched"]}))
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
                    # 明示 adopt (#166) の **完了判定**。この register が adopt の張った
                    # lease の秘密で通った時だけ adopt は成功する = 「秘密の発行に成功
                    # した」ではなく「adopting instance が現世代の claimer として登録
                    # された」が操作の完了条件 (事前 Codex design review Blocker/Major)。
                    # compare-and-clear: lease に紐づく adoption_id が pending と一致
                    # する時だけ落とす (期限切れ sweep 後に別 adopt が張った pending を
                    # 巻き添えにしない)。
                    pending = self._pending_adoptions.get(owner)
                    adopted = (observed and pending is not None
                               and lease.adoption_id == pending.adoption_id)
                    if adopted:
                        del self._pending_adoptions[owner]
                        journal.append(("delivery_adopt_completed", {
                            "owner": owner, "adoption_id": pending.adoption_id,
                            "generation": gen, "instance": instance_id,
                            "in_flight_policy": pending.in_flight_policy,
                            "in_flight_rows": pending.in_flight_rows,
                            "elapsed": round(now - pending.started_at, 3),
                        }))
                    journal.append(("delivery_generation_registered",
                                    {"owner": owner, "generation": gen,
                                     "instance": instance_id,
                                     "observed": observed, "adopted": adopted}))
                    result = {"ok": True, "owner": owner, "generation": gen,
                              "instance_id": instance_id}
        for event_name, fields in journal:
            self._journal(event_name, **fields)
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
            # adopt の arming deadline はここで刈る (#166)。本 daemon に周期タスクは
            # 無いが、adopt で fence された旧 sidecar が poll cadence (~1s) で叩き
            # 続けるため、失敗した adopt は期限直後に確実に検知される。**fence 判定
            # より前**に置くのが要点で、fence された poll はこの下で早期 return する。
            dup_journal = self._sweep_adoptions_locked(now)
            # 記録 + duplicate 検知は fence 判定より前に行う (stale 世代の poll でも
            # 「二重 sidecar が生きている」シグナルを残す — Major #5 / #10)。
            dup_journal.extend(self._note_poll_locked(owner, instance_id, now))
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
                # **既に activate 済の lease だけ** renew する (Issue #129 / #169)。
                # stale (TTL 切れ) も renew 対象に含む: 止まっていた現職が戻ってきた
                # ケースで、sticky により扉は開いていないのだから素直に active へ戻す。
                #
                # armed は **renew しない**: activate は「秘密を提示した register」の
                # 専権にする。poll で activate できてしまうと、秘密を一度も提示して
                # いない instance が lease を活性化でき (docs §7 項目5)、arming 期限
                # (誰も提示できていないなら今日の挙動へ戻す、という Issue #165 の安全弁)
                # が黙って無効化される。
                lease, _state = self._observer_state_locked(owner, now)
                if lease is not None and lease.expires_at is not None:
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
            # 旧 observer lease を継承し、新 session の sidecar が claim できなくなる
            # (誤束縛)。**これが sticky lease の主要な解除経路** でもある (Issue #169):
            # pane の close/reap は broker が実際に観測した死なので、TTL の代わりに
            # これが「外部が正統と言った」に相当する。
            self._observer_leases.pop(owner, None)
            # 進行中の明示 adopt も落とす (#166)。pane 自体が消えた = adopting session の
            # 到着先が無くなったので、この adopt はもう完了しえない。**残すと** 期限まで
            # 後続 adopt が [adopt_in_flight] で塞がれ、期限後に「今はもう居ない owner の
            # adopt が失敗した」という誤解を招く event が出る。cancel として journal に
            # 残す (黙って消さない)。
            cancelled = self._pending_adoptions.pop(owner, None)
            # stand-down 記録も落とす (Issue #169): 旧 session の「黙っている」記録が
            # 同名 respawn 後の観測面に残ると、新 pane が muted だと誤読される。
            self._delivery_standdowns.pop(owner, None)
            for k in [k for k in self._duplicate_emit_at if k[0] == owner]:
                del self._duplicate_emit_at[k]
            for row in self._rows.values():
                if row.state == CLAIMED and row.to_id == owner:
                    row.state = UNDELIVERED
                    row.owner = None
        if cancelled is not None:
            self._journal("delivery_adopt_cancelled", owner=owner,
                          adoption_id=cancelled.adoption_id, reason="delivery_reset")

    # --------------------------------------------------------------- dump
    def delivery_dump(self) -> dict:
        """配送ライフサイクルの横断スナップショット (admin/診断用)。

        owner/state を晒すため admin scope に限定する想定 (§9.4 least-privilege:
        delivery-scoped cred からは到達不能)。
        """
        self._sweep_adoptions()   # #166: 期限切れ adopt の失敗確定 (lock 外の入口)
        with self._lock:
            reaped = self._reap_locked()
            now = time.time()
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
                # observed-session binding 診断 (Issue #129 / #169): owner ごとの lease
                # 状態。**bare な失効時刻ではなく state を出す** のが要点で、stale は
                # 「fence が外れた」ではなく「fence したまま heartbeat が途絶えている」
                # を意味するようになった (Issue #169 の sticky lease)。両者が同じ float
                # に見えると、fence 済 owner を unfenced と誤読する。秘密自体は晒さない。
                "observers": {
                    o: {"state": self._observer_state_locked(o, now)[1],
                        "expires_at": l.expires_at,
                        "arming_until": l.arming_until}
                    for o, l in self._observer_leases.items()
                },
                # stand-down 観測面 (Issue #169): claim していない sidecar は子プロセス
                # 内で沈黙するだけで外から見えないため、「どの owner の どの instance が
                # ・なぜ・いつから claim していないか」を owner -> instance -> 記録 で
                # 出す。``latched`` True は当該プロセスが二度と claim しないこと、False
                # は再試行中 (現職 lease の失効 / pane の消滅 / adopt で覆る) を意味する。
                # 同一 owner に 2 件以上並ぶこと自体が二重 sidecar のシグナルになる。
                "standdowns": {o: {i: dict(r) for i, r in per.items()}
                               for o, per in self._delivery_standdowns.items()},
                # 進行中の明示 adopt (#166)。**秘密は載せない**。fence 済で claimer が
                # 1 つも居ない窓 (adopt 発行〜adopting register) がここに現れるので、
                # 「なぜ誰も配達していないのか」を dump 単独で説明できる。
                "adoptions": {
                    o: {"adoption_id": p.adoption_id,
                        "armed_seconds_remaining": max(0.0, p.armed_until - now),
                        "in_flight_policy": p.in_flight_policy,
                        "in_flight_rows": p.in_flight_rows,
                        "fenced_generation": p.fenced_generation}
                    for o, p in self._pending_adoptions.items()
                },
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

        # server が供給する pane 切り離し (#166)。**_lock 保持中に呼ばれる**。
        def _detach_owner_panes_locked(self, owner: str) -> list[str]: ...
