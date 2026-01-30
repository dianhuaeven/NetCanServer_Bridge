import socket
import struct
import json

def start_udp_receiver():
    # 假设配置端口是 5555
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 5555))

    print("监听大端序 13 字节 UDP 包...")

    while True:
        data, addr = sock.recvfrom(1024)
        if len(data) != 13:
            continue

        # ">" 代表大端序，B 是 1字节信息，I 是 4字节整型 ID，8s 是 8字节数据
        info, can_id, can_data = struct.unpack(">BI8s", data)

        is_ext = bool(info & 0x80)
        dlc = info & 0x0F
        actual_data = can_data[:dlc]

        print(f"UDP 原始数据: {data.hex(' ')}")
        print(f"解析 -> ID: 0x{can_id:X}, DLC: {dlc}, 数据: {actual_data.hex(' ')}")
        print("-" * 30)

if __name__ == "__main__":
    start_udp_receiver()
