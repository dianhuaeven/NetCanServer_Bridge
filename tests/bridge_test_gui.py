#!/usr/bin/env python3
"""
Bridge Integrated Test GUI

将 tests/ 目录的所有测试用例整合到一个 GUI 程序中，统一提供：
    - UDP→CAN 压测
    - CAN→UDP 压测
    - 单通道 / 多通道乒乓
    - CAN Flood / 随机 CAN 发送器
    - UDP 原始帧抓取

特点：
    * 相同的核心 Worker 供 GUI 与 CLI 共享，减少重复代码。
    * 统一的参数输入与状态面板，支持实时折线图展示 PPS / 成功率等指标。
    * 无需再启动子脚本，所有功能在同一进程中运行。

使用方式：
    sudo python3 tests/bridge_test_gui.py
"""

from __future__ import annotations

import json
import os
import queue
import random
import select
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

UDP_FRAME = struct.Struct(">BI8s")
CAN_FRAME = struct.Struct("=IB3s8s")
CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.json"


# ---------------------------------------------------------------------------
# 配置解析与通用工具
# ---------------------------------------------------------------------------

def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    try:
        with open(path, "r") as cfg:
            return json.load(cfg)
    except FileNotFoundError:
        raise RuntimeError(f"未找到配置文件: {path}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置文件解析失败: {exc}")


def resolve_port(port_cfg: dict, key: str) -> Optional[int]:
    return port_cfg.get(key) or port_cfg.get("udp_port")


def ensure_channel(config: dict, port_index: int, ch_index: int) -> Tuple[dict, dict]:
    ports = config.get("ports") or []
    if port_index < 0 or port_index >= len(ports):
        raise RuntimeError(f"port 索引 {port_index} 无效 (0-{len(ports)-1})")
    port_cfg = ports[port_index]
    channels = port_cfg.get("channels") or []
    if ch_index < 0 or ch_index >= len(channels):
        raise RuntimeError(f"channel 索引 {ch_index} 无效 (0-{len(channels)-1})")
    return port_cfg, channels[ch_index]


def make_can_socket(iface: str, timeout: float = 0.1) -> socket.socket:
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.settimeout(timeout)
    sock.bind((iface,))
    return sock


def make_udp_socket(bind_ip: str = "0.0.0.0", bind_port: Optional[int] = None, timeout: float = 0.1) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if bind_port is not None:
        sock.bind((bind_ip, bind_port))
    sock.settimeout(timeout)
    return sock


def clamp_positive(value: float, minimum: float = 0.0) -> float:
    return max(value, minimum)


def now_ts() -> str:
    return time.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Worker 抽象
# ---------------------------------------------------------------------------


class BaseTestWorker(threading.Thread):
    def __init__(
        self,
        name: str,
        params: dict,
        stats_cb: Callable[[Dict[str, float]], None],
        log_cb: Callable[[str], None],
        status_cb: Callable[[str], None],
    ):
        super().__init__(name=name, daemon=True)
        self.params = params
        self._stats_cb = stats_cb
        self._log_cb = log_cb
        self._status_cb = status_cb
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def log(self, message: str):
        self._log_cb(f"[{now_ts()}] {message}")

    def publish_stats(self, stats: Dict[str, float]):
        self._stats_cb(stats)

    def run(self):
        self._status_cb("运行中")
        try:
            self.setup()
            self.loop()
        except Exception as exc:
            self.log(f"异常: {exc}")
        finally:
            try:
                self.teardown()
            finally:
                self._status_cb("已停止")

    # 子类覆盖
    def setup(self):
        pass

    def loop(self):
        pass

    def teardown(self):
        pass


# ---------------------------------------------------------------------------
# 具体 Worker 实现
# ---------------------------------------------------------------------------


