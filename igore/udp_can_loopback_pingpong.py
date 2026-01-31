import argparse
import json
import os
import random
import socket
import struct
import sys
import time

def load_config():
    try:
        with open("config.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print("错误: config.json 未找到")
        sys.exit(1)

def resolve_port(port_cfg, key):
    return port_cfg.get(key) or port_cfg.get("udp_port")

# --- 协议定义 ---
UDP_FMT = ">BI8s"  # 13字节大端序 UDP
CAN_FMT = "=IB3s8s" # 16字节标准 CAN (Linux 内核格式)

def run_ping_pong_test(port_index: int, channel_index: int):
    config = load_config()
    ports = config.get("ports") or []
    if not ports:
        print("错误: config.json 中没有 ports 配置")
        return
    if port_index < 0 or port_index >= len(ports):
        print(f"错误: ports 索引 {port_index} 超出范围 (0-{len(ports)-1})")
        return
    port_cfg = ports[port_index]

    channels = port_cfg.get("channels") or []
    if not channels:
        print(f"错误: ports[{port_index}] 未配置 channels")
        return
    if channel_index < 0 or channel_index >= len(channels):
        print(f"错误: channels 索引 {channel_index} 超出范围 (0-{len(channels)-1})")
        return

    channel_cfg = channels[channel_index]
    bridge_listen_port = resolve_port(port_cfg, "udp_listen_port")
    script_listen_port = resolve_port(port_cfg, "udp_send_port")
    server_ip = config["server"]["ip"]
    vcan_iface = channel_cfg["vcan_name"]
    if bridge_listen_port is None or script_listen_port is None:
        print("错误: 配置缺少 udp_listen_port/udp_send_port")
        return

    # 1. 初始化 UDP 套接字
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        udp_sock.bind(("0.0.0.0", script_listen_port))
        udp_sock.settimeout(0.5) 
    except Exception as e:
        print(f"错误: 无法绑定 UDP 端口 {script_listen_port}: {e}")
        return

    # 2. 初始化 CAN 套接字
    try:
        can_sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        can_sock.bind((vcan_iface,))
        can_sock.settimeout(0.5)
    except Exception as e:
        print(f"错误: 无法连接 CAN 接口 {vcan_iface}: {e}")
        return

    print(f"--- 乒乓全链路回环测试 (带带宽显示) ---")
    print(f"使用 ports[{port_index}], channels[{channel_index}] -> {vcan_iface}")
    print(f"路径: CAN({vcan_iface}) -> Bridge(send→UDP {server_ip}:{script_listen_port}) -> "
          f"脚本UDP -> Bridge(listen {server_ip}:{bridge_listen_port}) -> CAN")
    print("-" * 60)

    # 统计数据
    stats = {
        "total": 0, 
        "success": 0, 
        "timeout": 0, 
        "data_err": 0,
        "last_success": 0
    }
    
    start_time = time.time()
    last_report_time = start_time

    try:
        while True:
            stats["total"] += 1
            test_id = random.randint(0x100, 0x700)
            test_data = bytes([random.randint(0, 255) for _ in range(8)])
            
            # --- 步骤 1: 发送原始 CAN 包 ---
            tx_frame = struct.pack(CAN_FMT, test_id, 8, b'\x00\x00\x00', test_data)
            can_sock.send(tx_frame)
            
            # --- 步骤 2: 等待 UDP 转发 (从 5556 收) ---
            try:
                udp_pkt, _ = udp_sock.recvfrom(1024)
                u_info, u_id, u_data = struct.unpack(UDP_FMT, udp_pkt)
                if u_id != test_id or u_data != test_data:
                    stats["data_err"] += 1
                    continue
            except socket.timeout:
                stats["timeout"] += 1
                continue

            # --- 步骤 3: 原封不动发回桥接器 (往配置监听端口发送) ---
            udp_sock.sendto(udp_pkt, (server_ip, bridge_listen_port))

            # --- 步骤 4: 等待 CAN 回传 (从 vcan0 收) ---
            found_echo = False
            step4_start = time.time()
            while time.time() - step4_start < 0.5:
                try:
                    rx_frame, _ = can_sock.recvfrom(16)
                    r_id, r_dlc, _, r_data = struct.unpack(CAN_FMT, rx_frame)
                    if r_id == test_id and r_data == test_data:
                        found_echo = True
                        break
                except socket.timeout:
                    break
            
            if found_echo:
                stats["success"] += 1
            else:
                stats["timeout"] += 1

            # --- 速率与带宽计算 (每 0.5 秒更新一次显示) ---
            now = time.time()
            dt = now - last_report_time
            if dt >= 0.5:
                # 计算 PPS (每秒完成的完整闭环次数)
                current_success = stats["success"]
                pps = (current_success - stats["last_success"]) / dt
                
                # 计算 Mbps (13字节 * 8位 = 104 bits)
                mbps = (pps * 13 * 8) / 1000000
                
                success_rate = (stats["success"] / stats["total"]) * 100
                
                output = (
                    f"\r[测试中] 成功:{stats['success']:6d} | 失败:{stats['total']-stats['success']:4d} | "
                    f"成功率:{success_rate:5.1f}% | "
                    f"速率:{pps:6.1f} PPS | 带宽:{mbps:5.3f} Mbps | "
                    f"最后ID:0x{test_id:X}   "
                )
                sys.stdout.write(output)
                sys.stdout.flush()
                
                stats["last_success"] = current_success
                last_report_time = now
            
            # 如果想测试极限速率，可以减小 sleep 或删掉它
            time.sleep(0.00001)

    except KeyboardInterrupt:
        duration = time.time() - start_time
        print(f"\n\n--- 测试总结 ---")
        print(f"总耗时: {duration:.2f} 秒")
        print(f"总计尝试: {stats['total']}")
        print(f"完全闭环: {stats['success']}")
        print(f"平均速率: {stats['success']/duration:.1f} PPS")
        print(f"平均带宽: {(stats['success']*13*8)/(duration*1000000):.4f} Mbps")

def parse_args():
    parser = argparse.ArgumentParser(description="单通道 UDP↔CAN 乒乓回环测试")
    parser.add_argument("--port-index", type=int, default=0, help="选择 config.json 中的 ports 索引")
    parser.add_argument("--channel-index", type=int, default=0, help="选择 ports[*].channels 索引")
    return parser.parse_args()

if __name__ == "__main__":
    if os.getuid() != 0:
        print("请使用 sudo 运行此脚本")
    else:
        args = parse_args()
        run_ping_pong_test(args.port_index, args.channel_index)
