import argparse
import json
import socket
import struct

def load_default_port():
    try:
        with open("config.json", "r") as cfg:
            config = json.load(cfg)
            port_cfg = config["ports"][0]
            return port_cfg.get("udp_listen_port") or port_cfg.get("udp_port", 5555)
    except Exception:
        return 5555

def start_udp_receiver(bind_ip: str, port: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind_ip, port))

    print(f"监听 {bind_ip}:{port} 上的大端序 13 字节 UDP 包...")

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
        print(f"来源: {addr[0]}:{addr[1]}")
        print("-" * 30)

def parse_args():
    parser = argparse.ArgumentParser(description="监听并解析桥接器的 UDP 报文")
    parser.add_argument("--bind-ip", default="0.0.0.0", help="绑定 IP (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=load_default_port(), help="监听端口 (默认读取 config.json)")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    start_udp_receiver(args.bind_ip, args.port)
