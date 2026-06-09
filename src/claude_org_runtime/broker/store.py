# -*- coding: utf-8 -*-
"""queue store + journal (broker の永続化層)。

設計 SoT: docs/design/renga-decoupling.md §4 / Set D 2.3 (at-most-once
drain)。canonical 実装: claude-org-transport-lab spike/broker.py の
faithful port。

:class:`StoreMixin` は queue の投入 / 排出と JSONL journal の追記だけを担う
純粋な永続化責務で、:class:`~claude_org_runtime.broker.server.Broker` に
mix-in される。配達 (nudge / PTY 注入) は terminal adapter とスレッド管理に
依存する別責務なので server 側 (:meth:`Broker._trigger_nudge`) に置き、
ここからは ``self._trigger_nudge`` を呼ぶだけにしている (codex design review
Major 対応: queue 永続化と PTY 注入を結合させない)。

並行性契約 (移植元の検証済みロジック):
- ``_lock`` は binds / queues を一括ガードする単一 Lock (lock 分割はしない)。
- lock 内では外部 I/O を行わない。``_journal`` 自身が lock を取るため、
  ``_journal`` を別の lock スコープの中から呼ばない (DELETE デッドロック回避)。
- queue 書込先は ``state_dir / "queue.jsonl"``。daemon の既定 state_dir は
  ``.state/broker`` (CWD 相対、:mod:`claude_org_runtime.broker.cli` 参照)。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tokens import AgentBind


class StoreMixin:
    """queue store + journal。Broker.__init__ が ``_lock`` / ``_queues`` /
    ``_binds`` / ``state_dir`` を確立する前提で動く。"""

    # 型注釈のみ (実体は Broker.__init__)。mixin の自己文書化。
    _lock: threading.Lock
    _binds: dict[str, "AgentBind"]
    _queues: dict[str, list[dict]]
    state_dir: Path

    def _journal(self, event: str, **fields) -> None:
        rec = {"ts": time.time(), "event": event, **fields}
        path = self.state_dir / "queue.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def enqueue(self, from_bind: "AgentBind", to_id: str, message: str) -> dict:
        """queue store 投入 + ナッジ配達 trigger。帰属は token 由来 (自己申告不可)。"""
        entry = {
            "from_id": from_bind.agent_id,
            "from_name": from_bind.name,
            "sent_at": time.time(),
            "message": message,
        }
        # 宛先の registered 確認と queue append を**同一ロックスコープ**で原子的に
        # 行う。移植元 spike は確認と append を別スコープに分けており、その間に
        # DELETE が走ると登録解除済み session に enqueue できる残存レースがあった。
        # 「DELETE 後は配送先から外す」(round 3 Major) verified intent を並行時にも
        # 守るため、確認+append を 1 スコープに統合する (codex self-review Major 対応)。
        # I/O (_journal) と PTY 注入 (_trigger_nudge) は従来どおりロック外に出し、
        # 非再入 Lock の二重取得デッドロック回避契約は維持する。
        with self._lock:
            target: "AgentBind | None" = None
            for b in self._binds.values():
                # registered な bind のみ配送先にする (未接続 / DELETE 済み
                # client への配送を防ぐ。codex review round 3 Major 対応)
                if b.revoked or not b.registered:
                    continue
                if b.agent_id == to_id or b.name == to_id:
                    target = b
                    break
            if target is None:
                return {"ok": False, "error": f"[peer_not_found] no agent '{to_id}'"}
            self._queues.setdefault(target.agent_id, []).append(entry)
        self._journal(
            "message_enqueued",
            from_id=from_bind.agent_id,
            to_id=target.agent_id,
            chars=len(message),
        )
        self._trigger_nudge(target)
        return {"ok": True, "delivered_to": target.agent_id}

    def drain(self, bind: "AgentBind") -> list[dict]:
        """at-most-once drain (Set D 2.3 継承)。"""
        with self._lock:
            msgs = self._queues.get(bind.agent_id, [])
            self._queues[bind.agent_id] = []
        if msgs:
            self._journal("queue_drained", agent_id=bind.agent_id, count=len(msgs))
        return msgs

    if TYPE_CHECKING:  # server が供給する配達トリガ (型チェッカ向け宣言)
        def _trigger_nudge(self, target: "AgentBind") -> None: ...
