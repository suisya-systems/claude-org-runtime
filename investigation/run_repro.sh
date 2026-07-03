#!/usr/bin/env bash
# Issue #114 再現ランナー: 完全隔離した herdr サーバを起動し、再現ハーネスを走らせる。
#
# 隔離戦略: 専用 XDG_CONFIG_HOME (worker dir 配下の _run/) を使い、ユーザーの
# ~/.config/herdr (live server / default socket) には一切触れない。専用 session 名
# のため socket path も独立する。終了時にサーバを stop する。
#
# usage: investigation/run_repro.sh [--focus-fix]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$HERE/_run"
mkdir -p "$RUN_DIR"
SESSION="hr$$"
# AF_UNIX sun_path は ~108 bytes 上限。worker dir は深いので XDG は短い /tmp 配下に置く
# (herdr の config/socket 置き場でしかなく、ユーザーの ~/.config/herdr とは完全独立)。
export XDG_CONFIG_HOME="$(mktemp -d /tmp/hrx.XXXX)"
SOCK="$XDG_CONFIG_HOME/herdr/sessions/$SESSION/herdr.sock"

echo "[run] XDG_CONFIG_HOME=$XDG_CONFIG_HOME"
echo "[run] session=$SESSION"
echo "[run] expected socket=$SOCK"

cleanup() {
  echo "[run] stopping herdr server (session=$SESSION)..."
  HERDR_SESSION="$SESSION" herdr server stop >/dev/null 2>&1
  if [[ -n "${SRV_PID:-}" ]]; then kill "$SRV_PID" >/dev/null 2>&1; fi
}
trap cleanup EXIT

# headless server を専用 session で起動 (spike と同じ起動法)
herdr --session "$SESSION" server >"$RUN_DIR/server.log" 2>&1 &
SRV_PID=$!
echo "[run] server pid=$SRV_PID, waiting for socket..."

for i in $(seq 1 50); do
  [[ -S "$SOCK" ]] && break
  sleep 0.2
done
if [[ ! -S "$SOCK" ]]; then
  echo "[run] ERROR: socket did not appear. server.log tail:"
  tail -20 "$RUN_DIR/server.log"
  exit 1
fi
echo "[run] socket ready. running harness..."
sleep 1  # startup workspace の生成を待つ

/usr/bin/python3 "$HERE/herdr_repro.py" "$SOCK" "$@"
RC=$?
echo "[run] harness rc=$RC"
echo "[run] === isolated server.log tail ==="
tail -40 "$RUN_DIR/server.log"
exit $RC
