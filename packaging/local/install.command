#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPSANE_HOME="${OPSANE_HOME:-$HOME/.opsane}"
VENV_DIR="$OPSANE_HOME/.venv"

pause_if_tty() {
  if [[ -t 0 ]]; then
    printf '\n按 Enter 键关闭窗口...'
    read -r _
  fi
}

fail() {
  printf '安装失败：%s\n' "$1" >&2
  pause_if_tty
  exit 1
}

find_python() {
  local candidate
  for candidate in python3.12 python3.11 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

copy_if_missing() {
  local source="$1"
  local target="$2"
  if [[ ! -e "$target" ]]; then
    cp "$source" "$target"
  fi
}

PYTHON_BIN="$(find_python)" || fail "需要 Python 3.11 或更高版本。"
WHEEL_PATH="$(find "$SCRIPT_DIR/packages" -maxdepth 1 -type f -name '*.whl' -print -quit)"
[[ -n "$WHEEL_PATH" ]] || fail "安装包中没有找到 Opsane wheel。"

printf 'Opsane 本地安装\n'
printf 'Python: %s\n' "$PYTHON_BIN"
printf '数据目录: %s\n\n' "$OPSANE_HOME"

mkdir -p \
  "$OPSANE_HOME/config/safety" \
  "$OPSANE_HOME/data/logs" \
  "$OPSANE_HOME/data/session_files" \
  "$OPSANE_HOME/skills/templates" \
  "$OPSANE_HOME/run"
chmod 700 "$OPSANE_HOME" "$OPSANE_HOME/config" "$OPSANE_HOME/data" "$OPSANE_HOME/run" 2>/dev/null || true

copy_if_missing "$SCRIPT_DIR/templates/config/agent.yaml" "$OPSANE_HOME/config/agent.yaml"
copy_if_missing "$SCRIPT_DIR/templates/config/credentials.yaml" "$OPSANE_HOME/config/credentials.yaml"
copy_if_missing "$SCRIPT_DIR/templates/config/inventory.yaml" "$OPSANE_HOME/config/inventory.yaml"
copy_if_missing "$SCRIPT_DIR/templates/config/safety/env_policies.yaml" "$OPSANE_HOME/config/safety/env_policies.yaml"
copy_if_missing "$SCRIPT_DIR/templates/config/safety/safe_commands.yaml" "$OPSANE_HOME/config/safety/safe_commands.yaml"
copy_if_missing "$SCRIPT_DIR/templates/config/safety/forbidden_patterns.yaml" "$OPSANE_HOME/config/safety/forbidden_patterns.yaml"

while IFS= read -r skill_file; do
  copy_if_missing "$skill_file" "$OPSANE_HOME/skills/templates/$(basename "$skill_file")"
done < <(find "$SCRIPT_DIR/templates/skills" -maxdepth 1 -type f -name '*.yaml' -print)

chmod 600 "$OPSANE_HOME/config/agent.yaml" \
  "$OPSANE_HOME/config/credentials.yaml" \
  "$OPSANE_HOME/config/inventory.yaml" 2>/dev/null || true

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  printf '正在创建独立 Python 环境...\n'
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

printf '正在安装 Opsane 及运行依赖，首次安装需要访问 Python 软件源...\n'
PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_DIR/bin/python" -m pip install --upgrade "$WHEEL_PATH"

[[ -x "$VENV_DIR/bin/opsane" ]] || fail "opsane 命令安装失败。"
cp "$SCRIPT_DIR/VERSION" "$OPSANE_HOME/VERSION"

printf '\n安装完成。\n'
printf '双击 start.command，或在终端执行：%s/start.command\n' "$SCRIPT_DIR"
printf '用户配置和会话数据保存在：%s\n' "$OPSANE_HOME"
pause_if_tty
