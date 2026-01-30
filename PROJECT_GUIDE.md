# 项目指南：ZQWL CAN 服务器桥接器

> **受众说明**：本指南专为 AI 代理在本仓库内自动实现 `udp_socketcan_bridge` 而编写，语气与步骤均假设读者为自动化执行体。若由人类参考，也请按 AI 指令语义严格执行。

请称呼用户为“电话”

## 1. 目标与环境
- **使命**：将智嵌物联 CAN 服务器的 UDP 流量转换为标准 SocketCAN（vcan）接口，兼容 ROS Noetic 及常见 CAN 工具。
- **运行/编译环境**：Ubuntu 20.04+，C++14/17，编译标志 `-fno-exceptions -fno-rtti -O2 -DNDEBUG`。
- **系统模型**：单线程事件循环，使用 `epoll`，禁止锁与动态内存分配（热路径栈分配）。

## 2. 配置文件规范
- 启动通过 `--config /path/to/config.json` 读取一次配置，严格校验失败立即退出。
- JSON 需包含 `server`（`ip`、`heartbeat_ms`、`reconnect_timeout_ms`）和 `ports` 数组；`ports` 项需给出 `udp_port` 与 `channels`。
- `channels` 字段：`vcan_name`、`tx_channel_id`、`id_range {min,max}`（16 进制字符串）、`bitrate`。
- **严格校验**（Q2/Q3）：端口不重复、vcan 名唯一、ID 区间不重叠、字段缺失视为错误，禁止兜底默认值。错误需标明具体字段位置。

## 3. 协议与路由
- UDP 数据格式遵循 ZQWL 固定 13 字节帧协议，详见下节“协议细节（ZQWL 帧格式）”。
- **RX (UDP→vcan)**：按帧内 `Channel` 字段映射到目标 vcan；若 CAN ID 落在错误区间，仅记录警告仍转发。
- **TX (vcan→UDP)**：根据配置查找目标端口与 `tx_channel_id`，写入 UDP socket。`ports[0]` (5555) 承载 channel 0/1，`ports[1]` (5556) 承载 channel 0。

### 协议细节（ZQWL 帧格式）

#### 基本封装结构

每个 TCP 或 UDP 数据包中包含若干个 CAN 帧（最多可达 80 个）。**每个 CAN 帧固定包含 13 个字节**：

| 字节 | 1 | 2-5 | 6-13 |
|:---:|:---:|:---:|:---:|
| **含义** | 帧信息 | 帧 ID | 帧数据 |
| **长度** | 1 字节 | 4 字节 | 8 字节 |

#### 1. 帧信息（第 1 字节）

占 1 个字节，用于标识该 CAN 帧的类型、长度等信息。

| Bit | 7 | 6 | 5 | 4 | 3 | 2 | 1 | 0 |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **含义** | FF | RTR | RESVD | RESVD | D3 | D2 | D1 | D0 |

- **FF**：标准帧和扩展帧的标识；`1` 为扩展帧，`0` 为标准帧。
- **RTR**：远程帧和数据帧的标识；`1` 为远程帧，`0` 为数据帧。
- **RESVD**：保留值，必须填 0。
- **D3~D0**：标识该 CAN 帧的数据长度（0-8 字节）。

#### 2. 帧 ID（第 2-5 字节）

占 4 个字节，采用小端（低字节在前）存储：

- 标准帧：有效位是 11 位。
- 扩展帧：有效位是 29 位。

示例：

| 类型 | 低字节 |   |   | 高字节 | 实际 ID 值 |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 扩展帧 | `11` | `22` | `33` | `44` | `0x11223344` |
| 标准帧 | `22` | `01` | `00` | `00` | `0x122` |

#### 3. 帧数据（第 6-13 字节）

占 8 个字节，**有效长度由帧信息的 D3~D0 值决定**。

- **8 字节有效数据**：
  ```
  DATA1: 0x01, DATA2: 0x02, DATA3: 0x03, DATA4: 0x04
  DATA5: 0x05, DATA6: 0x06, DATA7: 0x07, DATA8: 0x08
  ```
- **4 字节有效数据**（后 4 字节补 0）：
  ```
  DATA1: 0x01, DATA2: 0x02, DATA3: 0x03, DATA4: 0x04
  DATA5: 0x00, DATA6: 0x00, DATA7: 0x00, DATA8: 0x00
  ```

