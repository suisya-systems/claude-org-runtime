#!/usr/bin/env bash
# Issue #114 修正の end-to-end 検証ランナー (/verify 相当)。
#
# run_repro.sh と同じ隔離戦略 (専用 XDG_CONFIG_HOME + 専用 session、ユーザの
# ~/.config/herdr / live server に非接触) で headless herdr を起動し、実 HerdrAdapter
# (Fix-C + Fix-D 込み) を herdr_fix_verify.py で駆動する。
#
# usage: investigation/run_verify.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_SRC="$(cd "$HERE/.." && pwd)/src"
RUN_DIR="$HERE/_run_verify"
mkdir -p "$RUN_DIR"
SESSION="hv$$"
export XDG_CONFIG_HOME="$(mktemp -d /tmp/hvx.XXXX)"
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
PYTHONPATH="$REPO_SRC" /usr/bin/python3 "$HERE/herdr_fix_verify.py" "$SOCK" "$@"
RC=$?
echo "[run] verify rc=$RC"
exit $RC
