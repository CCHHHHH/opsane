#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/shell_agent/web/frontend"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

echo "[quality] Python unit and integration tests"
"$PYTHON_BIN" -m pytest -q "$ROOT_DIR/tests"

echo "[quality] Frontend type, component, build and isolated browser tests"
npm --prefix "$FRONTEND_DIR" run test:quality

echo "[quality] All quality gates passed"
