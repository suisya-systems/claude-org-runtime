# Issue #114 調査結果 (worker: herdr-misreap-investigation)

- タスク: `herdr-misreap-investigation` / Refs #114 #109 #112
- スコープ: 既投稿 root cause (ja secretary セッション) の**独立検証・裏取り** + 修正設計案。実装はスコープ外。
- **人間向けの詳細分析・修正設計は `ISSUE_114_comment_draft.md`** (Issue 貼付用) に集約。本ファイルは成果物索引 + 再現手順 + 検証メモ。

## 成果物 (worker dir `investigation/`)

| ファイル | 用途 |
|---|---|
| `ISSUE_114_comment_draft.md` | **主成果物**。真因裏取り + 機序訂正 + 重大度引き上げ + 修正設計案 (Fix-A/C/D/E) + 推奨 |
| `run_repro.sh` | 隔離 herdr server を起動し再現ハーネスを走らせるランナー (`--with-user-ws` / `--focus-fix`) |
| `herdr_repro.py` | adapter の spawn RPC 列を raw socket で再走し reap 連鎖を実測 |
| `herdr_placement_probe.py` | agent.start の配置セマンティクス実測 (split 有無 / focus / pane.move) |
| `herdr_fix_probe.py` | 修正案の単一呼び出し実現性 (agent.start focus / pane.move destination) |

再現コマンド (ユーザーの live server に非接触・隔離インスタンス):
```bash
bash investigation/run_repro.sh --with-user-ws              # 事故を再現
bash investigation/run_repro.sh --with-user-ws --focus-fix  # Fix-A (事前 focus) で正着地を確認
```

## 真因 (独立 herdr 0.7.1 で再現・裏取り、既投稿と一致)

一言で: **herdr `agent.start` が `workspace`/`tab` を無視しフォーカス中の workspace (ユーザーの w1) に agent を配置 → adapter の専用 workspace 隔離 (isolated_session) が毎回 no-op → dispatcher が adapter の自 workspace 厳格フィルタ (`herdr.py:510`) に構造的に載らず → broker の list ベース liveness が「恒常欠落」と誤認 → #112 のゲート (age12/miss3/6s) は恒常欠落に無力で遅延後に生存 dispatcher へ能動 `pane.close`。**症状は「誤 reap」だが本質は isolation の全崩壊。**

## 検証で確定した主要事実 (コード行 + 実測)

1. **配置はフォーカス駆動** (実測): agent.start は split 有無・`focus:true` に依らず focused ws に着地。制御は事前 `workspace.focus` のみ。
2. **主因は strict フィルタ + placement、workspace 消失は二次的** (`herdr.py:510`): dispatcher は着地時点で adapter の list に構造的に落ちる。root close 前でも `pane.list{w2}=[w2:p1]` のみ (再現ログ)。
3. **`workspace_not_found`→空写像は 1 回で cache 化** (`herdr.py:485-502`): `_workspace_id=None` クリア後は socket を叩かず `[]`。server.log の pane.list error が事故ごと 1 回だけなのと整合。churn は w2→w3→w4→w5。
4. **`still_present` は構造的に必ず False** (`herdr.py:677`, `:510`): w1 常駐ペインは自 workspace フィルタに絶対載らないため、close 成否に無関係に False。liveness 価値ゼロ。
5. **物理 close 検証の論理が転倒** (`server.py:704-737`): 生存ペインで `pane.close` 成功→ `_REAP_CLOSE_EFFECTIVE`→kill。「成功」は生きていた証拠。
6. **タイミングの因果訂正**: `missing_since` は boot 窓の sweep (server.log c4 pane.list = spawn+40s、登録より前) で確定。「登録+5s で死亡」は見かけ。ゲートは恒常欠落への遅延にすぎない。
7. **request-driven 非決定性** (`server.py:388/959`): reap 起点は resolve_target/_reserve_name のみ。w1:p4 の spawn→reap 175s、w1:p5 生存 (潜在孤児) がこれを裏付ける。
8. **Fix-D 単独は不可視孤児化** (`server.py:475-477`, `launcher.py:493-502`): 非 logical dispatcher は list_panes_view で補完されず org 不可視・org down で閉じられない。

## 修正レバー (実測で実現性確認)

- **Fix-A** 事前 `workspace.focus(dedicated)` → 正着地 (再現)。placement + churn 両方直す。要 try/finally focus 復帰・事前フォーカス取得。難点: TUI focus flicker。
- **Fix-C** agent.start 後 `pane.move(pane, {"destination":{"type":"tab","tab_id":ded_tab,"split":"down"}})` (再現)。要 post-move id 返却・`_workspace_id` 同期・move を root cleanup 前。focus 奪取なし。
- **Fix-D** reaper liveness を workspace 非依存 `pane.get(pane_id)` に (再現)。要 herdr 専用 + getattr フォールバック・**terminal_id 照合で id 再利用ガード** (幽霊 binding 復活防止)。placement 修正と必ず併用。
- **Fix-E** upstream 報告。

## 却下 / 冪等性

- 既投稿案1「実配置 rebind」単独: `list_panes{w1}` がユーザー pane を返し org down (`launcher.py:497` 全 close) で巻き添え → 却下。
- 全修正は「実着地を検証し diverged 時のみ発火」の冪等形にし、将来 herdr が workspace/tab を honor した場合の退行 (Fix-C 二重移送等) を防ぐ。

## 検証状況

- 独立 herdr 0.7.1 インスタンスで事故を 100% 再現 (`--with-user-ws`)。`--focus-fix` で Fix-A の正着地を確認。
- Fix-C (pane.move)・Fix-D (pane.get liveness) を独立に実測確認。
- 既存テスト green: `PYTHONPATH=src /usr/bin/python3 -m pytest tests/terminal/test_herdr.py tests/broker/` → 293 passed。
- 敵対的セルフレビュー (別 agent、コード + queue.jsonl 突合) を実施し、機序の訂正 (主因=strict filter / still_present 構造的 False / timing / 重大度=isolation 崩壊) と修正案の破綻ケース (Fix-C の id 同期・Fix-D の id 再利用・Fix-D 単独の孤児化) を本結論に反映済み。
