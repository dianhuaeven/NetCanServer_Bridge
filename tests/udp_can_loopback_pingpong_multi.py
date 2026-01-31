#!/usr/bin/env python3
"""
多端口 / 多通道版本的 UDP↔CAN 全链路乒乓测试。

脚本会按 config.json 中的 ports[*].channels[*] 自动创建 CAN + UDP 套接字，
每个通道都会在自身 ID 范围内随机选择 CAN ID 进行闭环测试：

    CAN(vcanX) -> Bridge(udp_listen_port) -> 脚本UDP(udp_send_port)
                 -> 回送 -> Bridge -> CAN(vcanX)

依赖：
  - Python 3.8+
  - sudo/root 权限访问 SocketCAN
  - 已创建 vcan 接口并与配置文件匹配
"""

import json
import os
import random
import socket
import struct
import sys
import threading
import time
from collections import defaultdict, deque
from typing import Optional, Tuple

UDP_STRUCT = struct.Struct(">BI8s")   # 13 字节 UDP 帧：Info + ID + Data
CAN_STRUCT = struct.Struct("=IB3s8s") # Linux SocketCAN 原始帧

CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF


def load_config() -> dict:
    try:
        with open("config.json", "r") as cfg:
            return json.load(cfg)
    except FileNotFoundError:
        print("错误: 当前目录未找到 config.json")
        sys.exit(1)


def resolve_port(port_cfg: dict, key: str) -> Optional[int]:
    return port_cfg.get(key) or port_cfg.get("udp_port")


def parse_id_value(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError(f"无法解析 ID: {value}")


class ChannelStats:
    def __init__(self, label: str, id_min: int, id_max: int) -> None:
        self.label = label
        self.id_min = id_min
        self.id_max = id_max
        self.total = 0
        self.success = 0
        self.udp_timeouts = 0
        self.can_timeouts = 0
        self.data_errors = 0
        self.send_errors = 0
        self.last_id = 0
        self.lock = threading.Lock()

    def record(self, outcome: str, test_id: int) -> None:
        with self.lock:
            self.total += 1
            self.last_id = test_id
            if outcome == "success":
                self.success += 1
            elif outcome == "udp_timeout":
                self.udp_timeouts += 1
            elif outcome == "can_timeout":
                self.can_timeouts += 1
            elif outcome == "data_error":
                self.data_errors += 1
            elif outcome == "send_error":
                self.send_errors += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "label": self.label,
                "total": self.total,
                "success": self.success,
                "udp_timeouts": self.udp_timeouts,
                "can_timeouts": self.can_timeouts,
                "data_errors": self.data_errors,
                "send_errors": self.send_errors,
                "last_id": self.last_id,
                "id_min": self.id_min,
                "id_max": self.id_max,
            }


class PortContext:
    def __init__(self, name: str, server_ip: str, listen_port: int, send_port: int, stop_event: threading.Event) -> None:
        self.name = name
        self.server_ip = server_ip
        self.listen_port = listen_port
        self.send_port = send_port
        self.stop_event = stop_event
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", send_port))
        self.sock.settimeout(0.2)
        self._mailbox = defaultdict(deque)
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()
        self._running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, name=f"udp-listener-{name}", daemon=True)

    def start(self) -> None:
        self._rx_thread.start()

    def _rx_loop(self) -> None:
        while self._running and not self.stop_event.is_set():
            try:
                packet, _ = self.sock.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(packet) < UDP_STRUCT.size:
                continue

            info, can_id, payload = UDP_STRUCT.unpack(packet[:UDP_STRUCT.size])
            with self._cond:
                self._mailbox[can_id].append((packet, info, can_id, payload))
                self._cond.notify_all()

    def wait_for_packet(self, can_id: int, timeout: float) -> Optional[Tuple[bytes, int, int, bytes]]:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                queue = self._mailbox.get(can_id)
                if queue:
                    item = queue.popleft()
                    if not queue:
                        del self._mailbox[can_id]
                    return item
                remaining = deadline - time.time()
                if remaining <= 0 or self.stop_event.is_set():
                    return None
                self._cond.wait(remaining)

    def send_to_bridge(self, payload: bytes) -> None:
        with self._send_lock:
            self.sock.sendto(payload, (self.server_ip, self.listen_port))

    def close(self) -> None:
        self._running = False
        with self._cond:
            self._cond.notify_all()
        try:
            self.sock.close()
        except OSError:
            pass
        self._rx_thread.join(timeout=1.0)


