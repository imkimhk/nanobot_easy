#!/usr/bin/env bash
# Start the main gateway and the five persona gateways from this repository.
# A gateway that is already running is left running.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$ROOT/.local/gateway-repo.sh" ]]; then
  echo "Persona helper not found: $ROOT/.local/gateway-repo.sh; no gateway was changed." >&2
  exit 1
fi
status=0

echo "starting main"
if ! "$ROOT/scripts/start-main-gateway.sh"; then
  echo "warning: main did not start" >&2
  status=1
fi

for name in rees elle genie heart testy; do
  echo "starting $name"
  if ! "$ROOT/.local/gateway-repo.sh" start "$name"; then
    echo "warning: $name did not start" >&2
    status=1
  fi
done

exit "$status"
