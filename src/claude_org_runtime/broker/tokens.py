# -*- coding: utf-8 -*-
"""token ↔ agent/pane bind の保持 (broker の認証/登録状態)。

設計 SoT: docs/design/renga-decoupling.md §4.4 (per-agent token + static
headers)。現行 canonical は本モジュール。歴史的 origin: claude-org-transport-lab
spike/broker.py の faithful port。

:class:`AgentBind` は broker のみが保持する bind レコード。:class:`TokenMixin`
は bind 表 (``_binds``) を操作するメソッド群で、:class:`~claude_org_runtime.
broker.server.Broker` に mix-in される。``_binds`` / ``_lock`` の実体は
``Broker.__init__`` が確立する (この mixin の前提契約)。
"""

from __future__ import annotations

import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..terminal import PaneId

if TYPE_CHECKING:  # mixin が前提とする Broker の共有状態 (型のみ)
    pass

# 1 bind が同時に保持できる MCP session 数の上限。超えたら最古を落とす。
# ponytail: 固定上限。DELETE せず落ちた client の session が溜まり続けないための
# 歯止めで、token を持つ fork がこの数だけ initialize すれば最古 (元 session) を
# 追い出せる。token 保持者は drain も呼べるので新しい露出ではない (#168)。
MAX_SESSIONS_PER_BIND = 16

# token を env 参照で渡すときの変数名 (#218)。mcp-config には ``${ORG_BROKER_TOKEN}``
# だけが残り、値は起動プロセスの env で渡す。
TOKEN_ENV = "ORG_BROKER_TOKEN"


@dataclass
class AgentBind:
    """token ↔ agent/pane の bind (設計書 §4.4)。broker のみが保持する。

    role / auth_role の二系統 (Issue B codex Blocker 対応の維持):
    ``auth_role`` は token 発行時に確定する**不変の権限 tier** で、surface の
    role-scoped 公開 (§4.2) / tier gating はこれ**のみ**で決める。``role`` は
    list_peers / list_panes に出る**表示専用**ラベルで、``set_pane_identity``
    で書き換えられる (renga three-state)。表示 role を auth に使うと
    set_pane_identity 経由の権限昇格を許すため、両者を分離している
    (意図的セキュリティ強化であり gap ではない)。
    """

    token: str
    agent_id: str
    name: str
    role: str                         # 表示専用 (set_pane_identity で可変)
    auth_role: str = ""               # 不変の権限 tier (issue_token で確定)
    pane_id: PaneId | None = None     # backend ネイティブ型 (WezTerm=int / tmux="%N"=str)
    cwd: str | None = None            # spawn 時に broker が保持 (Set D cwd parity, §3.3-4)
    kind: str | None = None           # peer client 種別 ("claude" / "codex" / None)
    registered: bool = False          # MCP initialize 到達で True (AC-2-3 の検知点)
    registered_at: float | None = None
    # live MCP session の集合 (挿入順 = 古い順)。fork / resume は mcp-config の
    # token を再生するので 1 token を複数プロセスが共有しうる。session を 1 つしか
    # 持たないと、後から initialize した側が先の側を ``[session_invalid]`` に
    # 追い出し、片方の DELETE が共有 bind の ``registered`` を落として生きている
    # 側を配送先から消す (decision note §4.5 / #168)。session ごとに持てば、
    # 互いの session と配送登録を無効化できない。
    sessions: dict[str, None] = field(default_factory=dict)
    summary: str = ""
    revoked: bool = False
    # credential scope (§9.4 delivery-scoped token)。
    # - ``full``: agent / admin。全ツール面 + tier に従う MCP 操作が可能。
    # - ``delivery``: channel sidecar 専用。``/poll-claims`` と ``/confirm-delivered``
    #   ・``agent_id == owner`` の行のみ許可し、MCP ツール面 (/mcp) を**構造的に拒否**
    #   する (least-privilege)。registered=False のまま発行され list_peers / 配送先
    #   解決 / find_registered には現れない (real bind と agent_id を共有しても衝突
    #   しないのはこの registered=False 由来)。
    scope: str = "full"

    def __post_init__(self) -> None:
        # auth_role 未指定なら発行時 role を権限 tier の初期値にする。以後
        # set_pane_identity が role を書き換えても auth_role は不変に保つ。
        if not self.auth_role:
            self.auth_role = self.role


