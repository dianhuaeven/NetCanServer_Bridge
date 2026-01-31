import argparse
import socket
import struct
import time
import random

# Linux 内核标准 CAN 帧格式 (16字节)
CAN_FRAME_FMT = "=IB3s8s"

def stress_can_sender(iface="vcan0"):
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((iface,))
    except Exception as e:
        print(f"错误: 无法连接 CAN 接口 {e}")
        return

    print(f"正在向 {iface} 高频灌入数据... (Ctrl+C 停止)")
    
    # 预生成 100 个不同的包提高发送效率
    frames = []
    for _ in range(100):
        can_id = random.randint(0, 0x7FF)
        dlc = 8
        data = bytes([random.randint(0, 255) for _ in range(8)])
        frames.append(struct.pack(CAN_FRAME_FMT, can_id, dlc, b'\x00\x00\x00', data))

    count = 0
    start_time = time.time()
    
    try:
        while True:
            # 全速循环发送
            s.send(random.choice(frames))
            count += 1
            if count % 5000 == 0:
                print(f"\r已向 CAN 发送: {count} 帧", end="")
    except KeyboardInterrupt:
        duration = time.time() - start_time
        print(f"\n发送结束。平均速率: {count/duration:.2f} 帧/秒")
    finally:
        s.close()

def parse_args():
    parser = argparse.ArgumentParser(description="向指定 CAN 接口持续灌入随机帧")
    parser.add_argument("--iface", default="vcan0", help="CAN 接口名称 (默认: vcan0)")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    stress_can_sender(args.iface)
