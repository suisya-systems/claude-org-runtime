# broker org-start 段1 (secretary 自身) folder-trust 機械承認の設計判断

設計主導タスク `broker-bootstrap-stage1-folder-trust-575` の成果物。
Refs: ja#575 (段1 の真の残存ギャップ評価), ja#576 (実端末無人実証; 本件に blocked),
ja#566 (spawn-ritual folder-trust 機械承認), ja#515 (Epic #6 broker dogfood)。

先行タスク #84 (`docs/broker-bootstrap-folder-trust-approval.md`) は bootstrap 3 段
(secretary / dispatcher / worker) の **承認主体と配線状況**を確定し、段1 を
「launcher 構造上 runtime では機械承認できない = out-of-band」と結論した。本ドキュメントは
その段1 に絞り、ja#575 が挙げた候補方向 (外部 pre-spawn ドライバ / org up を
spawn-not-exec に再構築 / 上流 config) を **一次裏取りした上で評価**し、in-tree で
viable な自動承認があるか、無いならどう扱うかを**設計判断として確定**する。

> **検証天井 (厳守)**: 本ワーカーのスコープは『設計 + launcher 上の local/unit spike』
> まで。実端末バックエンド (tmux / WezTerm) での無人 org up 実証は本件外 = ja#576
> (本件に blocked)。本ドキュメントは設計判断と一次裏取りを残し、実端末での
> end-to-end 検証は #576 に委ねる。

---

## 0. 結論 (TL;DR)

