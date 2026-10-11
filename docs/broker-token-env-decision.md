# Broker token: env reference where the launch path can pass env safely

**Status:** Decided and shipped (#218); extended to tmux and wezterm (#222).
**Refs:** #218, #222 (measurements in its two comments), #168, `docs/channel-delivery-model-decision.md` §4.4 / §6.5.

## Decision

The broker agent token moves out of the MCP config and into the launching process's env, **only on launch
paths that can set that env without leaving the token in shell history or on a pane's screen, and without
keeping it on a command line for the life of the process**. On those paths the config says `"Authorization": "Bearer ${ORG_BROKER_TOKEN}"` and the token
travels as `ORG_BROKER_TOKEN`. Every other path keeps writing the token into the config, as before.

`mcp_config_for(token, token_env=True)` emits the reference. Callers opt in: the admin RPCs `mint_token` and
`adopt_delivery` take `token_env: true` (and `adopt_delivery` then returns the rotated `token` beside the
config), and `spawn_claude_pane` asks the terminal adapter's `token_via_pane_env` flag. Without the opt-in the
output is unchanged, so claude-org-ja and other admin callers keep working.

## Why

What Claude Code 2.1.296 does, from the #218 measurements:

- `${VAR}` and `${VAR:-default}` in `headers` are expanded in `-p` and in interactive mode, from the env of the
  process that starts.
- Expansion happens once, when the config loads. `--resume`, `--fork-session`, `--continue` and `/mcp`
  reconnect do not replay the original launch env. A changed env is not picked up either.
- An unset variable does not fail the config load. The placeholder is sent literally.
- `--mcp-config` is not stored with the session. Only a launcher that passes the file again replays it.

So a session that replays only the config (a fork or resume of a broker session, or a hand-run
`claude --mcp-config <secretary-mcp.json>`) sends `Bearer ${ORG_BROKER_TOKEN}`. The broker resolves no bind
for that string and answers **401** (`tests/broker/test_control_plane.py::test_unexpanded_token_placeholder_is_unauthenticated`).
That closes the pull door of `channel-delivery-model-decision.md` §4.4 on those paths: replaying the config no
longer gives a process the full token. This is the same non-replay property the observer lease already relies
on (§6.5.1).

Accepted residual (#222): on tmux and wezterm, env rides the launcher's command line (`tmux new-session -e K=V`,
`env K=V claude ...`). That line is readable through `ps` / `/proc` only while the launcher runs, a moment at
startup. A literal token in the config sits in claude's own argv for the whole life of the process, so env is
strictly shorter exposure. #218 first held these back; #222 reversed that on this comparison.

## Launch paths

| Path | How claude starts | How env reaches claude | Token | Reason |
|---|---|---|---|---|
| `org up` (POSIX) | `os.execvpe` | exec env | **env** | No command line, no shell, nothing typed. |
| `org up` (Windows) | `subprocess.call(env=)` | process env | **env** | Same. |
| `org up` fallback when `claude` is not found | human types the printed line | `ORG_BROKER_TOKEN=...` prefix in the printed line | env (printed) | Known limitation: the line shows the token. Before #218 it showed it inside the inline config, so exposure is unchanged. |
| `org adopt` | same as `org up` | exec env | **env** | Needs a daemon with #218. An older daemon ignores `token_env`, returns a literal config and no `token`, and the launch still authenticates. |
| `spawn_claude_pane`, herdr | `pane.split` / `agent.start` API | `env` param over the herdr socket | **env** | `token_via_pane_env = True`. Under herdr 0.7.5 the command line is typed into the pane, and with env the typed line carries only `${ORG_BROKER_TOKEN}` instead of the token. |
| `spawn_claude_pane`, tmux | `tmux new-session -e K=V` | the tmux client's argv | **env** | `token_via_pane_env = True` (#222). Residual: the token is in the tmux client's argv for the instant of launch. It is no longer in claude's argv. |
| `spawn_claude_pane`, wezterm | `wezterm cli spawn -- env K=V ...` (Windows: `cmd /c set "K=V"&& ...`) | the `env` / `cmd` argv | **env** | Same (#222). Residual: the token is in the `env` (or `cmd`) argv for the instant before it execs claude. |
| claude-org-ja on renga (`tools/transport.py`) | ja passes `--mcp-config` in renga `spawn_claude_pane` `args[]`; renga types the command into the pane's shell | renga has no per-pane env; a `VAR=... claude` prefix would be typed, shown and kept in history | literal | Same. ja calls `mint_token` without `token_env` and gets the old config. |
| other admin `mint_token` callers | caller-defined | - | literal | Opt-in only. |
| `broker serve` manual token print | human | - | literal | Debug output; it prints the token by design. |

On the literal paths the token stays in claude's argv for the life of the process, as it did before #218.
Adding a backend: set `token_via_pane_env = True` on its adapter when env reaches claude with a shorter
exposure than a literal config (a startup-only launcher argv counts) and add it to
`tests/broker/test_delivery.py::test_token_via_pane_env_is_declared_only_where_env_skips_the_command_line`.

## What changes for operators

- `secretary-mcp.json` written by `org up` no longer contains the agent token. On its own it does not
  authenticate. A hand-run `claude --resume` with that file gets 401 on `org-broker`. Use `org adopt --resume`,
  which re-supplies the env. (The file still holds the channel sidecar's delivery credential. That one is
  fenced by the observer lease and is out of scope here.)
- Inside a session started this way, the token is in the process env and is inherited by its children, for
  example Bash tool commands. Before #218 the same token was readable from argv and from the config file.

## Regression check

The behaviour above is undocumented and was measured on one version. With `CLAUDE_ORG_RUNTIME_LIVE_CLAUDE=1`
and an authenticated `claude` on PATH, `tests/test_live_claude_mcp_env_expansion.py` checks the two
properties the design needs: the header is expanded from the process env, and an unset variable is sent
literally. Run it when bumping the Claude Code version that the org pins.

Not covered by the live test: `/mcp` reconnect caching (it would need an interactive session), Windows
(not measured), and `/branch` while an MCP server is disconnected.
