# probe 6 実測結果 — Herdr workspace レイアウト配置決定性 (Issue #110 / 設計書 §11)

実行: `investigation/run_layout_probe.sh` (隔離 herdr 0.7.1 / 専用 XDG + session)。
実測日: 2026-07-03。ユーザの live herdr / 本番 broker には非接触。

| probe | 項目 | 実測 verdict | 設計への含意 |
|---|---|---|---|
| 6a | `agent.start {workspace,tab}` 尊重 | **IGNORED** (target=w2 だが focused=w1 に相乗り) | 戦略 A 不成立 |
| 6c | `pane.move` cross-workspace + id 保存性 | **MOVE_OK**: pane_id は**変わる** (`w1:p2`→`w2:p2`, id_preserved=False)、`pane.get.workspace_id` は移送先に更新、`terminal_id` は**保存** | 戦略 C 成立。move 後 verify 再実行 + broker bind re-key 必須 |
| 6d | `split` 方向尊重 | **STACKED** (split=down で y が異なる上下積み) | §8 per-space 分割方向 (control=上下) が効く |
| 6e | throwaway ws auto-close | **AUTO_CLOSED** (root pane close で workspace ごと消滅) | §7.4 root cleanup を実配置検証にゲート必須 |
| 6f | 非フォーカス ws 監視到達性 | **REACHABLE** (`pane.get`/`pane.read`/`pane.list` すべて可) | §9 poll 監視前提成立 |
| MS | multi-space 決定配置 | **MULTI_OK**: control(w3)/project(w4) を**別 workspace** へ配置、user(w1) focused でも両着地・観測可 | multi-space が戦略 C で成立 |

## 確定した配置戦略: C (spawn-then-move)

- `agent.start` は focused workspace に相乗りする (6a) が、`pane.move {destination:{type:tab, tab_id, split}}` で
  任意の owned workspace の tab へ決定的に移送できる (6c/MS)。
- `#115` が**単一** dedicated workspace で確立した Fix-C 移送を、**space_key ごとの複数 workspace の tab** へ
  ルーティングするだけで #110 の multi-space が成立する。設計書 §12「#110 は placement A/C どちらでも成立、
  C なら move 後 verify 再実行 + bind re-key」と完全一致。
- 実装上の帰結:
  - move で pane_id が変わる → adapter は post-move pane_id を PaneRef で返し、broker はそれを bind する
    (既存 `_reconcile_placement` は対応済み)。terminal_id は保存されるので Fix-D 権威 liveness は不変。
  - root pane cleanup は「agent が space の workspace に居ることを `pane.get` で確認後」に限定 (6e 対策、§7.4)。
  - 非フォーカス workspace も poll (`pane.list {workspace_id}`) で観測可 → 監視は poll ベース維持 (§9)。
