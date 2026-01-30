import socket
import struct
import random
import time

# CAN 帧结构体格式: <I (4字节ID), B (1字节长度), 3s (3字节填充), 8s (8字节数据)
CAN_FRAME_FMT = "=IB3s8s"

# 某些 Python 环境下可能没有定义这些常量，手动定义以防万一
if not hasattr(socket, 'AF_CAN'):
    socket.AF_CAN = 29
if not hasattr(socket, 'CAN_RAW'):
    socket.CAN_RAW = 1

def send_random_can(iface="vcan0"):
    try:
        # 创建 CAN 原始套接字 - 注意这里使用 socket.CAN_RAW
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    except AttributeError:
        print("错误: 你的 Python 环境不支持 AF_CAN (通常只在 Linux 上支持)。")
        return
    except OSError as e:
        print(f"创建套接字失败: {e}")
        return

    try:
        s.bind((iface,))
    except OSError as e:
        print(f"绑定接口 {iface} 失败: {e}")
        print("请检查接口是否存在，或尝试使用 'sudo' 运行。")
        return

    print(f"已连接到 {iface}，开始发送随机 CAN 包 (Ctrl+C 停止)...")

    try:
        while True:
            can_id = random.randint(0x000, 0x7FF)
            data_len = random.randint(1, 8)
            data = bytes([random.randint(0, 255) for _ in range(data_len)])
            
            # 补齐 8 字节数据
            data_padded = data.ljust(8, b'\x00')
            
            # 打包成 Linux 内核标准的 can_frame 结构
            frame = struct.pack(CAN_FRAME_FMT, can_id, data_len, b'\x00\x00\x00', data_padded)
            
            s.send(frame)
            print(f"已发送: ID=0x{can_id:03X} DLC={data_len} Data={data.hex(' ')}")
            
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止发送。")
    finally:
        s.close()

if __name__ == "__main__":
    send_random_can("vcan0")
