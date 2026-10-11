# herdr 0.9.3 (protocol 22) の実測結果と broker E2E (Issue #237)

実行: `investigation/run_09_probe.sh`（中身は `herdr_09_probe.py`）。全セクションの結果は `_run_09/report.json` に、`--e2e-only` で再実行した E2E の結果は `_run_09/report_e2e.json` に出る。
- 隔離: 専用の `herdr --session herdr-0-9-spike` を使い、既定 session と稼働中の組織には触れていない。
- 固定化: `HERDR_CONFIG_PATH` で `version_check` / `manifest_check` を無効化し、実行中に版と検出 manifest が変わらないようにした。
- 実測日: 2026-10-11。
- 実行場所: worker の sandbox は AF_UNIX の bind を拒否するため、ホスト（窓口）で実行した。

## wire 差分 (v0.7.5 / protocol 17 → v0.9.3 / protocol 22)

調べた対象は、herdr upstream の各 tag の `src/protocol/wire.rs` と `src/api/schema/*.rs`、および同梱の `herdr api schema --json`。

| tag | protocol |
|---|---|
| 0.7.0-0.7.1 | 14 |
| 0.7.2-0.7.4 | 16 |
| 0.7.5 | 17 |
| 0.8.0 | 19 |
| 0.8.2 | 20 (0.8.1 は tag なし) |
| 0.9.0-0.9.3 | 22 |

18 と 21 はリリースされていない。

- `ping` の `protocol` は `wire.rs` の `PROTOCOL_VERSION` と同じ値を返す。
- 18〜22 の bump は TUI client/server 間の binary wire 向けで、内容は kitty key release / native key lifecycle / terminal bell の転送 / workspace group close / native key identity。JSON socket API は別の契約で、client shell 向けには `ENDPOINT_PROTOCOL_GENERATION = 1` を持つ。
- adapter が使う method は ping / workspace.create・list・close / pane.list・get・layout・split・move・close・read・send_text・send_keys・rename / agent.start。0.9.3 ではどれも request param、result の field、error code が 0.7.5 と同じで、変更は任意 field の追加だけだった。
  - 追加された任意 field: `workspace.create.source_workspace_id` / `pane.split.right_click` / `PaneInfo.restore_error` / `AgentInfo.completion_seq` / `pane.report_agent.resume_argv`。
  - JSON API の schema（`src/api` と `src/protocol`）には、どの tag にも `deny_unknown_fields` が無い。
- adapter の変更は「22 を `PROTOCOL_AGENT_API_V075` に入れる」ことだけ。`SUPPORTED_PROTOCOLS` だけに足すと、22 が legacy 経路に落ちて #151 の症状が再発する。
- 0.9.3 では `workspace.close` に `close_group` が増えた。worktree group に属する workspace を閉じるには `close_group: true` が要り、無いと `workspace_group_close_required` が返る。adapter が作る workspace は group に入らないので影響しない。

## 0.7.1 の spike 所見の再実測

