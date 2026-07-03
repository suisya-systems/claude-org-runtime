# -*- coding: utf-8 -*-
"""send_keys の raw-key 語彙: canonical 定義と正規化 (単一の正本)。

設計 SoT: docs/design/renga-decoupling.md §4.7 (adapter 境界と能力表) /
runtime Issue #108 (raw-key vocabulary 拡張)。事前 Codex design review 確定事項:

- **正規化は broker/surface 側で一元化** (確定事項 (3)): schema が受理する
  エイリアス表記 (Esc/Escape・Enter/Return・Shift+Tab/BackTab・Delete/Del・
  Ctrl+A-Z 等) を **canonical 形へ畳む**責務は本モジュールが持つ。adapter は
  canonical のみを受け取り、自 backend の語彙へマップする。未知キー名は surface で
  -32602 (:func:`normalize_key` が ``None`` を返す)。
- **三重管理 drift の抑止** (確定事項 (6)): canonical 定義 / schema description /
  validation / backend map が別々に育つと drift する。本モジュールが canonical の
  唯一の出所となり、surface の validation・schema と各 adapter の backend map は
  ここから導出 / 突き合わせる (テストで包含関係を固定する)。

canonical 形は「小文字・単一表記」で統一する:
  enter / tab / backtab / esc / backspace / delete /
  up / down / left / right / home / end / pageup / pagedown / space /
  ctrl+a .. ctrl+z

Ctrl 系のみ ``ctrl+<letter>`` と ``+`` を残す (26 個を列挙するより形が明快で、
既存 surface 語彙 / renga golden shape と一致するため)。
"""

from __future__ import annotations

# canonical な ``ctrl+<letter>`` を生成する元 (A-Z を小文字で)。
CTRL_LETTERS = "abcdefghijklmnopqrstuvwxyz"

# canonical キーの全集合。adapter の ``supported_named_keys`` はこの部分集合を宣言し、
# backend map (tmux/herdr) の key はこの集合に含まれねばならない (テストで固定)。
CANONICAL_KEYS: frozenset[str] = frozenset(
    {
        "enter",
        "tab",
        "backtab",
        "esc",
        "backspace",
        "delete",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
        "space",
    }
    | {f"ctrl+{c}" for c in CTRL_LETTERS}
)

# 受理する入力表記 (小文字化後) -> canonical。schema が説明するエイリアス
# (Enter/Return, Shift+Tab/BackTab, Esc/Escape, Delete/Del) をここで畳む。
# 値は必ず :data:`CANONICAL_KEYS` の要素 (テスト ``test_alias_values_are_all_canonical``)。
_ALIAS_TO_CANONICAL: dict[str, str] = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "shift+tab": "backtab",
    "backtab": "backtab",
    "esc": "esc",
    "escape": "esc",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "space": "space",
    **{f"ctrl+{c}": f"ctrl+{c}" for c in CTRL_LETTERS},
}

# 契約上有効な入力キー名の集合 (= :data:`_ALIAS_TO_CANONICAL` の定義域から導出)。
# surface の語彙検証は :func:`normalize_key` (= _ALIAS_TO_CANONICAL 参照) が一次で行い、
# 本集合はその「受理される表記一覧」を外部へ公開する派生ビュー (drift 監視テストが
# ``ACCEPTED_KEY_NAMES == frozenset(_ALIAS_TO_CANONICAL)`` を固定する)。
ACCEPTED_KEY_NAMES: frozenset[str] = frozenset(_ALIAS_TO_CANONICAL)


def normalize_key(name: str) -> str | None:
    """入力キー名を canonical 形へ畳む。未知なら ``None``。

    大文字小文字と前後空白を無視する (``"Shift+Tab"`` / ``" ESC "`` を受理)。
    surface は ``None`` を -32602 (unknown key name) に写像する。
    """
    return _ALIAS_TO_CANONICAL.get(name.strip().lower())