class ChannelTester(threading.Thread):
    def __init__(self, port_name: str, channel_idx: int, channel_cfg: dict, port_ctx: PortContext, stop_event: threading.Event) -> None:
        label = f"{channel_cfg['vcan_name']}@{port_name}#ch{channel_idx}"
        super().__init__(name=f"tester-{label}", daemon=True)
        self.channel_cfg = channel_cfg
        self.port_ctx = port_ctx
        self.stop_event = stop_event
        self.vcan_iface = channel_cfg["vcan_name"]
        id_range = channel_cfg.get("id_range") or {}
        self.id_min = parse_id_value(id_range.get("min", 0))
        self.id_max = parse_id_value(id_range.get("max", 0x7FF))
        self.extended = self.id_max > 0x7FF
        self.stats = ChannelStats(label, self.id_min, self.id_max)
        self.can_sock: Optional[socket.socket] = None

    def open(self) -> bool:
        try:
            self.can_sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            self.can_sock.settimeout(0.2)
            self.can_sock.bind((self.vcan_iface,))
            return True
        except OSError as exc:
            print(f"[{self.stats.label}] 无法绑定 CAN 接口 {self.vcan_iface}: {exc}")
            return False

    def run(self) -> None:
        if not self.open():
            return

        while not self.stop_event.is_set():
            base_id = random.randint(self.id_min, self.id_max)
            payload = os.urandom(8)
            frame_id = base_id | (CAN_EFF_FLAG if self.extended else 0)
            frame = CAN_STRUCT.pack(frame_id, 8, b"\x00\x00\x00", payload)
            outcome = "success"

            try:
                assert self.can_sock is not None
                self.can_sock.send(frame)
            except OSError as exc:
                print(f"[{self.stats.label}] CAN 发送失败: {exc}")
                outcome = "send_error"
                self.stats.record(outcome, base_id)
                time.sleep(0.2)
                continue

            udp_resp = self.port_ctx.wait_for_packet(base_id, 0.5)
            if udp_resp is None:
                outcome = "udp_timeout"
                self.stats.record(outcome, base_id)
                continue

            packet_bytes, info, packet_id, udp_payload = udp_resp
            if packet_id != base_id or udp_payload != payload:
                outcome = "data_error"
                self.stats.record(outcome, base_id)
                continue

            try:
                self.port_ctx.send_to_bridge(packet_bytes)
            except OSError as exc:
                print(f"[{self.stats.label}] UDP 回送失败: {exc}")
                outcome = "send_error"
                self.stats.record(outcome, base_id)
                continue

            if self._wait_for_can_echo(base_id, payload, timeout=0.5):
                outcome = "success"
            else:
                outcome = "can_timeout"

            self.stats.record(outcome, base_id)

        if self.can_sock:
            try:
                self.can_sock.close()
            except OSError:
                pass

    def _wait_for_can_echo(self, expected_id: int, expected_payload: bytes, timeout: float) -> bool:
        assert self.can_sock is not None
        deadline = time.time() + timeout
        while not self.stop_event.is_set():
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            self.can_sock.settimeout(remaining)
            try:
                frame_bytes, _ = self.can_sock.recvfrom(CAN_STRUCT.size)
            except socket.timeout:
                continue
            except OSError:
                return False
            can_id, dlc, _, data = CAN_STRUCT.unpack(frame_bytes)
            actual_id = can_id & CAN_EFF_MASK
            if actual_id == expected_id and data[:dlc] == expected_payload[:dlc]:
                return True
        return False


