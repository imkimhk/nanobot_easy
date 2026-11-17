#!/usr/bin/env bash
# Restart the main gateway and the five persona gateways from this repository.
# Persona processes are stopped by PID, not by process group.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$ROOT/.local/gateway-repo.sh" ]]; then
  echo "Persona helper not found: $ROOT/.local/gateway-repo.sh; no gateway was changed." >&2
  exit 1
fi
LOG="$ROOT/.local/logs/restart-all-gateways.log"
CONFIG="$ROOT/.local/config.json"

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
  "$ROOT/.venv/bin/python" - "$0" "$LOG" <<'PY'
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
print("all-gateway restart scheduled")
PY
  exit 0
fi

status=0
echo "restarting main"
if ! NANOBOT_RESTART_DETACHED=1 "$ROOT/scripts/restart-main-gateway.sh"; then
  echo "warning: main did not restart" >&2
  status=1
fi

for name in rees elle genie heart testy; do
  echo "restarting $name"
  if ! "$ROOT/.local/gateway-repo.sh" restart "$name"; then
    echo "warning: $name did not restart" >&2
    status=1
  fi
done

exit "$status"
