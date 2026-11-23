#!/usr/bin/env bash
# Install the Pharos watch loop as a user service and start it.
# macOS uses launchd. Linux uses systemd --user.
# The process runs create, reprice, resolve, and settle immediately, then
# forever. The chain client loads .env from the repo root, so the RPC and
# key already configured for `make sync` are the ones the service uses.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$([ -x "$ROOT/.venv/bin/python3" ] && echo "$ROOT/.venv/bin/python3" || command -v python3)"
LABEL="com.lighthousemacro.pharos-watch"
mkdir -p "$ROOT/logs"

if [[ "$(uname -s)" == "Darwin" ]]; then
  PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>WorkingDirectory</key><string>${ROOT}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>-m</string>
    <string>pharos_agents.runner</string>
    <string>watch</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>${ROOT}/agents:${ROOT}/pricing</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${ROOT}/logs/watch.log</string>
  <key>StandardErrorPath</key><string>${ROOT}/logs/watch.err</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "started ${LABEL}"
  echo "log: ${ROOT}/logs/watch.log"
else
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat >"$UNIT_DIR/pharos-watch.service" <<EOF
[Unit]
Description=Pharos market sync (create, reprice, resolve, settle)

[Service]
WorkingDirectory=${ROOT}
Environment=PYTHONPATH=${ROOT}/agents:${ROOT}/pricing
ExecStart=${PY} -m pharos_agents.runner watch
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now pharos-watch.service
  echo "started pharos-watch.service"
  echo "log: journalctl --user -u pharos-watch.service -f"
fi
