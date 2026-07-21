#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPSANE_HOME="${OPSANE_HOME:-$HOME/.opsane}"
PORT="${OPSANE_PORT:-8010}"
OPSANE_BIN="$OPSANE_HOME/.venv/bin/opsane"
PYTHON_BIN="$OPSANE_HOME/.venv/bin/python"
PID_FILE="$OPSANE_HOME/run/opsane.pid"
LOG_FILE="$OPSANE_HOME/data/logs/opsane-console.log"
HEALTH_URL="http://127.0.0.1:$PORT/api/state"
APP_URL="http://127.0.0.1:$PORT/next/#/chat"

pause_if_tty() {
  if [[ -t 0 ]]; then
    printf '\n按 Enter 键关闭窗口...'
    read -r _
  fi
}

open_browser() {
  if [[ "${OPSANE_NO_BROWSER:-0}" == "1" ]]; then
    return 0
  fi
  if command -v open >/dev/null 2>&1; then
    open "$APP_URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$APP_URL" >/dev/null 2>&1 || true
  fi
}

is_healthy() {
  "$PYTHON_BIN" -c \
    'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=1).read()' \
    "$HEALTH_URL" >/dev/null 2>&1
}

if [[ ! -x "$OPSANE_BIN" ]]; then
  printf 'Opsane 尚未安装，请先运行 %s/install.command\n' "$SCRIPT_DIR" >&2
  pause_if_tty
  exit 1
fi

mkdir -p "$OPSANE_HOME/run" "$OPSANE_HOME/data/logs"

if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$EXISTING_PID" =~ ^[0-9]+$ ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    if is_healthy; then
      printf 'Opsane 已在运行：http://127.0.0.1:%s\n' "$PORT"
      open_browser
      pause_if_tty
      exit 0
    fi
    printf 'Opsane 进程 %s 仍在运行，但健康检查未通过。请查看：%s\n' "$EXISTING_PID" "$LOG_FILE" >&2
    pause_if_tty
    exit 1
  fi
  rm -f "$PID_FILE"
fi

printf '正在启动 Opsane...\n'
cd "$OPSANE_HOME"
nohup "$OPSANE_BIN" serve --host 127.0.0.1 --port "$PORT" >>"$LOG_FILE" 2>&1 </dev/null &
OPSANE_PID=$!
printf '%s\n' "$OPSANE_PID" >"$PID_FILE"

for _ in $(seq 1 40); do
  if is_healthy; then
    printf 'Opsane 已启动：http://127.0.0.1:%s\n' "$PORT"
    open_browser
    pause_if_tty
    exit 0
  fi
  if ! kill -0 "$OPSANE_PID" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

printf 'Opsane 启动失败，请查看日志：%s\n\n' "$LOG_FILE" >&2
tail -n 30 "$LOG_FILE" 2>/dev/null || true
pause_if_tty
exit 1
