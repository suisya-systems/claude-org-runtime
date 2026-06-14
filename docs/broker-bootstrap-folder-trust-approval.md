# broker org-start: bootstrap folder-trust machine-approval

調査主導タスク `broker-bootstrap-folder-trust-approve` の成果物。
Refs: ja#566 (verify/auto-wire spawn-ritual 3-3b folder-trust machine-approval),
ja#515 (Epic #6 broker dogfood, delivery cycle PASS).

`ORG_TRANSPORT=broker` 下で **fully-unattended org-start** を主張するには、
spawn された Claude pane が初回起動時に出す **folder-trust プロンプト**
("Do you trust the files in this folder?") を、人手の Enter 無しで機械承認できる
必要がある。本ドキュメントは「どの spawn 段で / 誰が / どのキーで」承認するかを
確定し (verify)、各段が配線済みかを示し、実端末での end-to-end 検証手順を残す。

> **検証天井**: 本ドキュメントと付随する pytest は wire + local/unit までを
> カバーする。実端末バックエンド (tmux / WezTerm) での fully-unattended
> spawn -> approve -> deliver の e2e dogfood は本スコープ外 (別途人間調整の
> dogfood run = 「実端末 e2e 検証手順」節の手順書として残す)。

---

## 0. 結論 (TL;DR)

