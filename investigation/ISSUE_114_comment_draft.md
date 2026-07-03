## 独立再現による root cause 裏取り + 修正設計案 (worker: herdr-misreap-investigation)

既投稿の root cause を、ユーザーの live server に非接触の**隔離 herdr 0.7.1 インスタンス** (専用 `XDG_CONFIG_HOME` + 専用 session) で独立再現し裏取りした。骨子は既投稿と一致。加えて **agent.start の配置セマンティクス・3 つの修正レバーを実測で確定**し、機序と重大度をいくつか訂正・引き上げる。

### 1. 症状名の訂正: これは「誤 reap」ではなく **isolation の全崩壊**

`agent.start` が dispatcher をユーザーの**実 workspace w1** に配置している以上、`isolated_session=True` が約束する「専用 workspace への隔離」は**毎回 no-op**。誤 reap はその一症状にすぎない。isolated 前提に依存する全ロジックが偽の土台に建つ:
- **専用 workspace が空実行される**: adapter は w2/w3/… を作るが agent は常に w1 に着地 → 専用 ws は root pane だけの空箱で、root cleanup 後 auto-close (churn)。
- **org 全操作から不可視**: dispatcher は adapter の `list_panes` (自 workspace 厳格フィルタ) に載らず、`list_panes_view` の補完は logical 限定 (`server.py:475-477`) なので非 logical の dispatcher は補われない → send_keys/close/inspect が `[pane_not_found]`。
- **org down が閉じられない**: `_close_managed_panes` は `list_panes` を反復 close する (`launcher.py:493-502`) が、載らない dispatcher は対象外 → **shutdown 後も孤児 TUI が w1 に残存**。
- **id 再利用ハザード (#109 と合流)**: 死んだ dispatcher の pane_id (w1:p2) がユーザーの新ペインに再利用されると、reap の `pane.close(w1:p2)` が**ユーザーの無関係ペインを誤爆**しうる。

### 2. 決定的前提: 「フォーカス済み既存 workspace の有無」

- **headless-no-client (startup workspace 無し) では再現しない**。`workspace.create` が w1 を作りフォーカスも付くため agent.start が正着地し事故らない。
- **ライブは TUI クライアント接続時に startup workspace を生成・フォーカス**する (`herdr-server.log`: `created startup workspace` + `workspace focused w1`)。この状態でのみ発生。→ Issue の「run 間で非決定的 (2 敗 1 生存)」の一因はこのフォーカス状態差。

独立再現ログ (`run_repro.sh --with-user-ws`):
```
simulated user startup workspace = 'w1' (focused)
workspace.create (dedicated) -> w2  focused: FALSE          # 専用 ws はフォーカスされない
agent.start (workspace=w2, split=down) -> pane_id='w1:p2'   # w2 指定を無視し focused w1 に着地 (DIVERGED)
pane.list per workspace: w1:[w1:p1, w1:p2]  w2:[w2:p1]      # *** dispatcher は adapter tracked w2 の list に元から居ない
pane.close(root w2:p1) -> ok
workspaces AFTER root close = ['w1']                        # w2 は空になり auto-close で消滅
pane.list(dedicated w2) -> ERROR workspace_not_found        # adapter.list_panes() は [] を返す
pane.close(dispatcher w1:p2) -> ok (closed_via=pane.close)   # 生存 dispatcher を kill = journal と一致
```
これは本番 `herdr-server.log` の事故列 (w2 create → **w2 closed** on root pane.close → pane.list error → `pane child exited pane_id=3 signal=Kill`) と 1:1 対応。同署名で 2/2 再現 (w1:p2 spawn→reap 51.9s / w1:p3 53.2s、いずれも `closed_via=pane.close`)。

### 3. 機序の訂正 (既投稿の因果を精密化)

- **主因は「strict workspace フィルタ」+ placement。workspace 消失は二次的**。dispatcher は agent.start で w1 に着地した時点で、adapter の `list_panes` の厳格フィルタ (`herdr.py:510` `p["workspace_id"] == self._workspace_id`) に**構造的に必ず落ちる**。root cleanup で w2 が auto-close しようがしまいが、dispatcher は adapter の list に元から現れない (上記再現ログ: root close 前でも `w2:[w2:p1]` のみ)。auto-close と `workspace_not_found` は症状を悪化させるが根本ではない。
- **`workspace_not_found`→空写像は 1 回で cache 化**。初回 `workspace_not_found` で `_workspace_id=None` にクリア (`herdr.py:499-502`)、以降 `list_panes` は socket を叩かず即 `[]` (`:485-486`)、次 spawn は `first=True` で ws 再作成 → w2→w3→w4→w5 と churn (server.log の pane.list error は事故ごとに **1 回だけ** = この機序と整合)。「pane.list{w2} を投げ続ける」ではない。
- **`still_present:false` は liveness の証拠として無価値**。`kill_pane_detailed` の `still_present` は `pane_exists`→`list_panes` (自 workspace フィルタ) で算出する (`herdr.py:677`)。w1 常駐ペインはこのフィルタに**絶対載らない**ため、**close が成功しようが失敗して生きていようが `still_present` は構造的に必ず False**。「今 close したから false」ですらなく、liveness としての価値はゼロ。
- **物理 close 検証の論理が転倒**。生存 w1:p2 に `pane.close` が成功 → `closed_via="pane.close"` ∈ `_REAP_CLOSE_EFFECTIVE` (`server.py:731-737`) → effective=True → `_cleanup_pane` で生存 dispatcher を kill (`:704-726`)。**「pane.close が成功した」を reap 正当の根拠にしているが、成功は pane が生きていた証拠**。
- **タイミングの因果を訂正**。reap 成立には `missing_since <= reaped-6s` が必要 (`server.py:690-694`)。w1:p2 は reaped `…056.47` に対し `missing_since <= …050.47`、これは registered `…051.02` より**前**。実際 server.log の最初の `pane.list`(c4) は spawn+40s (registration より前) で、ここで `missing_since` が確定。**「登録の 5 秒後に殺された」は見かけの因果**で、真は「boot 窓の reap sweep が spawn+40s で `missing_since` を確定 → 登録前後の broker 呼び出しバーストが `missing_count>=3` を満たし reap」。#112 のゲートは**恒常欠落に対する遅延にすぎず**、時間経過で必ず成立する。
- run 間非決定性は reap が request-driven (`resolve_target`/`_reserve_name` 契機、`server.py:388/959`) なことに由来。generic pane w1:p4 は spawn→reap 175s と大きくばらつき (request 頻度依存) これを裏付ける。w1:p5 の生存は「潜在孤児」で、次の spawn/resolve バーストで reap されうる (実際 w1:p4 は w1:p5 spawn の入口 reap で巻き添え)。

### 4. agent.start の配置セマンティクス (実測)

| 条件 | 着地先 |
|---|---|
| dedicated 非フォーカス + `split:down` / split 省略 / `focus:true` | いずれも **focused ws (w1)** に相乗り |
| **`workspace.focus(dedicated)` を事前呼び出し** | **dedicated (w2) に正着地** ✅ |

→ herdr `agent.start` の配置は **workspace/tab パラメータではなく現在フォーカスで決まる** (0.7.1 実測)。制御手段は `workspace.focus` の事前呼び出しのみ。この version-specific な挙動が診断の load-bearing fact であり、再現アーティファクトの生ログ (workspace.create=w2 / agent.start=w1:pN の並置) で確証済み。

### 5. 修正レバー (実測で実現性確認) と破綻ケース

- **Fix-A (placement / 事前 focus)**: agent.start 前に `workspace.focus(dedicated)` → 正着地を再現。**placement と churn を両方直す** (dispatcher が dedicated に残り空にならない → auto-close しない)。要件: (i) 事前に現フォーカス workspace を取得し agent.start 後 `try/finally` で復帰 (失敗時の focus 奪取放置を防ぐ)、(ii) 復帰先取得 RPC が stale だと誤 workspace 復帰、(iii) 人間の TUI 操作との focus race (`_spawn_lock` は同 adapter の spawn 直列化のみで人間とは競合)。難点は spawn ごとの一過性 focus flicker。
- **Fix-C (placement / pane.move、focus 奪取なし)**: agent.start 後に `pane.move(pane_id, {"destination":{"type":"tab","tab_id":dedicated_tab,"split":"down"}})` で相乗り pane を dedicated へ移送 → 再現。`terminal_id` 保持 = プロセス不再起動。**要件 (これを満たさないと誤 reap 再発)**: (i) 移送で pane_id が `w1:pN→wDED:pM` に変わるため PaneRef は **post-move id** を返し、(ii) `_workspace_id` を移送先 (dedicated) に一致させ、(iii) move を **root cleanup より前**に実行 (先に root を閉じると dedicated が空→auto-close し移送先 tab が消える)。
- **Fix-D (liveness 防御 / placement と独立)**: reaper の bookkeeping 削除前 liveness を **workspace 非依存の `pane.get(pane_id)`** に。実測: tracked ws の `pane.list{w2}` が `workspace_not_found` でも `pane.get(w1:p2)` は **ALIVE**、`pane.get(w9:p9)` は `pane_not_found` (権威)。**要件**: (i) `pane.get` は net-new かつ isolated 境界を越えるため **herdr 専用メソッド + `getattr` フォールバック** (tmux/wezterm は `pane.get` 相当が無く従来の即時 reap を維持)、(ii) **pane_id 再利用対策**: spawn 時に記録した `terminal_id` と `pane.get` の `terminal_id` を照合し、不一致 (id 再利用) or `pane_not_found` のときのみ reap。さもなくば死んだ dispatcher の id が再利用されると reap が永久 defer → `_cleanup_pane` 未実行で **#106/#112 が潰した幽霊 name binding が復活**。
- **Fix-D 単独では不十分**: placement を直さないと「殺される」が「不可視の孤児」に置換されるだけ (§1: list_panes_view 不可視・org down で閉じられない)。placement 修正と**必ず併用**。
- **Fix-E (upstream)**: agent.start の workspace/tab 無視が仕様か bug か herdr へ確認。尊重されれば adapter は現行設計で直る。

### 6. 設計評価と推奨

- **既投稿案1「実配置 rebind」は単独採用不可**: `_workspace_id` をユーザーの w1 に rebind すると `list_panes{w1}` がユーザー pane も返し、isolated backend の org down が list_panes **全 pane を close** する経路 (`launcher.py:497`) で**ユーザー pane 巻き添え close**。placement を専用 ws 側に寄せる A/C が安全。
- **全修正は冪等に**: 診断は「0.7.1 が agent.start の workspace を無視する」に依存する。将来 herdr が honor したら Fix-C の post-move は二重移送になりうる。**実着地を検証し、diverged のときのみ発火**する形にして version 跨ぎの退行を防ぐ。

**推奨の組み合わせ**:
1. **placement 修正 (必須・主)**: **Fix-A** (事前 focus、`try/finally` + 事前フォーカス取得で堅牢化) か **Fix-C** (pane.move、id remap 追跡込み) のいずれか。両者とも placement + churn を直す。トレードオフ: A は実装単純だが spawn ごとに TUI focus flicker、C は focus 奪取無しだが pane_id remap の追跡が複雑。**マルチエージェント org では spawn が連続し flicker が反復するため、TUI 体験を重視するなら C、実装単純さ・冪等性を重視するなら A**。
2. **liveness 防御 (必須・従)**: **Fix-D** (`pane.get` liveness、**terminal_id 照合で id 再利用ガード**、herdr 専用 + getattr フォールバック)。placement 修正のリグレッション/将来の list 喪失への最終防御。
3. `list_panes` の `workspace_not_found`→benign 空+binding クリア写像 (`herdr.py:491-502`) の見直し (degraded を空と偽らない)。
4. **Fix-E**: upstream 報告 (並行、runtime は 1-3 で自立)。

### 再現手順

隔離インスタンスで再現するスクリプトを worker dir に用意 (実装ブランチへ移植可):
```bash
bash investigation/run_repro.sh --with-user-ws              # 事故を再現 (DIVERGED + workspace_not_found + 生存 pane close)
bash investigation/run_repro.sh --with-user-ws --focus-fix  # Fix-A (事前 focus) で正着地することを確認
```
