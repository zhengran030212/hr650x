#!/usr/bin/env python3
"""HR650X 风扇调节的本机界面。只监听 127.0.0.1。"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shutil
import signal
import threading
import time
try:
    import webbrowser
except ModuleNotFoundError:  # OpenWrt python3-light 没有 webbrowser
    webbrowser = None
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ipmi_manager import (
    Config,
    ConfigError,
    Controller,
    FanPolicy,
    IpmiClient,
    IpmiError,
    IpmiTarget,
    SensorParseError,
    _speed,
    load_config,
    parse_config,
    cpu_temperature_sample,
    chassis_power_on,
    default_config_path,
)

WEB_ROOT = Path(__file__).resolve().with_name("web")


class ConsoleError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class DemoClient:
    """不访问 BMC 的温度和转速，用来看曲线和界面。"""

    def __init__(self) -> None:
        self.duty: int | None = None
        self.started = time.monotonic()

    def temperature(self) -> float:
        return 54 + 16 * math.sin((time.monotonic() - self.started) / 8)

    def read_sensors(self) -> str:
        temp = self.temperature()
        return (
            f"CPU1 Temp | {temp:.3f} | degrees C | ok | na | na | na | na | 95.000 | na\n"
            f"CPU2 Temp | {temp - 2.4:.3f} | degrees C | ok | na | na | na | na | 95.000 | na\n"
            "CPU1 VRD Temp | 71.000 | degrees C | ok | na | na | na | na | na | na\n"
        )

    def set_speed(self, speed: int) -> None:
        self.duty = int(speed)


class RingHandler(logging.Handler):
    def __init__(self, console: "Console") -> None:
        super().__init__()
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        self.console.add_log(record.levelname.lower(), record.getMessage())


def _plain(value: float) -> int | float:
    if float(value).is_integer():
        return int(value)
    return value


def yaml_scalar(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def dump_config(config: Config) -> str:
    """写回曲线和 BMC 登录信息。"""
    lines = [
        "# 由 HR650X 界面写回。",
        "ipmi:",
        f"  interface: {config.target.interface}",
        f"  host: {yaml_scalar(config.target.host)}",
        f"  username: {yaml_scalar(config.target.username)}",
    ]
    if config.target.password:
        lines.append(f"  password: {yaml_scalar(config.target.password)}")
    exit_speed = "null" if config.exit_speed is None else str(config.exit_speed)
    lines.extend([
        f"interval: {_plain(config.interval)}",
        f"hysteresis: {_plain(config.hysteresis)}",
        f"fallback_speed: {config.fallback_speed}",
        f"min_speed_on: {config.min_speed_on}",
        f"exit_speed: {exit_speed}",
        f"na_grace: {config.na_grace}",
        f"command_timeout: {_plain(config.command_timeout)}",
        f"sensor_pattern: {yaml_scalar(config.sensor_pattern)}",
        f"use_dts: {'true' if config.use_dts else 'false'}",
        "fan_speeds:",
    ])
    for band in config.bands:
        lines.append(
            f"  - temp_range: [{_plain(band.low)}, {_plain(band.high)}]"
        )
        lines.append(f"    speed: {band.speed}")
    return "\n".join(lines) + "\n"


class Console:
    def __init__(self, path: Path, demo: bool = False, environ: dict[str, str] | None = None) -> None:
        self.path = path
        self.demo = demo
        env = environ if environ is not None else None
        self.config = load_config(path, env, require_password=False)
        self.secret = self.config.target.password
        self.pattern = re.compile(self.config.sensor_pattern, re.IGNORECASE)
        self.log = logging.getLogger(f"hr650x.ui.{id(self)}")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        self.log.addHandler(RingHandler(self))
        self.client: Any = DemoClient() if demo else self._real_client()
        self.controller = self._make_controller()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.running = False
        self.sample = None
        self.decision = None
        self.duty: int | None = None
        self.error: str | None = None
        self.logs: deque[dict[str, str]] = deque(maxlen=40)

    def _real_client(self) -> IpmiClient:
        target = self.config.target
        if target.interface != "open" and self.secret and target.password != self.secret:
            target = IpmiTarget(target.interface, target.host, target.username, self.secret)
        return IpmiClient(target, self.config.command_timeout)

    def _make_controller(self) -> Controller:
        config = self.config
        return Controller(
            policy=FanPolicy(
                config.bands,
                config.hysteresis,
                config.fallback_speed,
                config.min_speed_on,
                config.na_grace,
                config.boot_speed,
            ),
            client=self.client,
            sensor_pattern=config.sensor_pattern,
            dry_run=False,
            exit_speed=config.exit_speed,
            log=self.log,
            use_dts=config.use_dts,
        )

    def add_log(self, level: str, message: str) -> None:
        entry = {"time": time.strftime("%H:%M:%S"), "level": level, "message": message}
        with self.lock:
            self.logs.append(entry)

    def state(self) -> dict[str, Any]:
        with self.lock:
            config = self.config
            sample = self.sample
            decision = self.decision
            return {
                "demo": self.demo,
                "running": self.running,
                "has_password": bool(self.secret),
                "ipmitool": shutil.which("ipmitool") is not None,
                "duty": self.duty,
                "error": self.error,
                "sample": None
                if sample is None
                else {
                    "temp": sample.temp,
                    "matched": sample.matched,
                    "valid": sample.valid,
                    "readings": [
                        {"name": name, "temp": temp} for name, temp in sample.readings
                    ],
                },
                "decision": None
                if decision is None
                else {"speed": decision.speed, "reason": decision.reason},
                "logs": list(self.logs),
                "config": {
                    "interface": config.target.interface,
                    "host": config.target.host or "",
                    "username": config.target.username or "",
                    "interval": _plain(config.interval),
                    "hysteresis": _plain(config.hysteresis),
                    "fallback_speed": config.fallback_speed,
                    "min_speed_on": config.min_speed_on,
                    "exit_speed": config.exit_speed,
                    "use_dts": config.use_dts,
                    "bands": [
                        {"low": _plain(band.low), "high": _plain(band.high), "speed": band.speed}
                        for band in config.bands
                    ],
                },
            }

    def _ready(self) -> None:
        if self.demo:
            return
        if self.config.target.interface != "open" and not self.secret:
            raise ConsoleError("先填写 BMC 密码")
        if shutil.which("ipmitool") is None:
            raise ConsoleError("找不到 ipmitool")

    def apply_settings(self, payload: dict[str, Any], save: bool) -> None:
        import re

        with self.lock:
            previous = self.config
            typed = payload.get("password")
            if isinstance(typed, str) and typed.strip():
                self.secret = typed.strip()
            data = self._payload(payload)
            environ = {"IPMI_PASSWORD": self.secret} if self.secret else {}
            config = parse_config(data, environ, require_password=False)
            self.config = config
            self.pattern = re.compile(config.sensor_pattern, re.IGNORECASE)
            self.controller.policy = FanPolicy(
                config.bands,
                config.hysteresis,
                config.fallback_speed,
                config.min_speed_on,
                config.na_grace,
                config.boot_speed,
            )
            self.controller.pattern = self.pattern
            self.controller.exit_speed = config.exit_speed
            self.controller.use_dts = config.use_dts
            if not self.demo:
                self.client = self._real_client()
                self.controller.client = self.client
            text = dump_config(config)
            changed = config != previous
        if save:
            self.path.write_text(text, encoding="utf-8")
            self.add_log("info", "已保存曲线")
        elif changed:
            self.add_log("info", "已更新设置")

    def _payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.config
        interface = payload.get("interface", config.target.interface)
        host = payload.get("host", config.target.host)
        username = payload.get("username", config.target.username)
        if "bands" in payload:
            bands = payload["bands"]
        else:
            bands = [
                {"low": band.low, "high": band.high, "speed": band.speed} for band in config.bands
            ]
        exit_speed = payload.get("exit_speed", config.exit_speed)
        if exit_speed == "":
            exit_speed = None
        return {
            "ipmi": {
                "interface": interface,
                "host": host or None,
                "username": username or None,
            },
            "interval": payload.get("interval", config.interval),
            "hysteresis": payload.get("hysteresis", config.hysteresis),
            "fallback_speed": payload.get("fallback_speed", config.fallback_speed),
            "min_speed_on": payload.get("min_speed_on", config.min_speed_on),
            "boot_speed": config.boot_speed,
            "exit_speed": exit_speed,
            "na_grace": config.na_grace,
            "command_timeout": config.command_timeout,
            "sensor_pattern": config.sensor_pattern,
            "use_dts": payload.get("use_dts", config.use_dts),
            "fan_speeds": [
                {"temp_range": [band["low"], band["high"]], "speed": band["speed"]}
                for band in bands
            ],
        }

    def set_demo(self, enabled: bool) -> None:
        with self.lock:
            running = self.running
        if running:
            self.stop_auto()
        with self.lock:
            self.demo = bool(enabled)
            self.client = DemoClient() if self.demo else self._real_client()
            self.controller.client = self.client
            self.error = None
        self.add_log("info", "模拟，不写 BMC" if self.demo else "改为连接 BMC")

    def probe(self) -> None:
        with self.lock:
            self._ready()
            client = self.client
            pattern = self.pattern
            use_dts = self.config.use_dts
        try:
            text = client.read_sensors()
            sample = cpu_temperature_sample(text, pattern, prefer_dts=use_dts)
            power_on = chassis_power_on(client)
        except (IpmiError, SensorParseError) as exc:
            with self.lock:
                self.sample = None
                self.error = str(exc)
            self.add_log("error", str(exc))
            raise ConsoleError(str(exc)) from exc
        with self.lock:
            self.sample = sample
            self.error = None
        if sample.temp is None and power_on is False:
            self.add_log("info", "主机关机")
        elif sample.temp is None:
            self.add_log("info", "开机中，CPU 温度还读不到")
        elif sample.readings:
            detail = "，".join(f"{name} {temp:.1f}°C" for name, temp in sample.readings)
            self.add_log("info", detail)
        else:
            self.add_log("info", f"CPU 最高 {sample.temp:.1f}°C")

    def manual(self, speed: object) -> None:
        duty = _speed(speed, "转速")
        with self.lock:
            if self.running:
                raise ConsoleError("自动调节进行中，先停止", status=409)
            self._ready()
            client = self.client
            controller = self.controller
        try:
            client.set_speed(duty)
        except IpmiError as exc:
            self.add_log("error", str(exc))
            raise ConsoleError(str(exc)) from exc
        with self.lock:
            self.duty = duty
            controller.last_speed = duty
            self.error = None
        self.add_log("info", f"写入风扇 {duty}%")

    def start_auto(self) -> None:
        with self.lock:
            if self.running:
                return
            self._ready()
            self.stop_event.clear()
            self.running = True
            self.thread = threading.Thread(target=self._loop, name="hr650x-fan", daemon=True)
            self.thread.start()
        self.add_log("info", "开始自动")

    def stop_auto(self) -> None:
        with self.lock:
            thread = self.thread
            running = self.running
            controller = self.controller
        if not running or thread is None:
            return
        self.stop_event.set()
        thread.join(timeout=max(5, self.config.command_timeout + 2))
        try:
            controller.shutdown()
        except IpmiError as exc:
            self.add_log("error", f"退出时写入风扇失败：{exc}")
        with self.lock:
            self.running = False
            self.duty = controller.last_speed
        self.add_log("info", "已停止")

    def _loop(self) -> None:
        controller = self.controller
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    controller = self.controller
                    interval = self.config.interval
                try:
                    decision = controller.step()
                except IpmiError as exc:
                    with self.lock:
                        self.error = str(exc)
                    self.add_log("error", f"写入风扇失败：{exc}")
                else:
                    with self.lock:
                        self.sample = controller.last_sample
                        self.decision = decision
                        self.duty = controller.last_speed
                        self.error = controller.last_error
                if self.stop_event.wait(interval):
                    break
        except Exception as exc:
            self.add_log("error", f"自动调节中断：{exc}")
            with self.lock:
                self.running = False
                controller = self.controller
            try:
                controller.shutdown()
            except IpmiError as err:
                self.add_log("error", f"退出时写入风扇失败：{err}")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(console: Console):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/state":
                self._json(200, console.state())
                return
            file_name = {"": "index.html", "/": "index.html", "/app.css": "app.css", "/app.js": "app.js"}.get(path)
            if file_name is None:
                self._json(404, {"error": "没有这个页面"})
                return
            self._file(WEB_ROOT / file_name)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                payload = self._body()
                if path == "/api/settings":
                    console.apply_settings(payload, bool(payload.get("save")))
                elif path == "/api/probe":
                    console.probe()
                elif path == "/api/manual":
                    console.manual(payload.get("speed"))
                elif path == "/api/auto/start":
                    console.start_auto()
                elif path == "/api/auto/stop":
                    console.stop_auto()
                elif path == "/api/demo":
                    console.set_demo(bool(payload.get("enabled")))
                else:
                    self._json(404, {"error": "没有这个操作"})
                    return
            except ConsoleError as exc:
                self._json(exc.status, {"error": str(exc)})
                return
            except ConfigError as exc:
                self._json(400, {"error": str(exc)})
                return
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                self._json(400, {"error": f"请求无法解析：{exc}"})
                return
            self._json(200, console.state())

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                raise ConsoleError("请求太大")
            if length == 0:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ConsoleError("请求必须是对象")
            return data

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _file(self, path: Path) -> None:
            if not path.is_file():
                self._json(404, {"error": "界面文件缺失"})
                return
            kind = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
            }.get(path.suffix, "application/octet-stream")
            raw = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def serve(config_path: Path, host: str, port: int, demo: bool, open_browser: bool) -> None:
    console = Console(config_path, demo=demo)
    httpd = AppServer((host, port), make_handler(console))
    url = f"http://{host}:{httpd.server_address[1]}/"

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(url, flush=True)
    if open_browser and webbrowser is not None:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    finally:
        console.stop_auto()
        httpd.server_close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="打开 HR650X 风扇调节界面")
    parser.add_argument("-c", "--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="用模拟温度，不连接 BMC")
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config or default_config_path()
    if not config_path.is_file():
        print(f"找不到配置：{config_path}", flush=True)
        return 2
    serve(config_path, args.host, args.port, args.demo, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
