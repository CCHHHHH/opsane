# Opsane Windows x64 Portable

This package is for 64-bit Windows 10/11. It contains Opsane, Python, and all
Python runtime dependencies. The target computer does not need Python, pip,
Node.js, Rust, or administrator access.

## Start

1. Extract the complete ZIP to a writable directory.
2. Double-click `start.cmd`.
3. Opsane opens `http://127.0.0.1:8010/next/#/chat` in the default browser.

Do not run `Opsane.exe` directly for normal use. `start.cmd` initializes the
portable data directory, starts the background service, performs a health
check, and opens the browser.

Use `status.cmd` to check the process and `stop.cmd` before moving, upgrading,
or deleting the portable directory.

## Data

All mutable files are stored under the adjacent `data` directory:

```text
data/
  config/               LLM, SSH, inventory, and safety configuration
  data/shell_agent.db   Sessions, tasks, memories, and audit records
  data/session_files/   Files uploaded in conversations
  data/logs/            Application and launcher logs
  skills/templates/     Editable Skill templates
```

The portable ZIP contains only blank configuration templates. It does not
contain the builder's API key, SSH credentials, server inventory, sessions, or
uploaded files.

To upgrade, stop Opsane and keep the existing `data` directory. Extract the new
version to a new directory and copy the old `data` directory into it.

## Troubleshooting

- If Windows SmartScreen appears, verify the ZIP SHA-256 before choosing to run
  it. The current portable executable is not code-signed.
- If port 8010 is occupied, open Command Prompt and run:
  `set OPSANE_PORT=8011` followed by `start.cmd`.
- Startup logs are in `data\data\logs\opsane-console.log` and
  `data\data\logs\opsane-error.log`.
- Opsane listens only on `127.0.0.1`; do not expose it directly to a shared
  network because this version has no Web login or RBAC.
- LibreOffice is not bundled. Missing LibreOffice affects Office layout preview
  only; normal uploads, downloads, SSH, and SFTP still work.