| 項目 | 0.7.1 の所見 | 0.9.3 の実測 |
|---|---|---|
| `pane.send_keys` の語彙 | enter/return, tab, space, esc/escape, backspace/bs, 矢印, shift+tab, ctrl+a..z は accept。delete/del/home/end/pageup/pagedown は `invalid_key` | **同じ**。大文字小文字は区別しない (`Enter` / `ESCAPE`)。`C-c` / `alt+x` / `f1` / `f12` も accept。`backtab` / `ctrl-c` / `insert` / `page_up` は reject。混在リスト `["enter","home"]` は全体が `invalid_key` になる。raw tty の pane で測ったので、キーが pane を殺すことは無い (`pane_alive_after: true`) |
| events のロス | 1500 件の flood で 982 件が欠落し、通知も無い（サイレントロス） | **サイレントではなくなった**。200 件の burst は全件届いた。1500 件を読まずに溜めると、192 件届いたあと `{"error":{"code":"events_lost","message":"event subscription fell behind retained history; resubscribe and resync with session.snapshot"}}` が来て EOF になる。購読開始時に `subscription_started` の ack が届く。live event の名前は snake 形 (`workspace_renamed`) |
| `pane.list` の整合と reap | eventually consistent（生きている pane が一時的に欠落しうる） | split 直後に `pane.list` を呼ぶと 20/20 回とも新 pane が載っていた。`exit` 後は 0.57〜0.93 秒で `pane.list` と `pane.get` の両方から同時に消えた。今回のサンプルでは欠落は観測されなかったが、#109 の reap 閾値はそのまま残す（下記） |
| `agent.start` / `report_agent` | report_agent が受けるのは idle/working/blocked/unknown だけで、`done` は不可 | **同じ**（`done` は `invalid_request: unknown variant done`）。agent.start は即座に返る（`launch_pending: true`, `agent_status: unknown`）。約 2 秒で `agent: claude` が検出され、約 5 秒で `idle` になった。動作中の agent pane へ再度 start すると `agent_pane_busy`。名前違反は `invalid_agent_name`、kind 違反は `unsupported_agent_kind`、`timeout_ms: 3000` は `invalid_agent_timeout`。claude が `/exit` で終わっても pane は shell として残る |
| pane id の形式 | `w<ws>:p<n>`。ws segment に `_` を含みうる | 形式は同じで、`w2` / `w2:t1` / `w2:p1`。**pane 番号は 10 進数ではなく base-32 風の英数字**で、10 番目は `w2:pA`、以降 `pB`, `pC`, …, `pP` と続く（herdr の `encode_public_number`。0.7.5 から変わっていない）。`terminal_id` は `term_<hex>` |

reap 閾値（`reap_min_*`）は変えない。0.9.3 の `pane.list` は今回のサンプルで整合していたが、20 回の試行で eventual consistency が解消したとは言えない。また閾値が保守側に寄っていても実害は reap が数秒遅れるだけで、外すと誤 reap のリスクを取り戻すことになる。

### pane id の base-32 表記の影響（本 Issue の範囲外、follow-up）

`dispatcher/runner.py` の `_HERDR_PANE_ID = ^w[^:]+:p([0-9]+)$` は pane 番号を 10 進数と仮定している。このため **1 つの workspace で 10 個目以降の pane (`w1:pA` …) を拒否する**。この表記は 0.7.x から変わっていないので、protocol 22 で生じた問題ではない。

## broker E2E（MCP HTTP 経由、herdr backend）

| 操作 | 結果 |
|---|---|
| `spawn_claude_pane` | ok。`w1:p2` に立ち上がり、adapter の protocol は 22。dev-channel ダイアログは `spawn_prompt.decide` で承認し、6.0 秒で composer に到達した |
| `send_message` | ok (`delivered_to: e2e-worker`)。PTY nudge から worker の `check_messages`、worker の `send_message` を経て、root に `ACK-237` が 9.0 秒で届いた |
| `inspect_pane` | ok。claude の画面を取得できた |
| `send_keys` | text は ok。`Shift+Tab` で permission mode が切り替わった（footer が `manual mode on` に変わった）。`Home` は `[key_unsupported]`（想定どおり） |
| `close_pane` | ok。close 後の `list_panes` に pane は残っていない |

## 0.9.3 で他に変わった点（adapter への影響なし）

- （0.9.2 以降）新しい pane の環境から `CLAUDECODE` / `CLAUDE_CODE_*` / `TMUX` / `WEZTERM_PANE` 等が除去され、`TERM_PROGRAM=herdr` が設定される。adapter が `pane.split` の `env` で明示的に渡す値は、除去の後に適用されるので届く（E2E の broker token 受け渡しで確認した）。

実測した版は 0.9.3 だけである。0.9.0-0.9.2 も protocol は 22 で、adapter が使う JSON schema にも差が無い。adapter は protocol で判定しているので、これらの版も同じ経路で受理する。