- folder-trust プロンプトを抑止する **CLI flag / settings キー / 環境変数は存在しない**
  (Claude Code 公式 docs + 未解決 feature request anthropics/claude-code#29285 で確認)。
  `--permission-mode bypassPermissions` / `--dangerously-skip-permissions` も
  folder-trust には**無効** (tool-permission とは別系統)。
- trust 受諾は `~/.claude.json` に per-directory で保存されるが、**home directory
  起動だと disk 非永続 = session 限り**になり spawn 毎に再発する
  (ja#566 の「`~/.claude.json` is not cached so it recurs per spawn」の正体)。
- したがって機械承認の**唯一の手段は `send_keys(enter=true)`** (PTY に CR=0x0D を
  送出。tmux / WezTerm 両 adapter で確認済)。
- 3 つの bootstrap 段のうち **dispatcher / worker は既に agent 駆動で配線済**、
  **secretary 自身は launcher の構造上 send_keys 不能 = out-of-band 承認**が本質。
- ja#515 dogfood の caveat1 は、dogfood の **minimal driver (手書きハーネス) が
  3-3b を自動化していなかった**ことに由来する運用上の穴であり、runtime/agent の
  defect ではない (本物の org-start プロンプトは Block D-1 で自動化済)。

---

## 1. なぜ folder-trust が spawn 毎に再発するのか

Claude Code の folder-trust は permission system とは独立の「このフォルダを信頼するか」
という初回ゲートである。公式ドキュメント (Security / CLI reference / env-vars /
permission-modes) を確認した結果:

| 機構 | folder-trust への効果 |
|---|---|
| `--permission-mode bypassPermissions` | 無効 (tool-permission のみ) |
| `--dangerously-skip-permissions` | 無効 (tool-permission のみ) |
| settings.json の任意キー | 抑止キーは**存在しない** |
| 環境変数 | 抑止用は**存在しない** |
| `~/.claude.json` の per-dir trust | home dir 起動だと**非永続**、project subdir なら永続 |

`~/.claude.json` への永続化が効かない (home dir 起動 / 書き込み不能な場所) 場合、
プロンプトは spawn の度に再発する。broker は spawn 時にこれを auto-clear しない
ため、何らかの主体が毎回 Enter を送らねばならない。**抑止 flag が存在しない以上、
機械承認 = `send_keys(enter=true)` 一択**である。

`send_keys(enter=true)` が CR を送ることの確認 (byte-identical machine approval):

- tmux: `send-keys -t <pane> Enter`
  (`src/claude_org_runtime/terminal/tmux.py:183-185`)
- WezTerm: `send-text <pane> "\r" --no-paste` (CR=0x0D, bracketed-paste 回避)
  (`src/claude_org_runtime/terminal/wezterm.py` `send_enter`)
- broker surface: `send_keys(enter=true)` -> `Broker.send_keys_to` -> `adapter.send_enter`
  (`src/claude_org_runtime/broker/server.py:469-505`、`enter` は seq 末尾の `"enter"` に
  畳まれ adapter.send_enter にディスパッチ)

---

## 2. bootstrap の 3 段とそれぞれの承認主体

`org up` を起点とする broker org-start は、folder-trust を出す Claude pane を
段階的に 3 つ起こす。各段の「spawn する主体」と「承認する主体」は次のとおり。

```
[human or unattended driver]
        │ org up (CLI)
        ▼
   (段1) secretary pane   ← _launch_claude が exec/subprocess で起動
        │ /org-start (Block A: spawn_claude_pane)
        ▼
   (段2) dispatcher pane  ← secretary エージェントが spawn
        │ DELEGATE -> spawn-flow 3-2: spawn_claude_pane
        ▼
   (段3) worker pane      ← dispatcher エージェントが spawn
```

### 段1: secretary 自身 (org up が起動)

- 起動経路: `org up` -> `launcher.org_up` -> `launch(argv)` = `launcher._launch_claude`
  (`src/claude_org_runtime/broker/launcher.py:324-348`)。
- POSIX は `os.execvpe(argv[0], argv, env)` で**現プロセスを claude に置換** (以後返らない)。
  Windows は `subprocess.call(argv, env=env)` で**前景ブロック**。
- いずれの経路でも launcher は secretary プロセス**そのものになる / それにブロックされる**
  ため、**launcher から secretary pane へ `send_keys` する手段が構造的に無い**
  (自分自身の PTY に後から打鍵する経路が無い)。
- 抑止 flag も存在しない (§1)。
- => **段1 の folder-trust は runtime (launcher) では機械承認できない**。
  - human が `org up` を実行する通常運用では human が 1 回 Enter する。
  - fully-unattended で `org up` を自動起動する場合は、`org up` を pane 内で起こして
    **外部ドライバがその pane へ Enter を送る** out-of-band 承認が必要
    (launcher の外側の関心事)。

### 段2: dispatcher (secretary が spawn)

- 起動経路: secretary の `/org-start` が `mcp__org-broker__spawn_claude_pane`
  (`name="dispatcher"`) を発火 (ja `.claude/skills/org-start/SKILL.md` Block A)。
- broker の `Broker.spawn_claude` (`src/.../broker/server.py:740`) は
  `--mcp-config <broker>` を注入して `adapter.spawn` するが、**folder-trust を
  auto-clear しない** (spawn 後に Enter を送らない)。承認は呼び出し元 agent に委譲。
- 承認主体: **secretary エージェント**が
  `mcp__org-broker__send_keys(target="dispatcher", enter=true)` を送る。
  - 配線箇所: ja `.claude/skills/org-start/SKILL.md` **Block D-1**
    (ja@`6dddbce`, line 203 = Enter 送信 / line 207 = broker 注記。再検証可能な
    抜粋は §6)。broker 注記が「初回プロンプトは folder-trust に変わるが手順は
    同型で `send_keys(target="dispatcher", enter=true)` で機械承認」と明記。
  - 待ち合わせ: Enter 送信後、`list_peers` poll で dispatcher が未登録なら
    **Enter を再送**する (Block D-1 line 203 末尾。boot 速度でプロンプト未表示の
    段階の Enter は no-op になりうるため)。boot-not-registered の失敗分岐では
    最大 3 回 retry してダメなら fatal (Block D-2 失敗分岐, SKILL.md line 258)。
- => **段2 は agent 層 (ja org-start プロンプト) で配線済**。段2 を spawn する
  runtime コードは無い (dispatcher の spawn は org-start プロンプトが直接行う)。
  runtime は承認に使う `send_keys_to` / `send_enter` プリミティブを提供する (§1)。

### 段3: worker (dispatcher が spawn)

- 起動経路: dispatcher が DELEGATE 受信後 `spawn_claude_pane` (`name="worker-{task_id}"`)。
- 承認主体: **dispatcher エージェント**が
  `mcp__org-broker__send_keys(target="worker-{task_id}", enter=true)`。
- **runtime 層の配線**: dispatcher の action plan を生成する runtime helper
  `claude_org_runtime.dispatcher.runner.build_plan()` が、`after_spawn` に
  **`send_keys(target=worker, enter=True)` を 1 回**入れる
  (`src/claude_org_runtime/dispatcher/runner.py:677-706`)。順序は
  `poll_events(pane_started)` -> `send_keys(enter=True)` -> `list_peers`
  (peer 出現を ~30s retry) -> `send_message`。
  - Enter は **単発** (再送 retry は Enter そのものでなく後続の `list_peers`
    peer-wait 側にある)。プロンプト未表示の段階で送ると no-op になりうる点は
    ja org-start Block D-1 と同じで、failure 時の Enter 再送は agent 判断に委ねる。
  - **reason 文言の正確化 (本 PR で修正済)**: この after_spawn の `reason` は
    元々 `"approve 'Load development channel?' Y/n prompt"` と **renga 旧文言のみ**で、
    broker では実際には folder-trust プロンプトを承認する文言ドリフトがあった。
    挙動 (Enter 送出) は元から正しく、文言のみの cosmetic だが、両 transport を
    正確に表すよう `"approve the spawn-ritual prompt (renga: 'Load development
    channel?' Y/n / broker: folder-trust)"` に更新した
    (人間承認の上、本タスクスコープ内の 1 行修正)。
- 配線箇所 (agent プロンプト): ja `.dispatcher/references/spawn-flow.md` **3-3b**
  (ja@`6dddbce`, line 115-125。抜粋は §6)。broker 注記が folder-trust への
  読み替えと同型承認を明記。
- => **段3 は runtime helper (after_spawn) + agent プロンプト (3-3b) の両層で
  配線済** (brief の既知事項)。

### まとめ表

| 段 | pane | spawn する主体 | 承認する主体 | 承認キー | 配線状況 |
|---|---|---|---|---|---|
| 1 | secretary | `org up` (CLI) | human / 外部ドライバ (out-of-band) | Enter | launcher 構造上 runtime 不能 |
| 2 | dispatcher | secretary agent | secretary agent | `send_keys(enter=true)` | agent: ja org-start Block D-1 |
| 3 | worker | dispatcher agent | dispatcher agent | `send_keys(enter=true)` | runtime: `dispatcher.runner` after_spawn + agent: ja spawn-flow 3-3b |

---

## 3. runtime が所有する範囲の verdict

runtime (この repo) が所有するのは launcher.py (org up/down) と broker server / surface /
terminal adapter である。folder-trust 機械承認に対する runtime の責務は:

1. **機械承認プリミティブの提供**: `send_keys(enter=true)` -> `send_keys_to` ->
   `adapter.send_enter` (CR)。両 backend で実装・確認済。**配線済**。
2. **agent 駆動承認のための surface**: dispatcher/secretary tier に `send_keys` を公開
   (`src/.../broker/surface.py` の `_OPS_TOOLS`)。**配線済**。

段2/段3 の機械承認は「agent が上記プリミティブを呼ぶ」形で ja プロンプトに配線済。
段1 (secretary 自身) は launcher が exec/subprocess で secretary になるため runtime
では承認不能 = out-of-band。

=> **runtime が所有できる範囲の production path は既に正しい**。
ja#515 caveat1 は minimal driver が 3-3b を自動化していなかった運用上の穴であり、
runtime defect ではない。

---

## 4. 実端末 e2e 検証手順 (別途人間調整の dogfood; 本ワーカースコープ外)

fully-unattended の主張には実端末での spawn -> approve -> deliver 完走が要る。
以下は再現手順書 (本ワーカーは実行しない。検証天井の外)。

### 前提

- WSL2 / Linux + tmux (POSIX 正準) もしくは Windows + WezTerm。
- isolated state-dir (repo 外推奨)、daemon は非既定ポートで bind。
- claude は対話 TUI (課金中立厳守、`-p`/`--print`/headless 不可)。

### 段1 (secretary) の承認 — out-of-band

1. unattended なら `org up` を pane 内で起こす (例 tmux):
   ```bash
   tmux new-session -d -s org -c <root_cwd> \
     'python -m claude_org_runtime.broker up --state-dir <state> --backend tmux'
   ```
2. secretary pane に folder-trust が出るのを待ち、外部ドライバから Enter を送る:
   ```bash
   # 画面に "Do you trust the files in this folder" が出るまで poll してから:
   tmux send-keys -t org Enter
   ```
   (human 運用ならこの 1 回を人手で Enter)。

### 段2 (dispatcher) の承認 — agent 駆動

3. secretary の `/org-start` が dispatcher を spawn (Block A)。
4. secretary が Block D-1 に従い、dispatcher pane の folder-trust を
   `mcp__org-broker__send_keys(target="dispatcher", enter=true)` で承認。
   - 反映タイミング: プロンプトは boot 数秒後に出る。早すぎる Enter は no-op に
     なりうるため、Block D-1 は「Enter 送信 -> `list_peers` poll で未登録なら
     **Enter 再送**」で待ち合わせる (SKILL.md line 203 末尾)。boot-not-registered
     の失敗分岐は最大 3 回 retry してダメなら fatal (SKILL.md line 258)。
5. `list_peers` に `name="dispatcher"` が現れることを確認。

### 段3 (worker) の承認 — agent 駆動

6. dispatcher が DELEGATE で worker を spawn (3-2)。
7. dispatcher が 3-3b に従い
   `mcp__org-broker__send_keys(target="worker-{task_id}", enter=true)` で承認。
8. `list_peers` に worker が現れ、`send_message` -> `check_messages` で本文が届くことを確認。

### 完走判定

- 段1〜3 すべてが human の in-cycle 打鍵 0 で承認され (段1 は外部ドライバ)、
  delivery cycle (enqueue -> nudge -> check_messages で body 到達) が成立すれば
  fully-unattended PASS。
- body 到達は host channel mcp-log (`~/.cache/claude-cli-nodejs/<slug>/mcp-logs-*/`) の
  ZodError 不在で判定する (ja dogfood runbook の手法)。

---

## 5. 既知の制約 / 申し送り

- **段1 (secretary) の fully-unattended 化は runtime の外**: `org up` を pane 化して
  外部ドライバが Enter を送る運用設計が必要。launcher に send_keys を足しても
  exec/subprocess の先には届かない。将来 `org up` 自体を broker-managed pane として
  起こす設計に変える場合は別 issue。
  - **段1 の設計判断は別ドキュメントで深掘り済 (ja#575)**:
    [`broker-bootstrap-stage1-folder-trust-design.md`](broker-bootstrap-stage1-folder-trust-design.md)
    が候補方向 (daemon→secretary send_keys / spawn-not-exec 再構築 / local
    PTY-wrapper / trust pre-seed / 抑止 flag) を網羅評価し、段1 = 意図的 human
    1-Enter を production 判断として確定、fully-unattended が要件化した際の
    sanctioned mechanism (faithful POSIX PTY-wrapper) と escalation 前提を明示する。
- **broker.spawn_claude での auto-clear は意図的に未実装**: spawn 直後の blind Enter は
  (a) プロンプト表示前で取りこぼす、(b) agent 側 Block D-1/3-3b と二重 Enter になり
  空 turn を暴発させる、リスクがある。承認は「画面を見て出てから 1 回」が正しく、
  これは agent 駆動 (Block D-1/3-3b の retry 付き待ち合わせ) が担う。
- **`dispatcher.runner` after_spawn の reason 文言ドリフト (本 PR で修正済)**:
  `src/claude_org_runtime/dispatcher/runner.py` の worker spawn 用 `send_keys`
  step は reason が renga 旧文言 (`"approve 'Load development channel?' Y/n
  prompt"`) のみで broker の folder-trust を表さない文言ドリフトがあった。挙動
  (Enter 送出) は元から正しい cosmetic だが、両 transport を正確に表す文言に更新
  した (人間承認の上、本タスクスコープ内)。dispatcher は Enter を無条件発火し
  reason 文字列に依存しないため挙動への影響は無い。
- 抑止 flag が公式に入れば (anthropics/claude-code#29285) secretary 段も settings で
  解消できる。それまでは send_keys / out-of-band が唯一手段。

---

## 6. 再検証可能な証跡 (ja citations)

本ドキュメントが行番号で参照する ja プロンプトは別リポジトリ
(`claude-org-ja`) にあり、本 worktree には含まれない。再検証のため pin と抜粋を
残す。

- **ja repo**: `claude-org-ja`
- **commit**: `6dddbcebd9500d6e818fdf8a3f9dba73a96b224d` (`6dddbce`)

### 段2: `.claude/skills/org-start/SKILL.md` Block D-1 (line 203-210)

```
1. **Enter を送信** — Claude Code 初回起動時の「Load development channel? (Y/n)」
   プロンプトを承認する:
       mcp__renga-peers__send_keys(target="dispatcher", enter=true)
   - Enter は CR (0x0D) として PTY に書き込まれる
   - boot 速度によりプロンプト未表示の段階で Enter を送信すると no-op になる
     場合がある。次の list_peers poll で peer 登録が確認できなければ Enter を再送する
   - broker (ORG_TRANSPORT=broker) の場合: ... 初回プロンプトは「Load development
     channel?」ではなく Claude Code の folder-trust プロンプトに変わるが、承認手順は
     同型で mcp__org-broker__send_keys(target="dispatcher", enter=true) で機械承認する。
2. list_peers を poll し dispatcher の peer 登録を確認
```

(boot-not-registered の失敗分岐で「Enter を再送 → 再 poll。3 回 retry してダメなら
fatal」は同 SKILL.md line 258。)

### 段3: `.dispatcher/references/spawn-flow.md` 3-3b (line 115-125)

```
### 3-3b. 「Load development channel?」プロンプトを Enter で承認
spawn_claude_pane は内部で --dangerously-load-development-channels server:renga-peers
を付与するため、初回起動で Y/n 確認プロンプトが出る。Enter で承認する:
    mcp__renga-peers__send_keys(target="worker-{task_id}", enter=true)
Enter は CR (0x0D) として PTY に書き込まれる (byte-identical to renga append_enter)。

> broker (ORG_TRANSPORT=broker) の場合: ... 初回プロンプトは ... Claude Code の
> folder-trust プロンプト (「Do you trust the files in this folder?」相当) に変わるが、
> 承認手順は同型で mcp__org-broker__send_keys(target="worker-{task_id}", enter=true)
> で機械承認する。
```
