#!/usr/bin/env bash
set -euo pipefail

OPSANE_HOME="${OPSANE_HOME:-$HOME/.opsane}"
PID_FILE="$OPSANE_HOME/run/opsane.pid"

pause_if_tty() {
  if [[ -t 0 ]]; then
    printf '\n按 Enter 键关闭窗口...'
    read -r _
  fi
}

if [[ ! -f "$PID_FILE" ]]; then
  printf 'Opsane 当前未运行。\n'
  pause_if_tty
  exit 0
fi

OPSANE_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ ! "$OPSANE_PID" =~ ^[0-9]+$ ]] || ! kill -0 "$OPSANE_PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  printf 'Opsane 当前未运行，已清理过期状态。\n'
  pause_if_tty
  exit 0
fi

printf '正在停止 Opsane（PID %s）...\n' "$OPSANE_PID"
kill "$OPSANE_PID"
for _ in $(seq 1 20); do
  if ! kill -0 "$OPSANE_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    printf 'Opsane 已停止。\n'
    pause_if_tty
    exit 0
  fi
  sleep 0.5
done

printf 'Opsane 未在 10 秒内退出，请检查进程 %s。\n' "$OPSANE_PID" >&2
pause_if_tty
exit 1
