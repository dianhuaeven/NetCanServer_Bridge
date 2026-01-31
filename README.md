# udp_socketcan_bridge

将UDP CANServer 流量桥接到 Linux SocketCAN 的纯 C++17 工具。程序通过单线程 `epoll` 循环在 UDP 与 CAN 之间互转 13 字节固定帧，可直接作为 ROS / `can-utils` 等应用的数据源，也能把本地 vcan 数据推送回服务器。

## 功能特性
- **双向桥接**：UDP → SocketCAN 与 SocketCAN → UDP 同时工作，热路径全程无动态分配。
- **可配置端口**：支持分别指定 UDP 监听端口与发送端口，兼容旧版 `udp_port` 配置。
- **固定协议解析**：遵循 ZQWL 13 字节帧格式（Info + ID + Data），自动处理标准/扩展帧与 RTR。
- **事件驱动**：所有套接字均设为非阻塞，使用 `epoll` 统一调度。
- **附带压测脚本**：`tests/` 中提供多种端到端脚本，方便验证 RX/TX 吞吐或做回环测试。

## 目录结构
```
src/
  bridge.hpp / bridge.cpp   # BridgeApp 类：套接字初始化、epoll 循环、收发逻辑
  config.hpp / config.cpp   # 配置解析与校验
  protocol.hpp / protocol.cpp # 13 字节帧编解码
tests/                      # 各类压测与示例脚本
config.json                 # 示例配置
start.sh                    # 参考启动脚本（可扩展为 systemd service）
```

## 构建
```bash
cmake -S . -B build
cmake --build build
```
构建产物：`build/udp_socketcan_bridge`。编译器需支持 C++17；推荐在 Ubuntu 20.04+ 下使用系统 `gcc`/`clang`。

## 配置
入口通过 `--config` 读取 JSON。示例（`config.json`）：
```json
{
  "server": {
    "ip": "127.0.0.1",
    "heartbeat_ms": 1000,
    "reconnect_timeout_ms": 5000
  },
  "ports": [
    {
      "udp_listen_port": 5555,
      "udp_send_port": 5555,
      "channels": [
        {
          "vcan_name": "vcan0",
          "tx_channel_id": 0,
          "id_range": {
            "min": "0x000",
            "max": "0x7FF"
          },
          "bitrate": 500000
        }
      ]
    }
  ]
}
```
- `udp_listen_port`：桥接程序绑定的本地端口（RX）。
- `udp_send_port`：桥接程序向服务器发送的目的端口（TX）。
- 若仅提供旧字段 `udp_port`，程序会将其同时用作监听与发送端口，保证向后兼容。

### 配置快速校验
构建后可以使用 `udp_config_validator` 进行静态检查：
```bash
./build/udp_config_validator config.json
```
若配置有效会输出 `Config OK`，否则会指出首个错误字段。

## 运行
1. 准备 SocketCAN 接口（示例以 `vcan0` 为例）：
   ```bash
   sudo modprobe vcan
   sudo ip link add dev vcan0 type vcan
   sudo ip link set up vcan0
   ```
2. 启动桥接器：
   ```bash
   sudo ./build/udp_socketcan_bridge --config config.json
   ```
3. 观察日志：程序会打印监听/发送端口信息以及“Bridge is running”提示，可配合 `candump vcan0`、`tcpdump udp port …` 做联调。

## 测试与调试脚本
`tests/` 目录包含多个 Python3 脚本（需 `sudo` 以访问 SocketCAN）：

| 脚本 | 说明 |
| ---- | ---- |
| `udp_to_can_rx_stress.py` | UDP → CAN 方向压测器，显示 TX/RX PPS 与 Mbps。 |
| `can_to_udp_tx_stress.py` | CAN → UDP 方向压测器，监控回传吞吐与丢包。 |
| `udp_can_loopback_pingpong.py` | 全链路乒乓测试：CAN→Bridge→UDP→Bridge→CAN，验证闭环时延。 |
| `can_raw_flood.py` | 高速向指定 CAN 接口灌包。 |
| `random_can_sender.py` | 每秒生成随机 CAN 帧，便于基本功能验证。 |
| `udp_frame_dump.py` | 监听 UDP 端口并解析输出 13 字节帧内容。 |

使用示例：
```bash
sudo python3 tests/udp_to_can_rx_stress.py
# 或
sudo python3 tests/can_to_udp_tx_stress.py
```

## 开发与扩展
- 核心桥接逻辑集中在 `BridgeApp`，如需增加统计、自定义过滤、心跳等功能，可在 `bridge.cpp` 中扩展对应方法。
- 协议修改只需调整 `protocol.cpp/hpp`，其余模块通过 `kUdpFrameSize` 常量共享帧长度。
- 若要在真实硬件上运行，请根据 `config.json` 中的 `channels[].bitrate` 创建/配置物理 CAN 接口（例如 `ip link set can0 type can bitrate 500000`）。
- 部署在系统服务时，可参考 `start.sh` 或自行编写 systemd unit，记得将日志重定向到 syslog / journal。

如需了解更详细的需求背景与开发约束，可阅读 `PROJECT_GUIDE.md`。欢迎根据自身场景扩展配置、脚本或监控指标。***
