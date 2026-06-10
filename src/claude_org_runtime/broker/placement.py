# -*- coding: utf-8 -*-
"""balanced-split placement: broker から dispatcher.choose_split を再利用する境界。

設計 SoT: docs/design/ja-migration-plan.md §8 Issue B (broker は terminal/ と
choose_split を一方向に使う側。ja は broker を import しない)。

依存方向 (一方向):
    broker.placement -> claude_org_runtime.dispatcher.runner.choose_split
broker は balanced-split ロジックを再実装せず、dispatcher runner の
:func:`~claude_org_runtime.dispatcher.runner.choose_split` を再利用する。

本フェーズのスコープ (確定: 浅い再利用):
    list_panes(dict) -> Pane.from_dict -> choose_split -> SplitChoice
の純関数ラッパまで。spawn フローへは結線しない。terminal adapter の ``spawn``
は ``new_window`` のみで「特定ペインを方向指定で分割」する面を持たないため、
split-target 対応の adapter 拡張 (深い統合) は本 Issue のスコープ外
(別 Issue 相当)。ここで提供するのは broker が将来 layout-aware spawn を
行う際の placement 計算の単一の出入口であり、import 契約のテスト対象でもある。

入力の pane dict は renga 由来の geometry (``id`` / ``x`` / ``y`` / ``width`` /
``height`` / ``role`` / ``name`` / ``focused``) を想定する。これは terminal
adapter の ``list_panes`` 出力 (``pane_id`` / ``left`` / ``top`` / ``active``)
とは別スキーマである点に注意 (placement は orchestration 層の pane 表を食う)。
"""

from __future__ import annotations

from typing import Any, Optional

from ..dispatcher.runner import Pane, SplitChoice, choose_split

__all__ = ["Pane", "SplitChoice", "choose_split", "choose_pane_split"]


def choose_pane_split(panes: list[dict[str, Any]]) -> Optional[SplitChoice]:
    """renga 由来の pane dict 列から次の balanced-split 先/方向を選ぶ。

    dispatcher runner の :func:`choose_split` をそのまま再利用する薄いラッパ。
    候補が無ければ ``None`` (SPLIT_CAPACITY_EXCEEDED 相当)。
    """
    parsed = [Pane.from_dict(p) for p in panes]
    return choose_split(parsed)
