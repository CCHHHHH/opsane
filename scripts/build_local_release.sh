#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
OUTPUT_DIR="$ROOT_DIR/release"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
[[ -n "$PYTHON_BIN" ]] || { echo "Python 3.11+ not found" >&2; exit 1; }

cd "$ROOT_DIR"
VERSION="$($PYTHON_BIN -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
PACKAGE_NAME="Opsane-$VERSION-local"
STAGE_DIR="$OUTPUT_DIR/$PACKAGE_NAME"
WHEEL_DIR="$OUTPUT_DIR/.wheel-$VERSION"
SOURCE_DIR="$OUTPUT_DIR/.source-$VERSION"
ARCHIVE_PATH="$OUTPUT_DIR/$PACKAGE_NAME.zip"

rm -rf "$STAGE_DIR" "$WHEEL_DIR" "$SOURCE_DIR"
rm -f "$ARCHIVE_PATH" "$ARCHIVE_PATH.sha256"
mkdir -p "$STAGE_DIR/packages" \
  "$STAGE_DIR/templates/config/safety" \
  "$STAGE_DIR/templates/skills" \
  "$WHEEL_DIR" \
  "$SOURCE_DIR/config/safety" \
  "$SOURCE_DIR/skills/templates"

cp pyproject.toml "$SOURCE_DIR/"
cp .env.example "$SOURCE_DIR/"
cp -R shell_agent "$SOURCE_DIR/"
rm -rf "$SOURCE_DIR/shell_agent/web/frontend"
find "$SOURCE_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} +
cp config/*.yaml.example "$SOURCE_DIR/config/"
cp config/safety/*.yaml.example "$SOURCE_DIR/config/safety/"
cp skills/templates/*.yaml "$SOURCE_DIR/skills/templates/"

"$PYTHON_BIN" -m pip wheel "$SOURCE_DIR" --no-deps --wheel-dir "$WHEEL_DIR"
cp "$WHEEL_DIR"/*.whl "$STAGE_DIR/packages/"

cp packaging/local/install.command "$STAGE_DIR/"
cp packaging/local/start.command "$STAGE_DIR/"
cp packaging/local/stop.command "$STAGE_DIR/"
cp packaging/local/status.command "$STAGE_DIR/"
cp packaging/local/install.ps1 "$STAGE_DIR/"
cp packaging/local/start.ps1 "$STAGE_DIR/"
cp packaging/local/stop.ps1 "$STAGE_DIR/"
cp packaging/local/status.ps1 "$STAGE_DIR/"
cp packaging/local/README.md "$STAGE_DIR/README.md"

cp config/agent.yaml.example "$STAGE_DIR/templates/config/agent.yaml"
cp packaging/local/templates/config/credentials.yaml "$STAGE_DIR/templates/config/credentials.yaml"
cp packaging/local/templates/config/inventory.yaml "$STAGE_DIR/templates/config/inventory.yaml"
cp config/safety/env_policies.yaml.example "$STAGE_DIR/templates/config/safety/env_policies.yaml"
cp config/safety/safe_commands.yaml.example "$STAGE_DIR/templates/config/safety/safe_commands.yaml"
cp config/safety/forbidden_patterns.yaml.example "$STAGE_DIR/templates/config/safety/forbidden_patterns.yaml"
cp skills/templates/*.yaml "$STAGE_DIR/templates/skills/"
printf '%s\n' "$VERSION" >"$STAGE_DIR/VERSION"

chmod +x "$STAGE_DIR"/*.command

cd "$OUTPUT_DIR"
zip -qr "$ARCHIVE_PATH" "$PACKAGE_NAME"
shasum -a 256 "$ARCHIVE_PATH" >"$ARCHIVE_PATH.sha256"
rm -rf "$SOURCE_DIR" "$WHEEL_DIR"

printf 'Created %s\n' "$ARCHIVE_PATH"
du -h "$ARCHIVE_PATH"
