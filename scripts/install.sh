#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${AGENTSYNC_REPO_URL:-git+https://github.com/saleqss/agentsync.git}"

echo "[agentsync] Starting installation from ${REPO_URL}..."

if command -v uv >/dev/null 2>&1; then
  echo "[agentsync] Installing via uv tool..."
  uv tool install --force "${REPO_URL}"
elif command -v pipx >/dev/null 2>&1; then
  echo "[agentsync] Installing via pipx..."
  pipx install --force "${REPO_URL}"
elif command -v python3 >/dev/null 2>&1; then
  echo "[agentsync] Installing via python3 -m pip --user..."
  python3 -m pip install --user --upgrade "${REPO_URL}"
  USER_BIN="$(python3 -c 'import site, os; print(os.path.join(site.USER_BASE, "bin"))')"
  export PATH="${USER_BIN}:${PATH}"
else
  echo "Error: Python 3.11+, pipx, or uv is required to install agentsync." >&2
  exit 1
fi

if command -v agentsync >/dev/null 2>&1; then
  echo "[agentsync] Verifying global command..."
  agentsync --version
  echo "[agentsync] Installation complete! Run 'agentsync discover --save' to get started."
else
  echo "[agentsync] Installed, but 'agentsync' is not yet on PATH. Add your Python user bin directory to PATH and restart your shell."
fi
