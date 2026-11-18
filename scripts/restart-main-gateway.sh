#!/usr/bin/env bash
# Restart only the main gateway from this repository.
# Does not call `nanobot restart`, which also restarts persona gateways.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$ROOT/.local/logs/restart-main-gateway.log"
CONFIG="$ROOT/.local/config.json"
WORKSPACE="$ROOT/.local/workspace"

main_is_ancestor() {
  local pid="$$"
  local seen=""
  local cmd
  while [[ "$pid" -gt 1 ]]; do
    case " $seen " in
      *" $pid "*) return 1 ;;
    esac
    seen="$seen $pid"
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$cmd" == *"$CONFIG"* && "$cmd" == *"nanobot"* ]]; then
      return 0
    fi
    pid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')"
    [[ -n "$pid" ]] || return 1
  done
  return 1
}

if [[ "${NANOBOT_RESTART_DETACHED:-0}" != "1" ]] && main_is_ancestor; then
  mkdir -p "$(dirname "$LOG")"
  NANOBOT_RESTART_DETACHED=1 "$ROOT/.venv/bin/python" - "$0" "$LOG" <<'PY'
import os
import subprocess
import sys

script, log_path = sys.argv[1], sys.argv[2]
env = os.environ.copy()
env["NANOBOT_RESTART_DETACHED"] = "1"
env["NANOBOT_ALLOW_SELF_STOP"] = "1"
with open(log_path, "a", encoding="utf-8") as handle:
    subprocess.Popen(
        [script],
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
print("main-only restart scheduled")
PY
  exit 0
fi

export NANOBOT_CONFIG="$CONFIG"
export NANOBOT_WORKSPACE="$WORKSPACE"
export NANOBOT_ALLOW_SELF_STOP=1
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
unset NANOBOT_PID_FILE NANOBOT_LOG_FILE || true
export NANOBOT_START_PERSONAS=0
cd "$ROOT"
"$ROOT/.venv/bin/nanobot" down --config "$CONFIG" --workspace "$WORKSPACE"
exec "$ROOT/start-nanobot-easy.sh"
