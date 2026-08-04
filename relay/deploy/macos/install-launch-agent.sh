#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install-launch-agent.sh --executable /absolute/path/to/aixion-relay \
  --config /absolute/path/to/config.json

Installs a per-user LaunchAgent. Relay credentials remain in macOS Keychain and are
not copied into the plist.
EOF
}

EXECUTABLE=""
CONFIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --executable)
      EXECUTABLE="${2:-}"
      shift 2
      ;;
    --config)
      CONFIG="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$EXECUTABLE" || -z "$CONFIG" ]]; then
  usage >&2
  exit 2
fi
if [[ ! -x "$EXECUTABLE" ]]; then
  echo "Relay executable is not executable: $EXECUTABLE" >&2
  exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "Relay configuration does not exist: $CONFIG" >&2
  exit 1
fi
if [[ "$EXECUTABLE" != /* || "$CONFIG" != /* ]]; then
  echo "Executable and config paths must be absolute." >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/com.aixion.agent-relay.plist.template"
TARGET_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/AixionRelay"
TARGET="$TARGET_DIR/com.aixion.agent-relay.plist"
mkdir -p "$TARGET_DIR" "$LOG_DIR"
chmod 700 "$LOG_DIR"

escape_sed() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

sed \
  -e "s|__AIXION_RELAY_EXECUTABLE__|$(escape_sed "$EXECUTABLE")|g" \
  -e "s|__AIXION_RELAY_CONFIG__|$(escape_sed "$CONFIG")|g" \
  -e "s|__AIXION_RELAY_LOG_DIR__|$(escape_sed "$LOG_DIR")|g" \
  "$TEMPLATE" > "$TARGET.tmp"
plutil -lint "$TARGET.tmp"
mv "$TARGET.tmp" "$TARGET"
chmod 600 "$TARGET"

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/com.aixion.agent-relay" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET"
launchctl enable "$DOMAIN/com.aixion.agent-relay"
launchctl kickstart -k "$DOMAIN/com.aixion.agent-relay"

echo "Installed $TARGET"
echo "Inspect status with: launchctl print $DOMAIN/com.aixion.agent-relay"
echo "Logs: $LOG_DIR"
