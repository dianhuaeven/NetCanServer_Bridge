import argparse
import json
import os
import socket
import struct
import sys
import threading
import time
# --- 协议定义 ---
CAN_FRAME_FMT = "=IB3s8s"  # Linux 内核标准 CAN 帧 (16字节)
UDP_PROTOCOL_FMT = ">BI8s" # 13字节大端序协议 (1字节Info + 4字节ID + 8字节Data)

# 全局计数器
stats = {
    "can_sent": 0,
    "udp_rcvd": 0,
    "errors": 0,
    "last_id": 0,
    "current_pps": 0.0,
    "current_mbps": 0.0
}

def load_config():
    try:
        with open("config.json", 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print("错误: 当前目录下未找到 config.json")
        sys.exit(1)

# --- 线程 1: UDP 接收与校验 (从桥接器收) ---
def udp_monitor_thread(ip, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # 增加系统接收缓冲区，防止压测时系统层面丢包
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    sock.bind((ip, port))
    
    while True:
        try:
            data, _ = sock.recvfrom(2048)
            if len(data) != 13:
                stats["errors"] += 1
                continue
            
            # 解析大端序报文
            info, can_id, _ = struct.unpack(UDP_PROTOCOL_FMT, data)
            if (info & 0x0F) > 8:
                stats["errors"] += 1
            else:
                stats["udp_rcvd"] += 1
                stats["last_id"] = can_id
        except Exception:
            break

# --- 线程 2: CAN 发送端 (向 vcan0 发) ---
def can_sender_thread(iface):
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((iface,))
    except Exception as e:
        print(f"\n无法打开 CAN 接口 {iface}: {e}")
        return

    # 预生成数据包提高效率
    frames = []
    for i in range(100):
        # 模拟不同的 ID 和数据
        frames.append(struct.pack(CAN_FRAME_FMT, i, 8, b'\x00\x00\x00', bytes([i]*8)))

    while True:
        try:
            s.send(frames[stats["can_sent"] % 100])
            stats["can_sent"] += 1
            # 调节此处的 sleep 可以改变压测强度
            # time.sleep(0.00001) # 极高频率
            time.sleep(0.00001)  # 约 10000 PPS
        except OSError:
            # 如果缓冲区满了，稍等一下
            time.sleep(0.01)

def resolve_port(port_cfg, key):
    return port_cfg.get(key) or port_cfg.get("udp_port")

def main():
    if os.getuid() != 0:
        print("错误: 请使用 sudo 运行此脚本以访问 SocketCAN。")
        sys.exit(1)

    args = parse_args()
    config = load_config()
    ports = config.get('ports') or []
    if not ports:
        print("错误: config.json 中未配置 ports")
        sys.exit(1)
    if args.port_index < 0 or args.port_index >= len(ports):
        print(f"错误: port 索引 {args.port_index} 超出范围 (0-{len(ports)-1})")
        sys.exit(1)

    port_cfg = ports[args.port_index]
    channels = port_cfg.get('channels') or []
    if not channels:
        print(f"错误: ports[{args.port_index}] 未配置 channels")
        sys.exit(1)
    if args.channel_index < 0 or args.channel_index >= len(channels):
        print(f"错误: channel 索引 {args.channel_index} 超出范围 (0-{len(channels)-1})")
        sys.exit(1)

    channel_cfg = channels[args.channel_index]
    vcan_iface = args.iface or channel_cfg['vcan_name']
    udp_ip = config['server']['ip']
    udp_port = resolve_port(port_cfg, "udp_send_port")
    if udp_port is None:
        print("错误: 配置中缺少 udp_send_port/udp_port")
        sys.exit(1)

    print(f"--- 桥接器 TX 路径全链路压测 ---")
    print(f"流向: [Python] -> {vcan_iface} -> [Bridge] -> UDP {udp_ip}:{udp_port} -> [Python]")
    print(f"使用 ports[{args.port_index}], channels[{args.channel_index}]")
    
    # 启动线程
    t_udp = threading.Thread(target=udp_monitor_thread, args=(udp_ip, udp_port), daemon=True)
    t_can = threading.Thread(target=can_sender_thread, args=(vcan_iface,), daemon=True)
    
    t_udp.start()
    t_can.start()

    last_rcvd = 0
    last_time = time.time()
    start_time = last_time

    try:
        while True:
            time.sleep(1.0)
            
            # 计算瞬时速率
            now = time.time()
            dt = now - last_time
            curr_rcvd = stats["udp_rcvd"]
            
            pps = (curr_rcvd - last_rcvd) / dt
            # 13字节 * 8位 = 每个包的比特数
            mbps = (pps * 13 * 8) / (1024 * 1024)
            
            last_rcvd = curr_rcvd
            last_time = now
            
            # 计算累计丢包率
            sent = stats["can_sent"]
            loss = (1 - curr_rcvd/sent)*100 if sent > 0 else 0
            
            # 打印统计信息
            # PPS: 每秒包数, Mbps: 每秒兆比特流量
            output = (
                f"\r[运行中] 时间:{now-start_time:4.1f}s | "
                f"发(CAN):{sent:7d} | 收(UDP):{curr_rcvd:7d} | "
                f"丢包:{loss:5.2f}% | 错误:{stats['errors']} | "
                f"速率:{pps:8.1f} PPS | 带宽:{mbps:5.2f} Mbps | "
                f"最后ID:0x{stats['last_id']:X}   "
            )
            sys.stdout.write(output)
            sys.stdout.flush()

    except KeyboardInterrupt:
        print(f"\n\n测试结束。总计接收: {stats['udp_rcvd']} 帧。")

def parse_args():
    parser = argparse.ArgumentParser(description="CAN→UDP 压测脚本")
    parser.add_argument("--port-index", type=int, default=0, help="选择 config.json 中的 ports 索引")
    parser.add_argument("--channel-index", type=int, default=0, help="选择 ports[*].channels 索引")
    parser.add_argument("--iface", help="覆盖使用的 CAN 接口名称")
    return parser.parse_args()

if __name__ == "__main__":
    main()
