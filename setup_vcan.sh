#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}" && pwd)"
BRIDGE_BIN="${REPO_ROOT}/build/udp_socketcan_bridge"
CONFIG_PATH="${SCRIPT_DIR}/config.json"

usage() {
  echo "Usage: $0 [--config <config.json>]" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || usage
      CONFIG_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must be run as root (e.g., sudo $0 --config config/minimal_config.json)" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required but not found in PATH" >&2
  exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Config file not found: ${CONFIG_PATH}" >&2
  exit 1
fi

if [[ ! -x "${BRIDGE_BIN}" ]]; then
  echo "Bridge binary not found at ${BRIDGE_BIN}. Build it via 'cmake -S . -B build && cmake --build build' before running this script." >&2
  exit 1
fi

CONFIG_PATH="$(cd "$(dirname "${CONFIG_PATH}")" && pwd)/$(basename "${CONFIG_PATH}")"

mapfile -t VCAN_NAMES < <(jq -r '.ports[]? | .channels[]? | .vcan_name // empty' "${CONFIG_PATH}" | LC_ALL=C sort -u)

if [[ ${#VCAN_NAMES[@]} -eq 0 ]]; then
  echo "No vcan interfaces defined in config ${CONFIG_PATH}" >&2
  exit 1
fi

modprobe vcan >/dev/null 2>&1 || true

CREATED_IFACES=()

create_iface() {
  local if_name="$1"
  if ip link show "${if_name}" >/dev/null 2>&1; then
    echo "[bridge] ${if_name} already exists"
  else
    echo "[bridge] creating ${if_name}"
    ip link add dev "${if_name}" type vcan
    CREATED_IFACES+=("${if_name}")
  fi
  echo "[bridge] bringing ${if_name} up"
  ip link set "${if_name}" up
}

cleanup() {
  local exit_code="${1:-0}"
  if [[ -n "${BRIDGE_PID:-}" ]]; then
    if kill -0 "${BRIDGE_PID}" >/dev/null 2>&1; then
      kill "${BRIDGE_PID}" >/dev/null 2>&1 || true
      wait "${BRIDGE_PID}" >/dev/null 2>&1 || true
    fi
    BRIDGE_PID=""
  fi
  for if_name in "${CREATED_IFACES[@]}"; do
    echo "[bridge] removing ${if_name}"
    ip link delete "${if_name}" type vcan >/dev/null 2>&1 || true
  done
  exit "${exit_code}"
}

trap 'cleanup 0' INT TERM

for if_name in "${VCAN_NAMES[@]}"; do
  create_iface "${if_name}"
done

echo "[bridge] starting udp_socketcan_bridge"
"${BRIDGE_BIN}" --config "${CONFIG_PATH}" &
BRIDGE_PID=$!

wait "${BRIDGE_PID}"
status=$?
BRIDGE_PID=""
cleanup "${status}"
