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

# 确保 vcan 模块已加载，如果失败则报错
modprobe vcan || echo "[bridge] Warning: modprobe vcan failed, ifaces might not be created."

CREATED_IFACES=()

# 清理函数
cleanup() {
  # 禁用 trap 以防止递归触发
  trap - EXIT INT TERM
  
  echo ""
  echo "[bridge] Cleaning up resources..."
  
  # 1. 停止桥接程序
  if [[ -n "${BRIDGE_PID:-}" ]]; then
    if kill -0 "${BRIDGE_PID}" 2>/dev/null; then
      echo "[bridge] Stopping udp_socketcan_bridge (PID: ${BRIDGE_PID})"
      kill "${BRIDGE_PID}" 2>/dev/null || true
      wait "${BRIDGE_PID}" 2>/dev/null || true
    fi
    BRIDGE_PID=""
  fi

  # 2. 移除创建的虚拟接口
  for if_name in "${CREATED_IFACES[@]}"; do
    if ip link show "${if_name}" >/dev/null 2>&1; then
      echo "[bridge] Removing interface: ${if_name}"
      ip link delete "${if_name}" 2>/dev/null || true
    fi
  done
  echo "[bridge] Cleanup complete."
}

# 关键修改：绑定 EXIT 信号
trap cleanup EXIT INT TERM

create_iface() {
  local if_name="$1"
  if ip link show "${if_name}" >/dev/null 2>&1; then
    echo "[bridge] ${if_name} already exists"
  else
    echo "[bridge] Creating ${if_name}"
    # 如果这里报错，set -e 会触发脚本退出，进而触发 trap cleanup
    ip link add dev "${if_name}" type vcan
    CREATED_IFACES+=("${if_name}")
  fi
  echo "[bridge] Bringing ${if_name} up"
  ip link set "${if_name}" up
}

# 依次创建接口
for if_name in "${VCAN_NAMES[@]}"; do
  create_iface "${if_name}"
done

echo "[bridge] Starting udp_socketcan_bridge"
# 启动程序
"${BRIDGE_BIN}" --config "${CONFIG_PATH}" &
BRIDGE_PID=$!

# 等待程序运行
# 如果程序因为 "Operation not supported" 退出，wait 会返回非零值
# 随后 set -e 会终止脚本，触发上面的 cleanup 函数
wait "${BRIDGE_PID}"