#### 完整帧示例：扩展数据帧

```
0x88 0x11 0x22 0x33 0x44 0x01 0x02 0x03 0x04 0x05 0x06 0x07 0x08
```

- 帧信息 `0x88` (`10001000b`)：FF=1（扩展帧）、RTR=0（数据帧）、长度=8。
- 帧 ID `0x11223344`（小端存储：44 33 22 11）。
- 数据 `01 02 03 04 05 06 07 08`（8 字节有效）。

#### 完整帧示例：标准数据帧

```
0x08 0x00 0x00 0x01 0x22 0x01 0x02 0x03 0x04 0x05 0x06 0x07 0x08
```

- 帧信息 `0x08` (`00001000b`)：FF=0（标准帧）、RTR=0（数据帧）、长度=8。
- 帧 ID `0x00000122`（小端存储：22 01 00 00）。
- 数据 `01 02 03 04 05 06 07 08`（8 字节有效）。
## 4. 初始化与运行流程
1. 读取并验证配置。
2. 为每个 channel 确保对应 `vcan` 接口存在并 `ip link set ... type can bitrate <bitrate>`，接口异常需重试最多 3 次。
3. 创建 `epoll`，注册：
   - UDP sockets（非阻塞、接收缓冲 1MB），连接到服务器 IP 与端口。
   - `PF_CAN` `SOCK_RAW` vcan sockets。
   - `timerfd` 触发 1 Hz 心跳。
4. 进入事件循环：处理 UDP/VCan I/O、心跳发送、超时（5s 无数据重连）。

## 5. 恢复与日志
- UDP 超时：关闭并重建 socket，重新注册 epoll。
- vcan 异常：尝试重新创建接口；失败记录 `LOG_ERR`。
- 所有日志通过 `syslog` (`LOG_WARNING`/`LOG_ERR`)，禁止 stdout/stderr 正常输出以便 systemd 管理。仅在配置校验失败时向 stderr 给出定位信息。

## 6. 性能与资源约束
- 禁止 `new/delete`, `malloc/free`, `std::shared_ptr`, `std::vector` 动态扩容；推荐 `std::array`、固定大小 `struct can_frame`。
- 端到端延迟目标 <1ms（典型 <100μs），CPU <5%（CAN 3 路 @100Hz）。
- 不做应用层限速，部署文档需指导使用 `tc`。

## 7. 交付物与部署
- 可执行文件：`udp_socketcan_bridge`，静态或极少依赖。
- 示例配置：`config.json`（与示例 schema 保持一致）。
- Systemd service：`udp_socketcan_bridge.service`，`ExecStart=/usr/local/bin/udp_socketcan_bridge --config /etc/udp_socketcan_bridge/config.json`，`StandardOutput=null`。
- 辅助脚本：`setup_vcan.sh`，解析配置文件以创建/启动所需 vcan 接口并拉起主程序，可扩展附带 `tc` 速率脚本。
- 文档需说明：`journalctl -u udp_socketcan_bridge -f` 调试，`candump vcanX` 验证。

## 8. 开发建议
- 单元测试关注配置解析/校验、协议解析、路由映射表。
- 在实现 epoll 循环时，将 UDP 与 vcan 管理抽象为轻量结构体，避免动态内存而使用固定数组或 `std::array`.
- 使用 `timerfd` 实现心跳；可在错误时记录发生的端口、channel、ID 及 errno 以协助排查。
- 引导文档中强调部署前需执行 `setup_vcan.sh` 与 `systemctl enable --now udp_socketcan_bridge`.
- 可结合 `tests/send_random_can.py`（随机 UDP→CAN）与 `tests/recv_udp_frames.py`（持续 UDP 捕获解析）进行端到端调试；某些命令需 sudo、依赖 `jq` 与 `can-utils`。

## 9. 最小原型功能
- 支持单端口/单 channel 的最小配置解析，用于建立基础路由映射。
- 创建非阻塞 UDP/vcan sockets 并注册单线程 `epoll` 循环。
- 解析 UDP 帧并做长度/CRC 校验。
- 将解析出的 CAN 帧写入 vcan，反向将 vcan 帧封装为上述 UDP 包发送。
- 循环内处理最基础的 I/O 错误以保持运行。

本指南旨在帮助 AI 代理快速理解需求并贯彻实现过程，可根据后续需求演进同步更新；若执行中遇到规范缺失，以本文为最高优先级。
