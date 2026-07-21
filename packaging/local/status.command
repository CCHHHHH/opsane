#!/usr/bin/env bash
set -euo pipefail

OPSANE_HOME="${OPSANE_HOME:-$HOME/.opsane}"
PORT="${OPSANE_PORT:-8010}"
PID_FILE="$OPSANE_HOME/run/opsane.pid"

if [[ -f "$PID_FILE" ]]; then
  OPSANE_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$OPSANE_PID" =~ ^[0-9]+$ ]] && kill -0 "$OPSANE_PID" 2>/dev/null; then
    printf 'Opsane 正在运行：PID %s，http://127.0.0.1:%s\n' "$OPSANE_PID" "$PORT"
    exit 0
  fi
fi

printf 'Opsane 当前未运行。\n'
exit 1