- **discriminating question の答え = (a) 純粋に構造的**。段1 が詰まるのは
  folder-trust 自体に genuine-user 検出があるからではなく、**`_launch_claude` が
  secretary プロセスそのものになる / それにブロックされ、その PTY に後から打鍵できる
  別プロセスが存在しない**ためである。「実プロンプトが automated CR を弾かない」側は
  本 diff では証明できない (ja#515 dogfood = 実端末の cross-repo 証拠に依拠。crux の
  実端末検証は ja#576)。in-tree で固定できるのは spawn→approve の **wire seam** まで
  (後述 §1)。
- **scope/天井 内で viable な in-tree 自動承認は無い**。候補を全て評価した結果:
  - 抑止 flag / settings / env: **存在しない** (in-tree 契約 + 一次調査)。
  - `~/.claude.json` への trust pre-seed: schema が未文書化・永続性が脆弱で
    **production 基盤にできない** (fragility 理由。security 理由ではない)。
  - daemon から secretary 端末へ `send_keys`: **不可能** (secretary bind は pane_id を
    持たず、tmux は isolated socket / wezterm は global mux で相関不能。後述 §3)。
  - org up を spawn-not-exec に再構築 (= controller がキーを送る): **ja `/org-start` に
    波及する cross-repo 変更** → 本ワーカーのスコープ外、escalation 案件 (後述 §4)。
  - local PTY-wrapper (secretary はそのまま、launcher が relay して prompt 検出で
    Enter を 1 回注入): **ja 中立だが** POSIX/Windows 非対称・crux が検証天井の外
    (後述 §5)。
- **採用する設計判断 = 段1 は全プラットフォームで意図的に human 1-Enter (production
  path)。文書化する** (本ドキュメント + `_launch_claude` の docstring)。これは
  ja#575 の正当な結論 (iii)。
- **sanctioned future mechanism**: 将来 fully-unattended org up が要件化したら、
  faithful な POSIX PTY-wrapper が承認された方向 (§5)。ただし着手には (a) ja#576 で
  実端末 crux 検証を優先する判断と、(b) Windows ConPTY 依存導入の architecture 判断
  (escalation) の双方が前提。これは本ドキュメントに埋め込んだ **escalation pointer**
  であり、本ワーカーの deliverable は「文書化された判断」であってコード変更ではない。

---

## 1. discriminating question の一次確認: (a) 構造的か / (b) genuine-user 検出か

ja#575 / 窓口が最初に答えるべきとした問い:

> 段1 の残存ギャップは (a) 純粋に構造的か (org up が secretary プロセスに exec するため
> 外部プロセスがキーボードを保持しないだけ)、(b) folder-trust 自体にも genuine-user
> 検出があって自動ドライバを弾くか。

**答え = (a)。** 根拠は 2 種に分けて、それぞれの証明強度を明示する
(in-tree で固定できる部分と、cross-repo / 実端末の証拠に依拠する部分を混同しない):

1. **段1 が詰まるのは launcher の構造である (in-tree で証明済 = load-bearing)**。
   secretary は launcher が exec/subprocess で become/block する foreground 子で、
   その PTY に後から打鍵できる第二のプロセスが構造的に存在しない (下記 §1-本文)。
   加えて secretary bind は pane_id を持たず daemon からも send_keys 不能 (§2-3)。
   この「runtime/launcher 視点で機械承認できない」は本 diff のコードと in-tree
   テストで再検証できる。
2. **spawn→approve の wire seam は in-tree で固定済 (FakeAdapter unit 契約)**。
   `tests/broker/test_bootstrap_folder_trust.py` は、spawn された pane (dispatcher /
   worker) が**安定名で `send_keys(target=<name>, enter=true)` addressable** であり
   CR が**その pane だけ**に届くことを固定する
   (`test_spawned_pane_is_machine_approvable_by_name_enter` /
   `test_dispatcher_approves_worker_same_seam` /
   `test_machine_approval_targets_only_named_pane`)。**ただしこれは FakeAdapter
   (`send_enter` が画面に `\n` を足す unit 契約) であり、実 Claude Code の
   folder-trust プロンプトを通していない** = wire seam の証明であって「実プロンプトが
   CR を受理する」ことの証明ではない。
3. **「実プロンプトに anti-automation は無い」は ja#515 dogfood に依拠 (cross-repo /
   実端末、本 diff では再証明しない)**。同一の folder-trust プロンプトが段2/段3 で
   raw `send_keys(enter=true)` により実 clear できたことは ja#515 dogfood (実端末) で
   実証済とされている。これと wire seam (#2) が揃うと、段2/段3 が機械承認できる
   理由が説明される。crux (real-prompt-detect + exactly-once Enter on real TTY) の
   実端末検証は ja#576 が owner。
4. **よって (a)**: 段1 のギャップは genuine-user 検出 (b) ではなく構造 (#1)。
   段2/段3 が承認できるのに段1 が承認できない差は、プロンプトの性質ではなく
   「daemon-spawned pane か / launcher が become する foreground 子か」という
   構造の差に帰着する。

詳細:

- **段1 が詰まるのは launcher の構造**。`_launch_claude`
   (`src/claude_org_runtime/broker/launcher.py:324-348`) は
   - POSIX: `os.execvpe(argv[0], argv, env)` で現プロセスを claude に置換 (親は消える)。
   - Windows: `subprocess.call(argv, env=env)` で前景ブロック。**stdin/stdout/stderr の
     kwargs を渡さない**ため子は launcher の制御端末を直接継承し、親は `call()` 内で
     ただ待つ。
   どちらも PTY を作らず、子の出力を観測したりキーを注入する seam がゼロ。
   (対照: detach daemon を起こす `_spawn_daemon` は `stdin/stdout/stderr=DEVNULL` を
   渡す。launcher は foreground 子に対してはそれをしない =
   launcher.py:223-234 と 343 の差。)

> **red herring の排除**: 「非対話 stdin への programmatic CR は効かない」系の懸念は
> 本件に**当たらない**。broker の `send_keys` は実 tmux/wezterm PTY に書き込むのであって
> headless stdin ではない。secretary も実 PTY (端末) を持っている。欠けているのは
> 「その PTY に書ける**第二のプロセス**」だけ — これが (a) の核心。

> **外部 issue/CVE の扱い**: 先行調査や下位 agent が挙げた anthropics/claude-code の
> issue 番号 / CVE 等は**未検証**であり、本ドキュメントの load-bearing な根拠には
> しない。判断は上記 in-tree 契約と「抑止 flag が文書化されていない」という頑健な事実に
> のみ依拠する (外部 ID の真偽に結論が左右されないようにするため)。

---

## 2. なぜ段1 だけ「自分で承認」できないのか (段2/段3 との非対称性)

| 段 | pane | 起こす主体 | adapter handle (pane_id) | 承認主体 | 承認可能か |
|---|---|---|---|---|---|
| 1 | secretary | `org up` (CLI, human) | **無し** (MCP token のみで登録) | — | **後から打鍵する別プロセスが無い** |
| 2 | dispatcher | secretary agent | 有り (daemon が spawn) | secretary agent | `send_keys(enter=true)` |
| 3 | worker | dispatcher agent | 有り (daemon が spawn) | dispatcher agent | `send_keys(enter=true)` |

段2/段3 の pane は **daemon が spawn する**ため adapter handle (pane_id) が bind に
付き、`resolve_target` で名前解決して `send_keys` できる。一方 secretary は
`admin_mint_token` で **MCP token だけ**発行され (pane_id は付かない)、daemon の
backend pane tree に存在しない。`bind_pane` (pane_id を bind に付ける唯一の経路) は
daemon 自身の spawn 経路からしか呼ばれない
(`src/claude_org_runtime/broker/server.py:813` / `:872`)。`resolve_target` の
name→bind 分岐は `b.pane_id is not None` を要求し (`server.py:359-361`)、
`_trigger_nudge` は pane_id が None なら早期 return する (`server.py:259`)。

つまり **secretary は daemon から send_keys 不能**であり、org up を実行した human の
ambient 端末も同様に daemon からは駆動できない (§3)。

---

## 3. 候補評価 (1): daemon から secretary 端末へ send_keys → 不可能

「daemon が外部 controller として secretary の folder-trust に Enter を送る」案は
backend を問わず成立しない:

- **tmux (isolated_session=True)**: daemon の adapter は専用 socket
  (`-L <SPIKE_SOCKET>`) で `list-panes` する (`terminal/tmux.py:81,84,94,113-115`)。
  human の ambient 端末は**その socket から見えない** = 解決不能 = send_keys 不能。
- **wezterm (isolated_session=False)**: `wezterm cli list` は global mux を返すので
  human の ambient pane は**見えるが匿名** (name/role=None)。secretary bind との
  相関手段が無く、`resolve_target` は handle / `_pane_meta` の名前 / pane_id 付き bind
  でしか引けない。相関は明示的に out of scope と文書化済
  (`server.py:408-414`)。

→ どちらも「daemon は un-spawned ambient pane を駆動できない」に帰着する。この案は
launcher の構造 (§1) を変えない限り無効。

---

## 4. 候補評価 (2): org up を spawn-not-exec に再構築 → cross-repo escalation

secretary を daemon-managed pane として起こせば adapter handle が付き、controller が
`send_keys(enter=true)` で folder-trust を承認できる。これは段2/段3 と同型になり
構造的には筋が通る。**しかし ja `/org-start` に実体のある波及がある** (一次確認済):

1. **identity 自己検出**: `/org-start` は `list_panes` の `focused=true` から自分を
   特定し `name/role == secretary` を期待、不一致なら
   `set_pane_identity(target="focused", ...)` で自動修復する
   (ja `org-start/SKILL.md:83-96`)。daemon-spawned secretary は focused pane とは
   限らず、broker では `target="focused"` が解決できない既知制約があり、id 明示や
   logical-pane fallback の分岐が要る。spawn 化はこの自己検出を一般化/置換せねばならない。
2. **env 継承と MCP bootstrap**: `/org-start` は renga 系では
   `RENGA_SOCKET`/`RENGA_PANE_ID` の継承に依存する (`SKILL.md:36,69`)。daemon-managed
   spawn 経路は env 注入を厳密に再現しないと MCP 呼び出しが壊れる。
3. **human-driven logical pane の bookkeeping**: broker では root secretary は
   geometry 全 0 / kind null の **logical pane** として現れ、`close_pane` は
   `[logical_pane]` で拒否、`inspect_pane` は不能 (`SKILL.md:93-96`)。
   adapter-backed pane になるとこの契約 (close/inspect の挙動、balanced-split target
   選出が secretary を特別扱いする `SECRETARY_MIN_*`) が変わる。

`/org-start` は「自分は human が `org up` (renga では `renga --layout ops`) で起こした
secretary TUI そのものであり、自分を起動はしない。子 (dispatcher/worker) だけを
spawn する」という前提に立っている (`SKILL.md:36,310`; `spawn-flow.md:74`)。
spawn-not-exec はこの前提を覆すため **ja 側 prompt の変更を伴う cross-repo 変更**であり、
本ワーカーのスコープ外 = **architecture escalation 案件**。

---

## 5. 候補評価 (3): local PTY-wrapper → ja 中立だが scope/天井 の外

`_launch_claude` (POSIX) が `os.execvpe` の代わりに PTY を介在させ
(`pty.fork` / `openpty` + select relay)、human の端末 ⇄ secretary を中継しつつ、
master 出力で folder-trust プロンプトを検出したら **CR を 1 回注入**し、以後は素通し
する案。secretary プロセスはそのまま走るので `/org-start` から見て透過。

- **ja 中立性 = true (条件付き)**: `/org-start` は「自分の stdin がどう供給されるか」を
  一切検査しない。依存するのは (i) renga/backend session で focused pane であること、
  (ii) env 継承、(iii) MCP 到達性のみ。**faithful な passthrough** はこの 3 つを保つので
  不可視。ただし中立性は relay が次を保つことが条件: TTY 性 (folder-trust プロンプトは
  real TTY でのみ出る。slave PTY が TTY でないと claude が非対話に落ちる)、
  winsize/SIGWINCH (list_panes geometry + TUI 描画)、env (`RENGA_SOCKET`/
  `RENGA_PANE_ID`/`TERM`/`COLORTERM`)、signal 転送 (Ctrl-C/SIGINT)、色/escape 透過。
  unfaithful な relay は human が触る secretary TUI を劣化させる。
- **feasibility 非対称 (決定的)**:
  - POSIX: stdlib `pty` + select relay で組める。**ただし** `_launch_claude` が
    process-replacement (`execvpe`) から **long-lived な relay 親**になり、startup だけ
    でなく **secretary セッション全体**の signal/exit-code 伝播が変わる。`_launch_claude`
    内に閉じるとはいえ 1 行では済まない実挙動変更。
  - Windows (本プロジェクトの既定 backend): stdlib に ConPTY が無く、**新規ネイティブ
    依存** (pywinpty 等) + platform 固有 relay が要る。かつ実 Claude Code に対して
    検証天井 (実端末無人 run 不可) の中では validate できない。
  → **POSIX-only spike は shipping platform (Windows) の段1 を解決しない**。
- **crux が検証天井の外**: この wrapper の load-bearing な核心は「real Claude Code の
  real TTY 上で**実プロンプトを検出**し**ちょうど 1 回 Enter**する」こと。これはまさに
  ja#576 (実端末実証) の領域で、本ワーカーは踏めない。plumbing だけ組んで crux を
  stub する spike は**false confidence**を生み、#566 で頭出しした thrash 失敗モードを
  再演する (brief が明示的に警告)。
- **fragility**: prompt 検出は Claude Code 内部文言 (版間で不安定) へのマッチ。
  早すぎる Enter (表示前取りこぼし) と二重 Enter (wrapper + agent/human) で空 turn 暴発、
  という `test_broker_spawn_does_not_auto_clear_trust` が意図的に防いでいる risk class を
  そのまま再導入する。
- **marginal value**: 段1 は human が `org up` を打った直後・端末の前に居る瞬間の
  secretary 自身のプロンプト。Enter 1 回は near-zero cost で、genuinely-unattended な
  段2/段3 (send_keys 自動化が正当) とは質的に異なる。

→ ja 中立な**機構は原理的に存在する**が、scope (cross-platform) と検証天井の双方を
満たす形では viable でない。

---

## 6. 採用する設計判断

**段1 (secretary 自身) の folder-trust は、全プラットフォームで意図的に human 1-Enter
とする (production path)。** これを本ドキュメントと `_launch_claude` の docstring で
文書化し、将来の保守者が「naive な blind Enter を launcher に足す」regression を
踏まないようにする (段2/段3 spawn 直後の blind auto-clear を禁ずる
`test_broker_spawn_does_not_auto_clear_trust` と同じ防御意図)。

理由の要約:
- 段1 のギャップは純構造的で (§1)、in-tree / scope 内で安全に閉じる手段が無い (§3-5)。
- human は `org up` 実行直後に端末の前に居るので Enter 1 回の cost は near-zero。
- 自動化を急いで PTY-wrapper を半端に入れると false confidence + thrash を招く (§5)。

### sanctioned future mechanism (escalation pointer)

将来 fully-unattended org up が要件化した場合の**承認された方向**は、faithful な
**POSIX PTY-wrapper** (§5) を `_launch_claude` 内に限定して導入すること。着手の前提:

1. **ja#576** で実端末 crux (real-prompt-detect + exactly-once Enter on real TTY) の
   検証を優先する判断。
2. **Windows ConPTY 依存導入の architecture 判断** (新規ネイティブ依存 = escalation)。
   Windows を解決しないなら POSIX-only は「既定 backend を残した partial fix」と明記する。

spawn-not-exec 再構築 (§4) は cross-repo (ja `/org-start`) 波及があるため、PTY-wrapper
より blast radius が大きく、独立の architecture 判断を要する (escalation)。

---

## 7. runtime が所有する範囲の verdict (#84 からの差分)

#84 は「段1 = out-of-band / launcher 構造上 runtime 不能」と結論した。本タスクはその
結論を**候補方向の網羅評価で裏取りし**、

- 「out-of-band」は **(a) 構造的**であることを一次確認 (§1)、
- in-tree 自動承認が scope/天井 内で無いことを候補別に確定 (§3-5)、
- 段1 = 意図的 human 1-Enter を **production 判断として確定・文書化** (§6)、
- fully-unattended が要件化した際の sanctioned mechanism (POSIX PTY-wrapper) と
  その escalation 前提を明示 (§6)、

まで進めた。runtime が所有する production path (段2/段3 の send_keys 機械承認
プリミティブ) は #84 のとおり既に正しく、本タスクで挙動変更は無い (docstring の
文書化のみ)。