class UdpToCanWorker(BaseTestWorker):
    def setup(self):
        config = load_config()
        port_cfg, channel_cfg = ensure_channel(
            config,
            int(self.params["port_index"]),
            int(self.params["channel_index"]),
        )
        self.vcan_iface = self.params.get("iface") or channel_cfg["vcan_name"]
        self.server_ip = config["server"]["ip"]
        self.bridge_port = resolve_port(port_cfg, "udp_listen_port")
        if self.bridge_port is None:
            raise RuntimeError("配置缺少 udp_listen_port/udp_port")
        self.rate = clamp_positive(float(self.params.get("pps", 5000)), 1.0)

        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setblocking(False)
        self.target_addr = (self.server_ip, self.bridge_port)

        self.can_sock = make_can_socket(self.vcan_iface, timeout=0.05)
        self.can_sock.setblocking(False)

        self.stats = {
            "udp_sent": 0,
            "can_rcvd": 0,
            "errors": 0,
            "pps": 0.0,
            "loss": 0.0,
        }
        self.last_report = time.time()
        self.prev_can = 0
        self.prev_udp = 0

    def loop(self):
        interval = 1.0 / self.rate
        next_send = time.time()
        while not self.stopped():
            now = time.time()
            if now >= next_send:
                packet = UDP_FRAME.pack(0x08, random.randint(0, 0x7FF), os.urandom(8))
                try:
                    self.udp_sock.sendto(packet, self.target_addr)
                    self.stats["udp_sent"] += 1
                except OSError as exc:
                    self.log(f"UDP 发送失败: {exc}")
                    self.stats["errors"] += 1
                next_send = now + interval

            try:
                cf = self.can_sock.recv(CAN_FRAME.size)
                self.stats["can_rcvd"] += 1
            except BlockingIOError:
                pass
            except socket.timeout:
                pass
            except OSError as exc:
                self.log(f"CAN 接收失败: {exc}")
                self.stats["errors"] += 1

            if now - self.last_report >= 1.0:
                dt = now - self.last_report
                can_delta = self.stats["can_rcvd"] - self.prev_can
                udp_delta = self.stats["udp_sent"] - self.prev_udp
                self.prev_can = self.stats["can_rcvd"]
                self.prev_udp = self.stats["udp_sent"]
                pps = can_delta / dt if dt else 0.0
                loss = 0.0
                if udp_delta > 0:
                    loss = max(0.0, 1.0 - (can_delta / udp_delta)) * 100.0
                self.stats.update({"pps": pps, "loss": loss})
                self.publish_stats(self.stats.copy())
                self.last_report = now

            time.sleep(0.001)

    def teardown(self):
        for sock in (self.udp_sock, self.can_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass


class CanToUdpWorker(BaseTestWorker):
    def setup(self):
        config = load_config()
        port_cfg, channel_cfg = ensure_channel(
            config,
            int(self.params["port_index"]),
            int(self.params["channel_index"]),
        )
        self.vcan_iface = self.params.get("iface") or channel_cfg["vcan_name"]
        self.server_ip = config["server"]["ip"]
        self.udp_port = resolve_port(port_cfg, "udp_send_port")
        if self.udp_port is None:
            raise RuntimeError("配置缺少 udp_send_port/udp_port")
        self.rate = clamp_positive(float(self.params.get("pps", 5000)), 1.0)

        self.can_sock = make_can_socket(self.vcan_iface, timeout=0.05)
        self.can_sock.setblocking(False)

        self.udp_sock = make_udp_socket(bind_ip=self.server_ip, bind_port=self.udp_port, timeout=0.05)

        self.stats = {
            "can_sent": 0,
            "udp_rcvd": 0,
            "errors": 0,
            "pps": 0.0,
            "loss": 0.0,
        }
        self.last_report = time.time()
        self.prev_udp = 0
        self.prev_can = 0

    def loop(self):
        interval = 1.0 / self.rate
        next_send = time.time()
        while not self.stopped():
            now = time.time()
            if now >= next_send:
                payload = bytes([self.stats["can_sent"] % 256] * 8)
                frame = CAN_FRAME.pack(self.stats["can_sent"] % 0x7FF, 8, b"\x00\x00\x00", payload)
                try:
                    self.can_sock.send(frame)
                    self.stats["can_sent"] += 1
                except OSError as exc:
                    self.log(f"CAN 发送失败: {exc}")
                    self.stats["errors"] += 1
                next_send = now + interval

            try:
                packet, _ = self.udp_sock.recvfrom(UDP_FRAME.size)
                if len(packet) == UDP_FRAME.size:
                    self.stats["udp_rcvd"] += 1
            except socket.timeout:
                pass
            except OSError as exc:
                self.log(f"UDP 接收失败: {exc}")
                self.stats["errors"] += 1

            if now - self.last_report >= 1.0:
                dt = now - self.last_report
                udp_delta = self.stats["udp_rcvd"] - self.prev_udp
                can_delta = self.stats["can_sent"] - self.prev_can
                self.prev_udp = self.stats["udp_rcvd"]
                self.prev_can = self.stats["can_sent"]
                pps = udp_delta / dt if dt else 0.0
                loss = 0.0
                if can_delta > 0:
                    loss = max(0.0, 1.0 - (udp_delta / can_delta)) * 100.0
                self.stats.update({"pps": pps, "loss": loss})
                self.publish_stats(self.stats.copy())
                self.last_report = now

            time.sleep(0.001)

    def teardown(self):
        for sock in (self.can_sock, self.udp_sock):
            try:
                sock.close()
            except OSError:
                pass


class PingPongWorker(BaseTestWorker):
    def setup(self):
        config = load_config()
        port_cfg, channel_cfg = ensure_channel(
            config,
            int(self.params["port_index"]),
            int(self.params["channel_index"]),
        )
        self.vcan_iface = channel_cfg["vcan_name"]
        self.server_ip = config["server"]["ip"]
        self.bridge_listen_port = resolve_port(port_cfg, "udp_listen_port")
        self.script_listen_port = resolve_port(port_cfg, "udp_send_port")
        if self.bridge_listen_port is None or self.script_listen_port is None:
            raise RuntimeError("配置缺少 udp_listen_port/udp_send_port")

        self.can_sock = make_can_socket(self.vcan_iface, timeout=0.05)
        self.udp_sock = make_udp_socket(bind_port=self.script_listen_port, timeout=0.05)

        self.stats = {
            "total": 0,
            "success": 0,
            "udp_timeout": 0,
            "can_timeout": 0,
            "data_err": 0,
            "pps": 0.0,
        }
        self.last_report = time.time()
        self.prev_success = 0

    def loop(self):
        while not self.stopped():
            self.stats["total"] += 1
            test_id = random.randint(0x100, 0x600)
            payload = os.urandom(8)
            frame = CAN_FRAME.pack(test_id, 8, b"\x00\x00\x00", payload)

            try:
                self.can_sock.send(frame)
            except OSError as exc:
                self.log(f"CAN 发送失败: {exc}")
                self.stats["data_err"] += 1
                time.sleep(0.05)
                continue

            udp_pkt = None
            try:
                udp_pkt, _ = self.udp_sock.recvfrom(UDP_FRAME.size)
                info, recv_id, data = UDP_FRAME.unpack(udp_pkt)
                if recv_id != test_id or data != payload:
                    self.stats["data_err"] += 1
                    udp_pkt = None
            except socket.timeout:
                self.stats["udp_timeout"] += 1
            except OSError as exc:
                self.log(f"UDP 接收失败: {exc}")
                self.stats["udp_timeout"] += 1

            if not udp_pkt:
                continue

            try:
                self.udp_sock.sendto(udp_pkt, (self.server_ip, self.bridge_listen_port))
            except OSError as exc:
                self.log(f"UDP 回送失败: {exc}")
                self.stats["data_err"] += 1
                continue

            success = False
            start_wait = time.time()
            while not self.stopped() and time.time() - start_wait < 0.5:
                try:
                    rx_frame = self.can_sock.recv(CAN_FRAME.size)
                    r_id, _, _, r_data = CAN_FRAME.unpack(rx_frame)
                    if r_id == test_id and r_data == payload:
                        success = True
                        break
                except socket.timeout:
                    pass
                except OSError:
                    break
            if success:
                self.stats["success"] += 1
            else:
                self.stats["can_timeout"] += 1

            now = time.time()
            if now - self.last_report >= 1.0:
                dt = now - self.last_report
                success_delta = self.stats["success"] - self.prev_success
                self.prev_success = self.stats["success"]
                pps = success_delta / dt if dt else 0.0
                self.stats["pps"] = pps
                self.publish_stats(self.stats.copy())
                self.last_report = now

    def teardown(self):
        for sock in (self.can_sock, self.udp_sock):
            try:
                sock.close()
            except OSError:
                pass


class MultiPingPongWorker(BaseTestWorker):
    def setup(self):
        self.config = load_config()
        self.stop_event = threading.Event()
        self.port_contexts = []
        self.channel_threads: List[threading.Thread] = []
        self.stats_map: Dict[str, dict] = {}
        self.lock = threading.Lock()
        self._build_channels()

    def _build_channels(self):
        server_ip = self.config["server"]["ip"]
        ports_cfg = self.config.get("ports") or []
        if not ports_cfg:
            raise RuntimeError("config.json 未配置 ports")

        for port_idx, port_cfg in enumerate(ports_cfg):
            listen_port = resolve_port(port_cfg, "udp_listen_port")
            send_port = resolve_port(port_cfg, "udp_send_port")
            if listen_port is None or send_port is None:
                raise RuntimeError(f"ports[{port_idx}] 缺少 udp 端口")
            udp_sock = make_udp_socket(bind_port=send_port, timeout=0.05)
            udp_sock.setblocking(False)
            port_ctx = {
                "sock": udp_sock,
                "listen_port": listen_port,
                "send_port": send_port,
                "server_ip": server_ip,
            }
            self.port_contexts.append(port_ctx)

            channels = port_cfg.get("channels") or []
            for chan_idx, chan_cfg in enumerate(channels):
                label = f"{chan_cfg['vcan_name']}@{listen_port}->{send_port}"
                stats = {
                    "label": label,
                    "success": 0,
                    "total": 0,
                    "udp_timeout": 0,
                    "can_timeout": 0,
                    "data_err": 0,
                    "pps": 0.0,
                }
                self.stats_map[label] = stats
                t = threading.Thread(
                    target=self._channel_loop,
                    args=(port_ctx, chan_cfg, stats),
                    daemon=True,
                )
                t.start()
                self.channel_threads.append(t)

        if not self.channel_threads:
            raise RuntimeError("未找到任何 channel 配置")

    def _channel_loop(self, port_ctx, chan_cfg, stats):
        iface = chan_cfg["vcan_name"]
        can_sock = make_can_socket(iface, timeout=0.05)
        udp_sock = port_ctx["sock"]
        server_ip = port_ctx["server_ip"]
        listen_port = port_ctx["listen_port"]

        id_range = chan_cfg.get("id_range") or {}
        id_min = int(id_range.get("min", 0), 0) if isinstance(id_range.get("min"), str) else id_range.get("min", 0)
        id_max = int(id_range.get("max", 0x7FF), 0) if isinstance(id_range.get("max"), str) else id_range.get("max", 0x7FF)
        extended = id_max > 0x7FF
        last_report = time.time()
        prev_success = 0

        while not self.stopped() and not self.stop_event.is_set():
            stats["total"] += 1
            base_id = random.randint(id_min, id_max)
            payload = os.urandom(8)
            frame_id = base_id | (CAN_EFF_FLAG if extended else 0)
            frame = CAN_FRAME.pack(frame_id, 8, b"\x00\x00\x00", payload)

            try:
                can_sock.send(frame)
            except OSError:
                stats["data_err"] += 1
                time.sleep(0.05)
                continue

            udp_pkt = None
            try:
                udp_pkt, _ = udp_sock.recvfrom(UDP_FRAME.size)
                info, recv_id, data = UDP_FRAME.unpack(udp_pkt)
                if recv_id != base_id or data != payload:
                    stats["data_err"] += 1
                    udp_pkt = None
            except socket.timeout:
                stats["udp_timeout"] += 1
            except BlockingIOError:
                pass
            except OSError:
                stats["udp_timeout"] += 1

            if not udp_pkt:
                continue

            try:
                udp_sock.sendto(udp_pkt, (server_ip, listen_port))
            except OSError:
                stats["data_err"] += 1
                continue

            success = False
            start_wait = time.time()
            while time.time() - start_wait < 0.5 and not self.stopped():
                try:
                    rx_frame = can_sock.recv(CAN_FRAME.size)
                    r_id, _, _, r_data = CAN_FRAME.unpack(rx_frame)
                    rid = r_id & CAN_EFF_MASK
                    if rid == base_id and r_data == payload:
                        success = True
                        break
                except socket.timeout:
                    pass
                except BlockingIOError:
                    pass
            if success:
                stats["success"] += 1
            else:
                stats["can_timeout"] += 1

            now = time.time()
            if now - last_report >= 1.0:
                dt = now - last_report
                suc_delta = stats["success"] - prev_success
                prev_success = stats["success"]
                stats["pps"] = suc_delta / dt if dt else 0.0
                last_report = now

        try:
            can_sock.close()
        except OSError:
            pass

    def loop(self):
        last_publish = time.time()
        while not self.stopped():
            if time.time() - last_publish >= 1.0:
                agg = {
                    "success": sum(s["success"] for s in self.stats_map.values()),
                    "total": sum(s["total"] for s in self.stats_map.values()),
                    "udp_timeout": sum(s["udp_timeout"] for s in self.stats_map.values()),
                    "can_timeout": sum(s["can_timeout"] for s in self.stats_map.values()),
                    "data_err": sum(s["data_err"] for s in self.stats_map.values()),
                    "pps": sum(s["pps"] for s in self.stats_map.values()),
                }
                self.publish_stats(agg)
                last_publish = time.time()
            time.sleep(0.2)

    def teardown(self):
        self.stop_event.set()
        for port_ctx in self.port_contexts:
            try:
                port_ctx["sock"].close()
            except OSError:
                pass
        for t in self.channel_threads:
            t.join(timeout=0.5)


class CanFloodWorker(BaseTestWorker):
    def setup(self):
        iface = self.params.get("iface") or "vcan0"
        self.can_sock = make_can_socket(iface, timeout=0.0)
        self.can_sock.setblocking(False)
        self.stats = {"sent": 0, "pps": 0.0}
        self.last_report = time.time()
        self.prev_sent = 0

    def loop(self):
        while not self.stopped():
            frame = CAN_FRAME.pack(random.randint(0, 0x7FF), 8, b"\x00\x00\x00", os.urandom(8))
            try:
                self.can_sock.send(frame)
                self.stats["sent"] += 1
            except BlockingIOError:
                time.sleep(0.0005)
            except OSError as exc:
                self.log(f"发送失败: {exc}")
                time.sleep(0.01)

            now = time.time()
            if now - self.last_report >= 1.0:
                dt = now - self.last_report
                sent_delta = self.stats["sent"] - self.prev_sent
                self.prev_sent = self.stats["sent"]
                self.stats["pps"] = sent_delta / dt if dt else 0.0
                self.publish_stats(self.stats.copy())
                self.last_report = now

    def teardown(self):
        try:
            self.can_sock.close()
        except OSError:
            pass


class RandomCanWorker(BaseTestWorker):
    def setup(self):
        iface = self.params.get("iface") or "vcan0"
        self.interval = clamp_positive(float(self.params.get("interval", 1.0)), 0.001)
        self.can_sock = make_can_socket(iface, timeout=0.0)
        self.stats = {"sent": 0, "pps": 0.0}
        self.last_report = time.time()
        self.prev_sent = 0

    def loop(self):
        while not self.stopped():
            dlc = random.randint(1, 8)
            data = os.urandom(dlc).ljust(8, b"\x00")
            frame = CAN_FRAME.pack(random.randint(0, 0x7FF), dlc, b"\x00\x00\x00", data)
            try:
                self.can_sock.send(frame)
                self.stats["sent"] += 1
            except OSError as exc:
                self.log(f"发送失败: {exc}")
                time.sleep(0.1)
            time.sleep(self.interval)

            now = time.time()
            if now - self.last_report >= 1.0:
                dt = now - self.last_report
                delta = self.stats["sent"] - self.prev_sent
                self.prev_sent = self.stats["sent"]
                self.stats["pps"] = delta / dt if dt else 0.0
                self.publish_stats(self.stats.copy())
                self.last_report = now

    def teardown(self):
        try:
            self.can_sock.close()
        except OSError:
            pass


class UdpDumpWorker(BaseTestWorker):
    def setup(self):
        config = load_config()
        port_index = int(self.params.get("port_index", 0))
        ports = config.get("ports") or []
        listen_port = None
        if ports:
            port_cfg = ports[min(port_index, len(ports) - 1)]
            listen_port = resolve_port(port_cfg, "udp_listen_port")
        listen_port = int(self.params.get("udp_port") or listen_port or 5555)
        self.udp_sock = make_udp_socket(bind_port=listen_port, timeout=0.1)
        self.stats = {"packets": 0, "last_id": 0, "pps": 0.0}
        self.last_report = time.time()
        self.prev_packets = 0

    def loop(self):
        while not self.stopped():
            try:
                packet, addr = self.udp_sock.recvfrom(UDP_FRAME.size)
                if len(packet) != UDP_FRAME.size:
                    continue
                info, can_id, data = UDP_FRAME.unpack(packet)
                dlc = info & 0x0F
                self.stats["packets"] += 1
                self.stats["last_id"] = can_id
                self.log(
                    f"UDP {addr[0]}:{addr[1]} -> ID 0x{can_id:X} DLC {dlc} 数据 {data[:dlc].hex(' ')}"
                )
            except socket.timeout:
                pass
            except OSError as exc:
                self.log(f"接收失败: {exc}")
                time.sleep(0.1)

            now = time.time()
            if now - self.last_report >= 1.0:
                dt = now - self.last_report
                delta = self.stats["packets"] - self.prev_packets
                self.prev_packets = self.stats["packets"]
                self.stats["pps"] = delta / dt if dt else 0.0
                self.publish_stats(self.stats.copy())
                self.last_report = now

    def teardown(self):
        try:
            self.udp_sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GUI 组件
# ---------------------------------------------------------------------------


@dataclass
class ParamSpec:
    name: str
    label: str
    default: str
    cast: Callable[[str], object] = str
    help: str = ""


@dataclass
class MetricSpec:
    key: str
    label: str
    fmt: str = "{:.0f}"


@dataclass
class TestDefinition:
    key: str
    title: str
    description: str
    worker_cls: type
    params: List[ParamSpec] = field(default_factory=list)
    metrics: List[MetricSpec] = field(default_factory=list)
    chart_metric: Optional[str] = None
    requires_root: bool = True


TEST_DEFINITIONS: List[TestDefinition] = [
    TestDefinition(
        key="udp_rx",
        title="UDP→CAN 压测",
        description="模拟远程 UDP 高频输入，验证桥接能否按端口将报文落到指定 CAN 通道。",
        worker_cls=UdpToCanWorker,
        params=[
            ParamSpec("port_index", "port 索引", "0", int),
            ParamSpec("channel_index", "channel 索引", "0", int),
            ParamSpec("iface", "CAN 接口(留空=配置)", ""),
            ParamSpec("pps", "UDP 目标 PPS", "5000", float),
        ],
        metrics=[
            MetricSpec("udp_sent", "UDP 已发"),
            MetricSpec("can_rcvd", "CAN 已收"),
            MetricSpec("pps", "CAN PPS", "{:.1f}"),
            MetricSpec("loss", "丢包 %", "{:.2f}"),
            MetricSpec("errors", "错误"),
        ],
        chart_metric="pps",
    ),
    TestDefinition(
        key="can_tx",
        title="CAN→UDP 压测",
        description="从指定 CAN 接口高速发包，监控桥接回传到 UDP 的吞吐与丢包率。",
        worker_cls=CanToUdpWorker,
        params=[
            ParamSpec("port_index", "port 索引", "0", int),
            ParamSpec("channel_index", "channel 索引", "0", int),
            ParamSpec("iface", "CAN 接口(留空=配置)", ""),
            ParamSpec("pps", "CAN 目标 PPS", "5000", float),
        ],
        metrics=[
            MetricSpec("can_sent", "CAN 已发"),
            MetricSpec("udp_rcvd", "UDP 已收"),
            MetricSpec("pps", "UDP PPS", "{:.1f}"),
            MetricSpec("loss", "丢包 %", "{:.2f}"),
            MetricSpec("errors", "错误"),
        ],
        chart_metric="pps",
    ),
    TestDefinition(
        key="ping",
        title="单通道乒乓",
        description="完整链路：CAN -> Bridge -> UDP -> Bridge -> CAN，监控闭环成功率与时延。",
        worker_cls=PingPongWorker,
        params=[
            ParamSpec("port_index", "port 索引", "0", int),
            ParamSpec("channel_index", "channel 索引", "0", int),
        ],
        metrics=[
            MetricSpec("success", "成功"),
            MetricSpec("total", "总计"),
            MetricSpec("udp_timeout", "UDP 超时"),
            MetricSpec("can_timeout", "CAN 超时"),
            MetricSpec("data_err", "数据错误"),
            MetricSpec("pps", "闭环 PPS", "{:.2f}"),
        ],
        chart_metric="pps",
    ),
    TestDefinition(
        key="multi_ping",
        title="多通道乒乓",
        description="遍历 config.json 所有通道并发测试，实时观察整体成功率。",
        worker_cls=MultiPingPongWorker,
        params=[],
        metrics=[
            MetricSpec("success", "成功"),
            MetricSpec("total", "总计"),
            MetricSpec("udp_timeout", "UDP 超时"),
            MetricSpec("can_timeout", "CAN 超时"),
            MetricSpec("data_err", "数据错"),
            MetricSpec("pps", "总 PPS", "{:.2f}"),
        ],
        chart_metric="pps",
    ),
    TestDefinition(
        key="can_flood",
        title="CAN Flood",
        description="向指定 CAN 接口全速灌包，可用于物理链路压力测试。",
        worker_cls=CanFloodWorker,
        params=[
            ParamSpec("iface", "CAN 接口", "vcan0"),
        ],
        metrics=[
            MetricSpec("sent", "已发送"),
            MetricSpec("pps", "PPS", "{:.1f}"),
        ],
        chart_metric="pps",
    ),
    TestDefinition(
        key="random_can",
        title="随机 CAN 发送",
        description="以固定间隔随机发送 CAN 帧，便于功能验证或模拟普通负载。",
        worker_cls=RandomCanWorker,
        params=[
            ParamSpec("iface", "CAN 接口", "vcan0"),
            ParamSpec("interval", "间隔(秒)", "1.0", float),
        ],
        metrics=[
            MetricSpec("sent", "已发送"),
            MetricSpec("pps", "PPS", "{:.2f}"),
        ],
        chart_metric="pps",
    ),
    TestDefinition(
        key="udp_dump",
        title="UDP 帧抓取",
        description="监听 UDP 端口并解析 13 字节帧，辅助排查桥接输出。",
        worker_cls=UdpDumpWorker,
        requires_root=False,
        params=[
            ParamSpec("port_index", "port 索引", "0", int),
            ParamSpec("udp_port", "自定义端口(可空)", ""),
        ],
        metrics=[
            MetricSpec("packets", "抓取数量"),
            MetricSpec("last_id", "最后 ID", "{:.0f}"),
            MetricSpec("pps", "PPS", "{:.1f}"),
        ],
        chart_metric="pps",
    ),
]


class MiniChart(tk.Canvas):
    def __init__(self, master, max_points: int = 120, **kwargs):
        super().__init__(master, width=360, height=140, bg="#111", highlightthickness=0, **kwargs)
        self.history = deque(maxlen=max_points)

    def push(self, value: float):
        self.history.append(value)
        self.redraw()

    def redraw(self):
        self.delete("all")
        if not self.history:
            self.create_text(180, 70, text="无数据", fill="#666")
            return
        max_val = max(self.history) or 1.0
        min_val = min(self.history)
        span = max(max_val - min_val, 1e-6)
        width = int(self["width"])
        height = int(self["height"])
        points = []
        for idx, value in enumerate(self.history):
            x = 10 + idx * (width - 20) / max(len(self.history) - 1, 1)
            norm = (value - min_val) / span
            y = height - 10 - norm * (height - 20)
            points.append((x, y))
        self.create_line(points, fill="#4CAF50", width=2, smooth=True)
        self.create_text(
            12,
            12,
            anchor="nw",
            text=f"最近值: {self.history[-1]:.2f}",
            fill="#EEE",
            font=("Helvetica", 9),
        )


class TestPanel(ttk.Frame):
    def __init__(self, master, definition: TestDefinition):
        super().__init__(master)
        self.definition = definition
        self.worker: Optional[BaseTestWorker] = None
        self.queue: queue.Queue = queue.Queue()

        self.status_var = tk.StringVar(value="未运行")
        self.metric_vars = {spec.key: tk.StringVar(value="--") for spec in definition.metrics}

        self._build_ui()
        self.after(100, self._poll_queue)

    def _build_ui(self):
        ttk.Label(self, text=self.definition.description, wraplength=520, justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 4)
        )
        ttk.Label(self, text=f"状态: ", width=10).grid(row=1, column=0, sticky="e", padx=(8, 2))
        ttk.Label(self, textvariable=self.status_var, width=16).grid(row=1, column=1, sticky="w")

        form_row = 2
        self.inputs: Dict[str, tk.Entry] = {}
        for spec in self.definition.params:
            ttk.Label(self, text=spec.label + ":").grid(row=form_row, column=0, sticky="e", padx=(8, 2), pady=2)
            entry = ttk.Entry(self)
            entry.insert(0, spec.default)
            entry.grid(row=form_row, column=1, sticky="we", padx=(0, 8), pady=2)
            if spec.help:
                ttk.Label(self, text=spec.help, foreground="#666").grid(row=form_row, column=2, sticky="w")
            self.inputs[spec.name] = entry
            form_row += 1
        self.columnconfigure(1, weight=1)

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=form_row, column=0, columnspan=3, sticky="we", padx=8, pady=6)
        ttk.Button(btn_frame, text="启动", command=self.start_worker).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="停止", command=self.stop_worker).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="清空日志", command=lambda: self.log_box.delete("1.0", tk.END)).pack(side="left", padx=4)
        form_row += 1

        metric_frame = ttk.LabelFrame(self, text="实时指标")
        metric_frame.grid(row=form_row, column=0, columnspan=3, sticky="we", padx=8, pady=6)
        for idx, spec in enumerate(self.definition.metrics):
            ttk.Label(metric_frame, text=spec.label + ":").grid(row=idx, column=0, sticky="e", padx=(8, 2), pady=2)
            ttk.Label(metric_frame, textvariable=self.metric_vars[spec.key]).grid(
                row=idx, column=1, sticky="w", padx=(0, 8), pady=2
            )

        form_row += 1
        self.chart = MiniChart(self)
        self.chart.grid(row=form_row, column=0, columnspan=3, padx=8, pady=4)
        form_row += 1

        self.log_box = tk.Text(self, height=10, wrap="word")
        self.log_box.grid(row=form_row, column=0, columnspan=3, sticky="nsew", padx=8, pady=(4, 8))
        self.rowconfigure(form_row, weight=1)

    def _collect_params(self) -> Optional[dict]:
        params = {}
        for spec in self.definition.params:
            text = self.inputs[spec.name].get()
            if text.strip() == "":
                params[spec.name] = ""
                continue
            try:
                params[spec.name] = spec.cast(text)
            except Exception:
                messagebox.showerror("参数错误", f"{spec.label} 无法解析: {text}")
                return None
        return params

    def start_worker(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "当前测试仍在运行，请先停止。")
            return
        params = self._collect_params()
        if params is None:
            return
        if self.definition.requires_root and os.getuid() != 0:
            messagebox.showwarning("权限不足", "此测试需要 root 权限，请使用 sudo 运行 GUI。")
            return

        def stats_cb(stats):
            self.queue.put(("stats", stats))

        def log_cb(message):
            self.queue.put(("log", message))

        def status_cb(status):
            self.queue.put(("status", status))

        self.worker = self.definition.worker_cls(
            params=params,
            stats_cb=stats_cb,
            log_cb=log_cb,
            status_cb=status_cb,
            name=self.definition.key,
        )
        self.worker.start()

    def stop_worker(self):
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=1.0)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "stats":
                    self._update_metrics(payload)
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _update_metrics(self, stats: dict):
        for spec in self.definition.metrics:
            if spec.key in stats:
                value = stats[spec.key]
                if isinstance(value, float):
                    text = spec.fmt.format(value)
                else:
                    text = str(value)
                self.metric_vars[spec.key].set(text)
        if self.definition.chart_metric and self.definition.chart_metric in stats:
            try:
                self.chart.push(float(stats[self.definition.chart_metric]))
            except (ValueError, TypeError):
                pass

    def _append_log(self, message: str):
        self.log_box.insert(tk.END, message + "\n")
        self.log_box.see(tk.END)


class BridgeTestGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UDP SocketCAN Bridge 测试中心")
        self.geometry("960x720")
        self._build_ui()

    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        for definition in TEST_DEFINITIONS:
            panel = TestPanel(notebook, definition)
            notebook.add(panel, text=definition.title)
        ttk.Label(
            self,
            text="提示：先启动 udp_socketcan_bridge，并确保 config.json 与实际 CAN 接口/端口一致。",
            foreground="#666",
        ).pack(side="bottom", pady=4)


def main():
    app = BridgeTestGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
