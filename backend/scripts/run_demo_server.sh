#!/usr/bin/env bash
set -euo pipefail

HOST="${AIXION_DEMO_HOST:-0.0.0.0}"
PORT="${AIXION_DEMO_PORT:-8000}"

export AIXION_PROFILE="${AIXION_PROFILE:-demo}"
export AIXION_AUTH_ENABLED="${AIXION_AUTH_ENABLED:-false}"
export AIXION_DB_PATH="${AIXION_DB_PATH:-runtime/aixion_control_tower_demo.sqlite3}"

echo "Starting Aixion Control Tower demo backend"
echo "Profile: $AIXION_PROFILE"
echo "Host: $HOST"
echo "Port: $PORT"
echo "Auth enabled: $AIXION_AUTH_ENABLED"
echo "DB path: $AIXION_DB_PATH"
echo ""
echo "For Android emulator, use: http://10.0.2.2:$PORT/"
echo "For a physical phone, use your computer LAN IP, for example: http://192.168.1.42:$PORT/"
echo ""

uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
