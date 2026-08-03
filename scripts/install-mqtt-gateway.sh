#!/usr/bin/env bash

set -euo pipefail
umask 077

CONFIG_PATH=""
NO_START=0
SKIP_PACKAGES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            shift
            [[ $# -gt 0 ]] || { echo "--config requires a path" >&2; exit 2; }
            CONFIG_PATH="$1"
            ;;
        --no-start) NO_START=1 ;;
        --skip-packages) SKIP_PACKAGES=1 ;;
        -h|--help)
            echo "Usage: sudo $0 --config PATH [--no-start] [--skip-packages]"
            exit 0
            ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

[[ -n "$CONFIG_PATH" ]] || { echo "ERROR: --config is required" >&2; exit 2; }

if [[ "$EUID" -ne 0 ]]; then
    ARGS=(--config "$CONFIG_PATH")
    [[ "$NO_START" -eq 1 ]] && ARGS+=(--no-start)
    [[ "$SKIP_PACKAGES" -eq 1 ]] && ARGS+=(--skip-packages)
    exec sudo -- bash "$0" "${ARGS[@]}"
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
CONFIG_PATH="$(realpath -e "$CONFIG_PATH")"
chmod 600 "$CONFIG_PATH"

/usr/bin/python3 "$REPO_DIR/sanlight_mqtt_gateway.py" \
    --config "$CONFIG_PATH" \
    --check

mapfile -t CONFIG_PATHS < <(
    PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 - "$CONFIG_PATH" <<'PY'
from pathlib import Path
import sys
from sanlight_mesh.gateway_config import load_gateway_config
config = load_gateway_config(Path(sys.argv[1]))
print(config.project_root)
print(config.state_dir)
PY
)
[[ "${#CONFIG_PATHS[@]}" -eq 2 ]] || { echo "ERROR: cannot resolve gateway paths" >&2; exit 1; }
CONFIG_REPO_DIR="$(realpath -m "${CONFIG_PATHS[0]}")"
STATE_DIR="$(realpath -m "${CONFIG_PATHS[1]}")"

if [[ "$CONFIG_REPO_DIR" != "$REPO_DIR" ]]; then
    echo "ERROR: gateway.project_root resolves to $CONFIG_REPO_DIR, expected $REPO_DIR" >&2
    exit 1
fi
for required in control-provisioner.json canonical-sender.json; do
    if [[ ! -f "$STATE_DIR/$required" ]]; then
        echo "ERROR: protected identity state is missing: $STATE_DIR/$required" >&2
        echo "Run the authoritative scripts/install-gateway.sh; it safely prepares or recovers Mesh state first." >&2
        exit 1
    fi
done
install -d -m 0700 "$STATE_DIR"

if [[ "$SKIP_PACKAGES" -eq 0 ]]; then
    apt-get update
    apt-get install -y --no-install-recommends python3-paho-mqtt
fi

render_unit() {
    local source="$1" target="$2"
    /usr/bin/python3 - "$source" "$target" "$REPO_DIR" "$CONFIG_PATH" "$STATE_DIR" <<'PY'
from pathlib import Path
import sys
source, target, repo, config, state = map(Path, sys.argv[1:])
text = source.read_text(encoding="utf-8")
for marker, value in {
    "@REPO_DIR@": str(repo),
    "@CONFIG_PATH@": str(config),
    "@STATE_DIR@": str(state),
}.items():
    if any(character.isspace() for character in value):
        raise SystemExit(
            f"unsupported whitespace in path for {marker}; move the repository/config "
            "to a path without spaces"
        )
    text = text.replace(marker, value)
target.write_text(text, encoding="utf-8")
target.chmod(0o644)
PY
}

render_unit \
    "$REPO_DIR/systemd/sanlight-mqtt-gateway.service.example" \
    "/etc/systemd/system/sanlight-mqtt-gateway.service"
render_unit \
    "$REPO_DIR/systemd/sanlight-mesh-watchdog.service.example" \
    "/etc/systemd/system/sanlight-mesh-watchdog.service"
install -m 0644 \
    "$REPO_DIR/systemd/sanlight-mesh-watchdog.timer" \
    "/etc/systemd/system/sanlight-mesh-watchdog.timer"

systemctl daemon-reload
systemctl enable sanlight-mqtt-gateway.service
systemctl enable sanlight-mesh-watchdog.timer
if [[ "$NO_START" -eq 0 ]]; then
    systemctl restart sanlight-mqtt-gateway.service
    systemctl restart sanlight-mesh-watchdog.timer
    sleep 2
    systemctl --no-pager --full status sanlight-mqtt-gateway.service || true
    systemctl --no-pager --full status sanlight-mesh-watchdog.timer || true
else
    echo "Gateway service and Mesh watchdog timer installed but not started (--no-start)."
fi

echo "MQTT gateway service and conservative Mesh watchdog installation complete."
