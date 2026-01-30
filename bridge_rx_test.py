import socket
import struct
import time
import threading
import json
import sys
import os
import random

# --- 协议定义 ---
UDP_PROTOCOL_FMT = ">BI8s" # 13字节大端序协议 (1字节Info + 4字节ID + 8字节Data)
CAN_FRAME_FMT = "=IB3s8s"  # Linux 内核标准 CAN 帧 (16字节)

# 全局计数器
stats = {
    "udp_sent": 0,
    "can_rcvd": 0,
    "errors": 0,
    "last_id": 0,
    "current_pps": 0.0
}

def load_config():
    try:
        with open("config.json", 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print("错误: 当前目录下未找到 config.json")
        sys.exit(1)

# --- 线程 1: UDP 发送端 (模拟外部输入) ---
def udp_sender_thread(ip, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # 预生成 100 个随机包提高发送效率
    packets = []
    for i in range(100):
        can_id = random.randint(0, 0x7FF)
        # 13字节协议: Info=0x08(标准帧,长度8), ID, Data
        data = bytes([random.randint(0, 255) for _ in range(8)])
        packets.append(struct.pack(UDP_PROTOCOL_FMT, 0x08, can_id, data))

    print(f"UDP 发送线程启动，目标 -> {ip}:{port}")
    
    while True:
        try:
            # 向桥接器的监听端口发包
            sock.sendto(random.choice(packets), (ip, port))
            stats["udp_sent"] += 1
            # 调节发送频率：0.0001 约等于 10000 PPS
            time.sleep(0.00000001) 
        except Exception as e:
            print(f"UDP 发送异常: {e}")
            break

# --- 线程 2: CAN 接收与校验 (在 vcan0 上收) ---
def can_monitor_thread(iface):
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        # 增大接收缓冲区避免内核丢包
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        s.bind((iface,))
    except Exception as e:
        print(f"无法打开 CAN 接口 {iface}: {e}")
        return

    while True:
        try:
            cf, addr = s.recvfrom(16) # 接收 16 字节内核帧
            can_id, dlc, _, data = struct.unpack(CAN_FRAME_FMT, cf)
            
            # 简单校验
            if dlc > 8:
                stats["errors"] += 1
            else:
                stats["can_rcvd"] += 1
                stats["last_id"] = can_id & 0x1FFFFFFF # 去掉标志位
        except Exception:
            break

def main():
    if os.getuid() != 0:
        print("错误: 请使用 sudo 运行此脚本以访问 SocketCAN。")
        sys.exit(1)

    config = load_config()
    vcan_iface = config['ports'][0]['channels'][0]['vcan_name']
    udp_port = config['ports'][0]['udp_port']

    print(f"--- 桥接器 RX 路径全链路压测 ---")
    print(f"流向: [Python UDP 发送] -> UDP {udp_port} -> [Bridge] -> {vcan_iface} -> [Python CAN 接收]")
    
    # 启动线程
    t_udp = threading.Thread(target=udp_sender_thread, args=("127.0.0.1", udp_port), daemon=True)
    t_can = threading.Thread(target=can_monitor_thread, args=(vcan_iface,), daemon=True)
    
    t_udp.start()
    t_can.start()

    last_rcvd = 0
    last_sent = 0
    last_time = time.time()
    start_time = last_time

    try:
        while True:
            time.sleep(1.0)
            now = time.time()
            dt = now - last_time
            if dt <= 0:
                continue

            curr_sent = stats["udp_sent"]
            curr_rcvd = stats["can_rcvd"]

            # 计算 PPS (每秒包数)
            tx_pps = (curr_sent - last_sent) / dt
            rx_pps = (curr_rcvd - last_rcvd) / dt
            
            # 计算 Mbps (每秒兆比特)
            # 协议长度 13 字节 * 8 位 = 104 bits 每包
            tx_mbps = (tx_pps * 13 * 8) / 1000000
            rx_mbps = (rx_pps * 13 * 8) / 1000000
            
            loss = (1 - curr_rcvd / curr_sent) * 100 if curr_sent > 0 else 0
            
            # 格式化输出，增加 Mbps 显示
            output = (
                f"\r[RX压测] 时间:{now-start_time:4.1f}s | "
                f"发(UDP):{curr_sent:7d} | 收(CAN):{curr_rcvd:7d} | "
                f"丢包:{loss:5.2f}% | 错误:{stats['errors']} | "
                f"TX:{tx_pps:7.1f} PPS ({tx_mbps:5.2f} Mbps) | "
                f"RX:{rx_pps:7.1f} PPS ({rx_mbps:5.2f} Mbps)  "
            )
            sys.stdout.write(output)
            sys.stdout.flush()
            
            last_rcvd = curr_rcvd
            last_sent = curr_sent
            last_time = now

    except KeyboardInterrupt:
        print(f"\n\n测试结束。总计还原 CAN 帧: {stats['can_rcvd']}")

if __name__ == "__main__":
    main()
