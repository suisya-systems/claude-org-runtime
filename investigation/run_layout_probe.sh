#!/usr/bin/env bash
# probe 6 (Issue #110 workspace layout 配置決定性) の隔離ランナー。
# run_verify.sh と同じ隔離戦略 (専用 XDG_CONFIG_HOME + 専用 session) で headless
# herdr を起動し、ユーザの live herdr / ~/.config/herdr に非接触で probe を駆動する。
#
# usage: investigation/run_layout_probe.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$HERE/_run_layout"
mkdir -p "$RUN_DIR"
SESSION="lp$$"
export XDG_CONFIG_HOME="$(mktemp -d /tmp/lpx.XXXX)"
SOCK="$XDG_CONFIG_HOME/herdr/sessions/$SESSION/herdr.sock"

echo "[run] XDG_CONFIG_HOME=$XDG_CONFIG_HOME session=$SESSION"

cleanup() {
  echo "[run] stopping herdr server (session=$SESSION)..."
  HERDR_SESSION="$SESSION" herdr server stop >/dev/null 2>&1
  if [[ -n "${SRV_PID:-}" ]]; then kill "$SRV_PID" >/dev/null 2>&1; fi
}
trap cleanup EXIT

herdr --session "$SESSION" server >"$RUN_DIR/server.log" 2>&1 &
SRV_PID=$!
for i in $(seq 1 50); do
  [[ -S "$SOCK" ]] && break
  sleep 0.2
done
if [[ ! -S "$SOCK" ]]; then
  echo "[run] ERROR: socket did not appear. server.log tail:"; tail -20 "$RUN_DIR/server.log"; exit 1
fi
sleep 1  # startup workspace の生成を待つ
/usr/bin/python3 "$HERE/herdr_layout_probe.py" "$SOCK" "$@"
RC=$?
echo "[run] probe rc=$RC"
exit $RC
