#!/usr/bin/env bash
# AgentMail unattended installer — follows AGENT_GUIDE.md exactly.
#
# Usage:
#   ./install.sh [INSTALL_DIR]
#   curl -fsSL https://raw.githubusercontent.com/PetrouilFan/AgentMail/main/install.sh | bash -s -- [INSTALL_DIR]
#
# Defaults: INSTALL_DIR=$HOME/AgentMail, port 12345, binds 0.0.0.0.
# Idempotent: safe to re-run. Writes a systemd unit (or tmux fallback if no systemd).
set -euo pipefail

INSTALL_DIR="${1:-$HOME/AgentMail}"
PORT=12345
REPO="https://github.com/PetrouilFan/AgentMail.git"

echo "==> AgentMail installer"
echo "    dir : $INSTALL_DIR"
echo "    port: $PORT"

# 1. uv (install if missing)
if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # make uv available in this non-login shell
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv still not on PATH after install. Source your shell profile and retry." >&2
    exit 1
  fi
fi

# 2. clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "==> updating existing repo"
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "==> cloning repo"
  git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# 3. deps
echo "==> uv sync"
uv sync

# 4. identity (skip if keys already exist)
if [ ! -f "$INSTALL_DIR/config.yaml" ] && [ ! -f "$HOME/.agentmail/keys/self.key" ]; then
  echo "==> agentmail init + keygen"
  uv run agentmail init
  uv run agentmail keygen
else
  echo "==> identity already present, skipping init/keygen"
fi

# Ensure a config.yaml exists for the service to point at
if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
  cat > "$INSTALL_DIR/config.yaml" <<YAML
identity:
  name: $(hostname -s)
transports:
  http:
    host: 0.0.0.0
    port: $PORT
agents: {}
defaults:
  content_type: text/plain
  require_signature: false
YAML
fi

BIN="$INSTALL_DIR/.venv/bin/agentmail"

# 5. run as a service
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  UNIT=/etc/systemd/system/agentmail.service
  echo "==> writing systemd unit: $UNIT"
  sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=AgentMail server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$BIN serve --host 0.0.0.0 --port $PORT --config $INSTALL_DIR/config.yaml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now agentmail
  echo "==> started. status:"
  systemctl status --no-pager agentmail | head -5
else
  echo "==> no systemd; starting under tmux (survives terminal close, not reboot)"
  command -v tmux >/dev/null 2>&1 || { echo "ERROR: tmux not found and no systemd. Install tmux or run 'uv run agentmail serve' manually." >&2; exit 1; }
  tmux new -s agentmail -d "$BIN serve --host 0.0.0.0 --port $PORT --config $INSTALL_DIR/config.yaml"
  sleep 2
  tmux capture-pane -t agentmail -p | tail -5
fi

# 6. make `agentmail` usable as a bare command (symlink into ~/.local/bin)
LOCAL_BIN="$HOME/.local/bin"
if [ -x "$BIN" ]; then
  mkdir -p "$LOCAL_BIN"
  ln -sf "$BIN" "$LOCAL_BIN/agentmail"
  case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *) echo "NOTE: add $LOCAL_BIN to your PATH to run 'agentmail' without 'uv run' (e.g. export PATH=\"\$HOME/.local/bin:\$PATH\")." ;;
  esac
  echo "==> linked $LOCAL_BIN/agentmail -> $BIN"
fi

# 7. connectivity self-check
echo "==> self-check /ping"
sleep 1
curl -fsS --max-time 8 "http://127.0.0.1:$PORT/ping" && echo "" && echo "==> DONE. AgentMail is live on port $PORT." \
  || echo "WARN: /ping failed locally — check logs (journalctl -u agentmail or tmux)."
