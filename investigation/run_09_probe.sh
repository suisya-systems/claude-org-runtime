#!/usr/bin/env bash
# herdr 0.9.x (protocol 22) re-measurement + broker E2E runner (Issue #237).
#
# Starts a dedicated `herdr --session herdr-0-9-spike` server (never the default
# session), runs herdr_09_probe.py against it, then stops the server and deletes
# the session. Update/manifest checks are disabled through HERDR_CONFIG_PATH so
# the binary and the claude detection manifest stay fixed during the run.
#
# usage: investigation/run_09_probe.sh [--skip-e2e | --e2e-only]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_SRC="$(cd "$HERE/.." && pwd)/src"
RUN_DIR="$HERE/_run_09"
mkdir -p "$RUN_DIR"
SESSION="herdr-0-9-spike"
CFG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
SOCK="$CFG_HOME/herdr/sessions/$SESSION/herdr.sock"

cat >"$RUN_DIR/config.toml" <<'EOF'
onboarding = false
[update]
version_check = false
manifest_check = false
EOF
export HERDR_CONFIG_PATH="$RUN_DIR/config.toml"

if [[ -S "$SOCK" ]]; then
  echo "[run] ERROR: session $SESSION already has a socket ($SOCK); refusing to reuse it"; exit 1
fi

cleanup() {
  echo "[run] stopping herdr server (session=$SESSION)..."
  herdr --session "$SESSION" server stop >/dev/null 2>&1
  if [[ -n "${SRV_PID:-}" ]]; then wait "$SRV_PID" 2>/dev/null; fi
  herdr session delete "$SESSION" >/dev/null 2>&1 \
    && echo "[run] session $SESSION deleted" \
    || echo "[run] WARN: session delete failed; run: herdr session delete $SESSION"
}
trap cleanup EXIT

herdr --version | tee "$RUN_DIR/version.txt"
herdr --session "$SESSION" server >"$RUN_DIR/server.log" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
  [[ -S "$SOCK" ]] && break
  sleep 0.2
done
if [[ ! -S "$SOCK" ]]; then
  echo "[run] ERROR: socket did not appear. server.log tail:"; tail -20 "$RUN_DIR/server.log"; exit 1
fi
sleep 1
PYTHONPATH="$REPO_SRC" python3 "$HERE/herdr_09_probe.py" "$SOCK" "$RUN_DIR/report.json" "$@"
RC=$?
echo "[run] probe rc=$RC report=$RUN_DIR/report.json"
exit $RC
