#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install-systemd-user.sh --executable /absolute/path/to/aixion-relay \
  --config /absolute/path/to/config.json

Installs the relay as a hardened per-user systemd service. The relay token remains in
Secret Service/keyring or an external secret manager and is never copied into the unit.
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
if [[ "$EXECUTABLE" != /* || "$CONFIG" != /* ]]; then
  echo "Executable and config paths must be absolute." >&2
  exit 1
fi
if [[ ! -x "$EXECUTABLE" ]]; then
  echo "Relay executable is not executable: $EXECUTABLE" >&2
  exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "Relay configuration does not exist: $CONFIG" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/aixion-agent-relay.service.template"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/aixion-relay"
TARGET="$UNIT_DIR/aixion-agent-relay.service"
mkdir -p "$UNIT_DIR" "$STATE_DIR"
chmod 700 "$STATE_DIR"

escape_sed() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

sed \
  -e "s|__AIXION_RELAY_EXECUTABLE__|$(escape_sed "$EXECUTABLE")|g" \
  -e "s|__AIXION_RELAY_CONFIG__|$(escape_sed "$CONFIG")|g" \
  -e "s|__AIXION_RELAY_STATE_DIR__|$(escape_sed "$STATE_DIR")|g" \
  "$TEMPLATE" > "$TARGET.tmp"
mv "$TARGET.tmp" "$TARGET"
chmod 600 "$TARGET"

systemctl --user daemon-reload
systemctl --user enable --now aixion-agent-relay.service
systemctl --user --no-pager status aixion-agent-relay.service || true

echo "Installed $TARGET"
echo "Logs: journalctl --user -u aixion-agent-relay.service -f"