class TokenMixin:
    """bind 表 (token 発行 / pane bind / 登録検知 / mcp-config 生成)。

    共有状態の前提 (Broker.__init__ が確立):
    - ``self._lock``: binds / rows / delivery-mode を一括ガードする単一 Lock。
    - ``self._binds``: ``token -> AgentBind``。
    """

    # 型注釈のみ (実体は Broker.__init__)。mixin の自己文書化。
    _lock: threading.Lock
    _binds: dict[str, AgentBind]

    def issue_token(
        self,
        agent_id: str,
        name: str,
        role: str,
        pane_id: PaneId | None = None,
        cwd: str | None = None,
        kind: str | None = None,
        auth_role: str | None = None,
        unique: bool = False,
    ) -> str:
        """spawn 時の per-agent token 発行 (設計書 §4.4)。

        ``role`` は**表示専用**ラベル。``auth_role`` は**不変の権限 tier** で、
        省略時は ``role`` を初期値にする。spawn フローは表示 role の自己申告に
        よる tier 昇格を防ぐため、caller tier で上限を切った tier を ``auth_role``
        に明示渡しする (表示 role はそのまま渡す)。以後 ``set_pane_identity`` が
        表示 role を書き換えても ``auth_role`` は不変 (tier gating の根拠を
        昇格不能にする)。``cwd`` / ``kind`` は list_peers / list_panes 出力の
        cwd parity (§3.3-4) に使う。

        ``unique=True`` は同 ``agent_id`` / ``name`` の active bind が既に在れば
        ``ValueError`` を投げる (admin mint の重複防御。queue は ``agent_id`` 単位で
        共有され配送解決も ``agent_id`` / ``name`` の先着 1 件に当たるため、重複名で
        再発行すると queue 共有・誤配送が起きる)。検査と insert を **同一ロック
        スコープ**で原子的に行い TOCTOU を閉じる (ThreadingHTTPServer 配下の並行
        admin 要求でも安全)。
        """
        token = secrets.token_urlsafe(32)
        with self._lock:
            if unique:
                for b in self._binds.values():
                    if not b.revoked and (b.agent_id == agent_id or b.name == name):
                        raise ValueError(
                            f"[name_taken] agent id/name {name!r} is already in use"
                        )
            self._binds[token] = AgentBind(
                token=token, agent_id=agent_id, name=name, role=role,
                auth_role=auth_role or role, pane_id=pane_id, cwd=cwd, kind=kind,
            )
        self._journal("token_issued", agent_id=agent_id, role=role, pane_id=pane_id)
        return token

    def _rekey_bind_locked(self, old_token: str, new_token: str | None = None) -> str:
        """bind を新しい token 文字列へ **付け替え** て返す。**_lock 保持中に呼ぶ**。

        明示 adopt (#166) 用。adopt は同じ agent の配達所有権を別プロセスへ移すが、
        bind をそのまま使い回すと **2 つの live プロセスが 1 つの bind を共有** する。
        session は bind ごとに複数持てる (#168) ので互いを追い出しはしないが、旧
        プロセスが同じ token で MCP 面 (check_messages の drain 等) を使い続けられて
        しまう。旧プロセスへ渡した token を無効化することで、「配達所有権を移した」が
        「MCP 面も移った」を伴うようにする。

        **bind オブジェクトは作り直さない**: 表の鍵だけ差し替える。registered / cwd /
        role / auth_role / pane_id をそのまま引き継ぐので、handover の最中に owner が
        「登録されていない agent」になって送信元から見えなくなることがない (その窓に
        届いた message は queue に残るべきで、``[peer_not_found]`` で弾かれてはいけない)。
        ``sessions`` だけは空にする — それは旧プロセスの MCP session の識別子であり、
        引き継ぐ相手が居ない。

        ``new_token`` を明示するのは **失効時の巻き戻し** 経路だけ (元の token へ戻す)。
        """
        bind = self._binds.pop(old_token)
        token = new_token or secrets.token_urlsafe(32)
        bind.token = token
        bind.sessions.clear()
        self._binds[token] = bind
        return token

    def issue_delivery_cred(self, owner: str) -> str:
        """channel sidecar 用の delivery-scoped credential を発行する (§9.4)。

        agent の full token とは **別物**。``scope="delivery"`` / ``agent_id=owner``
        (= 配送対象) で bind し、``registered=False`` のまま置く。これにより
        list_peers / 配送先解決 / find_registered には現れず (real bind と agent_id
        を共有しても registered=False で衝突しない)、MCP ツール面 (/mcp) は server が
        scope で構造的に拒否する。``/poll-claims`` / ``/confirm-delivered`` のみ、かつ
        ``to_id == owner`` の行のみ操作できる (least-privilege)。
        """
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._binds[token] = AgentBind(
                token=token, agent_id=owner, name="", role="",
                auth_role="", scope="delivery",
            )
        self._journal("delivery_cred_issued", owner=owner)
        return token

    def revoke_delivery_creds(self, owner: str) -> int:
        """owner 宛の delivery-scoped credential を全て revoke する (切戻し §5.5)。

        per-pane channel sidecar の reap に伴い当該 agent の配送 credential を失効
        させる (orphan 化させない)。revoke 件数を返す (冪等; 既 revoked は数えない)。
        """
        n = 0
        with self._lock:
            for b in self._binds.values():
                if b.scope == "delivery" and b.agent_id == owner and not b.revoked:
                    b.revoked = True
                    n += 1
        if n:
            self._journal("delivery_cred_revoked", owner=owner, count=n)
        return n

    def channel_server_config(self, delivery_cred: str, owner: str) -> dict:
        """spawn 時に子の --mcp-config へ加える channel sidecar の stdio server 定義。

        channel sidecar (:mod:`claude_org_runtime.broker.channel_sidecar`) を stdio
        MCP サーバーとして子 claude に load させる。delivery cred は env に**リテラルで**
        載せる (fork による replay は observer lease が fence する。agent token の env
        参照化 #218 の対象外)。daemon URL は base (``/mcp`` 無し) で渡し、
        sidecar が ``/poll-claims`` / ``/confirm-delivered`` を叩く。
        """
        return {
            "command": sys.executable,
            "args": ["-m", "claude_org_runtime.broker.channel_sidecar"],
            "env": {
                "ORG_BROKER_CHANNEL_DAEMON_URL": self.base_url,
                "ORG_BROKER_CHANNEL_CRED": delivery_cred,
                "ORG_BROKER_CHANNEL_OWNER": owner,
            },
        }

    def bind_pane(self, token: str, pane_id: PaneId) -> None:
        with self._lock:
            self._binds[token].pane_id = pane_id

    def register_local(self, token: str) -> None:
        """MCP を経由しない server-side 合成エージェント (検証用 observer 等) を
        登録済みにする。実エージェントの登録は initialize 到達でのみ行う。"""
        with self._lock:
            bind = self._binds[token]
            bind.registered = True
            bind.registered_at = time.time()

    def mcp_config_for(
        self, token: str, server_name: str = "org-broker", *, token_env: bool = False,
    ) -> dict:
        """--mcp-config に渡す JSON。

        既定は token を static headers に直書きする。``token_env=True`` なら header を
        ``Bearer ${ORG_BROKER_TOKEN}`` (:data:`TOKEN_ENV`) にし、呼び元が token を起動
        プロセスの env で渡す (#218)。Claude Code は ``${VAR}`` を起動時に一度だけ展開し、
        resume / fork / continue / ``/mcp`` 再接続は起動時の env を再生しないため、
        config だけを replay した session は認証されない (未設定なら literal が届いて
        401)。env を安全に渡せない起動経路は既定の直書きのまま
        (docs/broker-token-env-decision.md)。
        """
        auth = f"Bearer ${{{TOKEN_ENV}}}" if token_env else f"Bearer {token}"
        return {
            "mcpServers": {
                server_name: {
                    "type": "http",
                    "url": self.url,
                    "headers": {"Authorization": auth},
                }
            }
        }

    def get_bind(self, token: str) -> AgentBind | None:
        with self._lock:
            bind = self._binds.get(token)
            if bind and not bind.revoked:
                return bind
            return None

    def find_registered(self, agent_id: str) -> AgentBind | None:
        """list_peers 相当の登録検知 (AC-2-3)。bind 表ベース。"""
        with self._lock:
            for b in self._binds.values():
                if b.agent_id == agent_id and b.registered and not b.revoked:
                    return b
        return None

    if TYPE_CHECKING:  # 他 mixin / server が供給するメンバ (型チェッカ向け宣言)
        url: str
        base_url: str

        def _journal(self, event: str, **fields: object) -> None: ...
