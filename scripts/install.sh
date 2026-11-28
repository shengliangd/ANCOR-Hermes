#!/usr/bin/env bash
# ANCOR standalone one-line installer.
#
# Installs ANCOR into its own isolated virtual environment (~/.ancor/venv) and
# exposes the `ancor` CLI at ~/.local/bin/ancor without touching Hermes, Codex,
# AGY, DSH, or any other tool's configuration or environment.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/shengliangd/ANCOR-Hermes/dev/scripts/install.sh | bash
#   bash scripts/install.sh [--editable /path/to/repo] [--ref main] [--upgrade] [--uninstall] [--purge]
set -euo pipefail

REPO_URL="${ANCOR_REPO_URL:-https://github.com/shengliangd/ANCOR-Hermes.git}"
REF="${ANCOR_REF:-dev}"
ANCOR_HOME="${ANCOR_HOME:-$HOME/.ancor}"
BIN_DIR="${ANCOR_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$ANCOR_HOME/venv"
SRC_DIR="$ANCOR_HOME/src"

EDITABLE_PATH=""
ACTION="install"
PURGE=0

usage() {
  cat <<EOF
ANCOR standalone installer (isolated venv at \$ANCOR_HOME/venv)

Options:
  --editable <dir>   Install in editable mode from a local ANCOR checkout
  --repo <url>       Git repository URL (default: $REPO_URL)
  --ref <branch|tag> Git branch or tag to install (default: $REF)
  --home <dir>       ANCOR home directory (default: $ANCOR_HOME)
  --bin-dir <dir>    Directory for the ancor symlink (default: $BIN_DIR)
  --upgrade          Update existing installation in place
  --uninstall        Remove ancor symlink, venv, and managed src (preserves DB/config)
  --purge            With --uninstall, also remove \$ANCOR_HOME (including DB/config)
  -h, --help         Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --editable) EDITABLE_PATH="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --home) ANCOR_HOME="$2"; VENV_DIR="$ANCOR_HOME/venv"; SRC_DIR="$ANCOR_HOME/src"; shift 2 ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --upgrade) ACTION="upgrade"; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log() { printf '[ancor-install] %s\n' "$*"; }

if [[ "$ACTION" == "uninstall" ]]; then
  rm -f "$BIN_DIR/ancor"
  rm -rf "$VENV_DIR" "$SRC_DIR"
  if [[ "$PURGE" -eq 1 ]]; then
    rm -rf "$ANCOR_HOME"
    log "Removed $BIN_DIR/ancor and purged $ANCOR_HOME."
  else
    log "Removed $BIN_DIR/ancor, $VENV_DIR, and $SRC_DIR (data in $ANCOR_HOME preserved)."
  fi
  exit 0
fi

mkdir -p "$ANCOR_HOME" "$BIN_DIR"

# 1. Resolve source directory
if [[ -n "$EDITABLE_PATH" ]]; then
  INSTALL_SRC="$(cd "$EDITABLE_PATH" && pwd)"
  log "Using local checkout: $INSTALL_SRC"
else
  if [[ -d "$SRC_DIR/.git" ]]; then
    log "Updating managed checkout in $SRC_DIR ($REF)..."
    git -C "$SRC_DIR" fetch --depth 1 origin "$REF"
    git -C "$SRC_DIR" checkout -q FETCH_HEAD
  else
    log "Cloning $REPO_URL ($REF) into $SRC_DIR..."
    git clone --depth 1 --branch "$REF" "$REPO_URL" "$SRC_DIR"
  fi
  INSTALL_SRC="$SRC_DIR"
fi

# 2. Create isolated virtual environment & install ancor
if command -v uv >/dev/null 2>&1; then
  log "Creating isolated venv with uv at $VENV_DIR..."
  uv venv --clear --python ">=3.11" "$VENV_DIR" >/dev/null 2>&1 || uv venv "$VENV_DIR"
  log "Installing ancor into $VENV_DIR..."
  if [[ -n "$EDITABLE_PATH" ]]; then
    uv pip install --python "$VENV_DIR/bin/python" --no-build-isolation -e "$INSTALL_SRC" 2>/dev/null \
      || uv pip install --python "$VENV_DIR/bin/python" -e "$INSTALL_SRC"
  else
    uv pip install --python "$VENV_DIR/bin/python" -e "$INSTALL_SRC"
  fi
else
  PYTHON_BIN="$(command -v python3 || true)"
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "Error: Python 3.11+ or uv is required to install ANCOR." >&2
    exit 1
  fi
  "$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null || {
    echo "Error: Python 3.11+ is required (found $("$PYTHON_BIN" --version 2>&1))." >&2
    exit 1
  }
  log "Creating isolated venv with python3 -m venv at $VENV_DIR..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  log "Installing ancor into $VENV_DIR..."
  "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
  "$VENV_DIR/bin/pip" install -e "$INSTALL_SRC"
fi

# 3. Expose single entrypoint symlink
ln -sfn "$VENV_DIR/bin/ancor" "$BIN_DIR/ancor"
log "Installed ancor -> $BIN_DIR/ancor (venv: $VENV_DIR)"

# 4. Smoke check
"$BIN_DIR/ancor" --help >/dev/null
log "Verification passed."

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    printf '\nNote: %s is not currently in your PATH. Add:\n  export PATH="%s:$PATH"\n' "$BIN_DIR" "$BIN_DIR"
    ;;
esac

cat <<EOF

Next steps:
  ancor init                  # Create starter ~/.ancor/config.yaml
  ancor doctor                # Verify backends (hermes / codex / ...) and environment
  ancor install <target>      # Optional: install Interactor skill for hermes | codex | agy | dsh
  ancor daemon start          # Start standalone scheduling daemon
  ancor serve                 # Start Web viewer
EOF