def reporter_loop(runners, interval: float, stop_event: threading.Event) -> None:
    last_success = {runner: 0 for runner in runners}
    while not stop_event.is_set():
        time.sleep(interval)
        lines = ["--- 多路乒乓统计 ---"]
        for runner in runners:
            snap = runner.stats.snapshot()
            prev = last_success[runner]
            pps = (snap["success"] - prev) / interval
            last_success[runner] = snap["success"]
            success_rate = (snap["success"] / snap["total"] * 100) if snap["total"] else 0.0
            lines.append(
                f"{snap['label']}: OK {snap['success']}/{snap['total']} | "
                f"成功率 {success_rate:5.1f}% | PPS {pps:6.1f} | "
                f"UDP超时 {snap['udp_timeouts']:5d} | CAN超时 {snap['can_timeouts']:5d} | "
                f"数据错 {snap['data_errors']:4d} | 发送错 {snap['send_errors']:4d} | "
                f"ID范围 [0x{snap['id_min']:X},0x{snap['id_max']:X}] | 最后ID 0x{snap['last_id']:X}"
            )
        print("\n".join(lines), flush=True)


def main() -> None:
    if os.getuid() != 0:
        print("请使用 sudo 运行此脚本（访问 CAN-Socket 需要 root 权限）")
        sys.exit(1)

    config = load_config()
    ports_cfg = config.get("ports")
    if not ports_cfg:
        print("错误: 配置中未找到任何 ports 条目")
        sys.exit(1)

    server_ip = config["server"]["ip"]
    stop_event = threading.Event()

    port_contexts = []
    for idx, port_cfg in enumerate(ports_cfg):
        listen_port = resolve_port(port_cfg, "udp_listen_port")
        send_port = resolve_port(port_cfg, "udp_send_port")
        if listen_port is None or send_port is None:
            print(f"错误: ports[{idx}] 缺少 udp_listen_port/udp_send_port (或 legacy udp_port)")
            sys.exit(1)
        port_ctx = PortContext(f"UDP{listen_port}->{send_port}", server_ip, listen_port, send_port, stop_event)
        port_ctx.start()
        port_contexts.append(port_ctx)

    runners = []
    for port_index, port_cfg in enumerate(ports_cfg):
        channels = port_cfg.get("channels") or []
        if not channels:
            print(f"警告: ports[{port_index}] 未配置 channels，跳过")
            continue
        port_ctx = port_contexts[port_index]
        for chan_idx, chan_cfg in enumerate(channels):
            runner = ChannelTester(f"UDP{port_ctx.listen_port}", chan_idx, chan_cfg, port_ctx, stop_event)
            runner.start()
            runners.append(runner)
            print(f"[启动] {runner.stats.label} | "
                  f"端口 {port_ctx.listen_port}/{port_ctx.send_port} | "
                  f"ID范围 [0x{runner.id_min:X},0x{runner.id_max:X}]")

    if not runners:
        print("错误: 未找到可用的通道配置，无法运行测试。")
        stop_event.set()
        for port_ctx in port_contexts:
            port_ctx.close()
        sys.exit(1)

    reporter = threading.Thread(target=reporter_loop, args=(runners, 1.0, stop_event), daemon=True)
    reporter.start()

    print("\n--- 多路乒乓测试已启动，按 Ctrl+C 结束 ---\n")
    try:
        while any(runner.is_alive() for runner in runners):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到中断信号，正在停止测试...")
    finally:
        stop_event.set()
        for runner in runners:
            runner.join(timeout=1.0)
        for port_ctx in port_contexts:
            port_ctx.close()
        reporter.join(timeout=1.0)

        print("\n--- 测试总结 ---")
        for runner in runners:
            snap = runner.stats.snapshot()
            success_rate = (snap["success"] / snap["total"] * 100) if snap["total"] else 0.0
            print(
                f"{snap['label']}: OK {snap['success']}/{snap['total']} "
                f"({success_rate:5.1f}%) | UDP超时 {snap['udp_timeouts']} | "
                f"CAN超时 {snap['can_timeouts']} | 数据错 {snap['data_errors']} | 发送错 {snap['send_errors']}"
            )


if __name__ == "__main__":
    main()
