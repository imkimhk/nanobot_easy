#!/usr/bin/env bash
# Start only the main gateway from this repository. Does not start personas.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export NANOBOT_CONFIG="$ROOT/.local/config.json"
export NANOBOT_WORKSPACE="$ROOT/.local/workspace"
unset NANOBOT_PID_FILE NANOBOT_LOG_FILE || true
export NANOBOT_START_PERSONAS=0
exec "$ROOT/start-nanobot-easy.sh"
