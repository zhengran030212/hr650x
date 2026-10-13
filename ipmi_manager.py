#!/usr/bin/env python3
"""按 CPU 温度调节联想 HR650X / HR630X 风扇。

基于 Vision0220/HR650X-IPMI-Auto-Fan（Yuzhuo Wu，2023）的 IPMI OEM 命令::

    raw 0x2e 0x30 0x00 0x00 <duty>

duty 是 0–100 的十进制百分比。ipmitool 用 strtoul(base=0) 解析 raw 参数，
带前导 0 的数字会按八进制处理，所以 10% 必须写成 ``10``，不能写成 ``010``。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().with_name("HR650X.yaml")
LOCAL_CONFIG = DEFAULT_CONFIG.with_name("HR650X.local.yaml")
DEFAULT_SENSOR_PATTERN = r"^CPU[0-9]+[_ ]Temp$"
LOG = logging.getLogger("hr650x")


class ConfigError(Exception):
    pass


class IpmiError(Exception):
    pass


class SensorParseError(Exception):
    pass


class AlreadyRunning(Exception):
    pass


@dataclass(frozen=True)
class Band:
    low: float
    high: float
    speed: int


@dataclass(frozen=True)
class Sample:
    temp: float | None
    matched: int
    valid: int
    readings: tuple[tuple[str, float], ...] = ()


_CPU_DTS = re.compile(r"^CPU\d+[_ ]DTS$", re.IGNORECASE)


@dataclass(frozen=True)
class Decision:
    speed: int
    reason: str


@dataclass(frozen=True)
class IpmiTarget:
    interface: str
    host: str | None = None
    username: str | None = None
    password: str | None = None

    def __repr__(self) -> str:
        secret = "***" if self.password else None
        return (
            f"IpmiTarget(interface={self.interface!r}, host={self.host!r}, "
            f"username={self.username!r}, password={secret!r})"
        )


@dataclass(frozen=True)
class Config:
    target: IpmiTarget
    bands: tuple[Band, ...]
    interval: float
    hysteresis: float
    fallback_speed: int
    min_speed_on: int
    boot_speed: int
    exit_speed: int | None
    sensor_pattern: str
    command_timeout: float
    na_grace: int


class SensorClient(Protocol):
    def read_sensors(self) -> str: ...

    def set_speed(self, speed: int) -> None: ...


def parse_chassis_power(text: str) -> bool | None:
    lowered = text.lower()
    if "is off" in lowered:
        return False
    if "is on" in lowered:
        return True
    return None


def chassis_power_on(client: SensorClient) -> bool | None:
    method = getattr(client, "chassis_power_on", None)
    if method is None:
        return None
    try:
        return method()
    except IpmiError:
        return None


def format_duty(speed: int) -> str:
    """十进制、无前导零。``010`` 会被 ipmitool 当成八进制。"""
    if isinstance(speed, bool) or not isinstance(speed, int):
        raise ValueError(f"转速必须是整数，收到 {speed!r}")
    if not 0 <= speed <= 100:
        raise ValueError(f"转速超出 0–100：{speed}")
    return str(speed)


def parse_sensor_output(text: str, pattern: re.Pattern[str]) -> Sample:
    """解析 ``ipmitool sensor`` 表格。

    第 2 列才是当前读数。后面的列是阈值，不能拿来当温度。
    """
    matched: list[float | None] = []
    names: list[str] = []
    for raw in text.splitlines():
        if "|" not in raw:
            continue
        columns = [part.strip() for part in raw.split("|")]
        if len(columns) < 2 or not columns[0]:
            continue
        name, reading = columns[0], columns[1]
        names.append(name)
        if pattern.search(name) is None:
            continue
        token = reading.lower()
        if token in {"", "na", "n/a", "disabled"}:
            matched.append(None)
            continue
        try:
            matched.append(float(reading))
        except ValueError as exc:
            raise SensorParseError(f"传感器 {name} 的读数无法解析：{reading}") from exc

    if not matched:
        interesting = [
            name for name in names if "temp" in name.lower() or "cpu" in name.lower()
        ]
        shown = "、".join(interesting[:12]) or "无"
        raise SensorParseError(
            f"没有传感器匹配 {pattern.pattern}。相近名称：{shown}"
        )

    valid = [value for value in matched if value is not None]
    return Sample(
        temp=max(valid) if valid else None,
        matched=len(matched),
        valid=len(valid),
    )


def _temperature_value(reading: str) -> float | None:
    token = reading.lower()
    if token in {"", "na", "n/a", "disabled"}:
        return None
    value = float(reading)
    # 空槽位的 CPU 会报 0，不能当成温度。
    if value <= 0:
        return None
    return value


def cpu_temperature_sample(text: str, fallback: re.Pattern[str]) -> Sample:
    """优先用 CPU DTS。没有 DTS 时再用配置里的传感器表达式。"""
    found: list[tuple[str, float | None]] = []
    for raw in text.splitlines():
        if "|" not in raw:
            continue
        columns = [part.strip() for part in raw.split("|")]
        if len(columns) < 2 or not columns[0] or _CPU_DTS.search(columns[0]) is None:
            continue
        try:
            found.append((columns[0], _temperature_value(columns[1])))
        except ValueError as exc:
            raise SensorParseError(f"传感器 {columns[0]} 的读数无法解析：{columns[1]}") from exc
    present = tuple((name, value) for name, value in found if value is not None)
    if found:
        return Sample(
            temp=max(value for _, value in present) if present else None,
            matched=len(found),
            valid=len(present),
            readings=present,
        )
    return parse_sensor_output(text, fallback)


class FanPolicy:
    """把温度映射成转速。

    ``active == len(bands)`` 表示温度已经高于曲线，使用 fallback_speed。
    升温立刻换档。降温要落到当前档下沿再减去回差，才允许降档。
    """

    def __init__(
        self,
        bands: Sequence[Band],
        hysteresis: float,
        fallback_speed: int,
        min_speed_on: int,
        na_grace: int,
        boot_speed: int = 20,
    ) -> None:
        if not bands:
            raise ValueError("至少要有一档风扇曲线")
        self.bands = list(bands)
        self.hysteresis = float(hysteresis)
        self.fallback_speed = fallback_speed
        self.min_speed_on = min_speed_on
        self.na_grace = na_grace
        self.boot_speed = boot_speed
        self.active: int | None = None
        self.last_online_speed: int | None = None

    def decide(self, sample: Sample | None, *, power_on: bool | None = None) -> Decision:
        unavailable = sample is None or sample.valid == 0
        if unavailable and power_on is False:
            self.active = None
            self.last_online_speed = None
            index = self._index_for(0.0, online=False)
            return Decision(self._speed_of(index, online=False), "主机关机")
        if unavailable:
            if self.last_online_speed is None:
                return Decision(self.boot_speed, "开机中，CPU 温度还读不到")
            return Decision(self.last_online_speed, "CPU 温度暂时读不到，保持当前转速")

        assert sample is not None and sample.temp is not None
        index = self._index_for(sample.temp, online=True)
        speed = self._speed_of(index, online=True)
        self.last_online_speed = speed
        return Decision(
            speed,
            f"CPU 最高 {sample.temp:.1f}°C（{sample.valid}/{sample.matched}）",
        )

    def _natural(self, temp: float) -> int:
        if temp >= self.bands[-1].high:
            return len(self.bands)
        if temp < self.bands[0].low:
            return 0
        for index, band in enumerate(self.bands):
            if band.low <= temp < band.high:
                return index
        return len(self.bands)

    def _release_threshold(self, active: int) -> float:
        edge = self.bands[-1].high if active >= len(self.bands) else self.bands[active].low
        return edge - self.hysteresis

    def _index_for(self, temp: float, *, online: bool) -> int:
        natural = self._natural(temp)
        active = self.active if online else None
        if active is None or natural >= active:
            chosen = natural
        else:
            chosen = active if temp >= self._release_threshold(active) else natural
        self.active = chosen
        return chosen

    def _speed_of(self, index: int, *, online: bool) -> int:
        if index >= len(self.bands):
            return self.fallback_speed
        speed = self.bands[index].speed
        if online:
            return max(speed, self.min_speed_on)
        return speed


def build_ipmi_args(target: IpmiTarget, command: Sequence[str]) -> list[str]:
    args = ["ipmitool", "-I", target.interface]
    if target.interface != "open":
        if not target.host or not target.username:
            raise IpmiError("远程 IPMI 缺少地址或用户名")
        args.extend(["-H", target.host, "-U", target.username, "-E"])
    args.extend(command)
    return args


def ipmi_env(target: IpmiTarget, base: Mapping[str, str]) -> dict[str, str]:
    env = dict(base)
    if target.password:
        env["IPMI_PASSWORD"] = target.password
    return env


def default_runner(args: Sequence[str], env: Mapping[str, str], timeout: float) -> str:
    try:
        proc = subprocess.run(
            list(args),
            env=dict(env),
            timeout=timeout,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise IpmiError(f"ipmitool 超时（{timeout:.0f}s）") from exc
    except FileNotFoundError as exc:
        raise IpmiError("找不到 ipmitool") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        raise IpmiError(detail[:500] or f"ipmitool 退出码 {proc.returncode}")
    return proc.stdout


class IpmiClient:
    def __init__(
        self,
        target: IpmiTarget,
        timeout: float,
        runner=default_runner,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.target = target
        self.timeout = timeout
        self.runner = runner
        self.environ = os.environ if environ is None else environ

    def read_sensors(self) -> str:
        return self._run(("sensor",))

    def set_speed(self, speed: int) -> None:
        duty = format_duty(speed)
        self._run(("raw", "0x2e", "0x30", "0x00", "0x00", duty))

    def chassis_power_on(self) -> bool | None:
        return parse_chassis_power(self._run(("chassis", "power", "status")))

    def _run(self, command: Sequence[str]) -> str:
        args = build_ipmi_args(self.target, command)
        return self.runner(args, ipmi_env(self.target, self.environ), self.timeout)


class Controller:
    def __init__(
        self,
        *,
        policy: FanPolicy,
        client: SensorClient,
        sensor_pattern: str,
        dry_run: bool,
        exit_speed: int | None,
        log: logging.Logger,
    ) -> None:
        self.policy = policy
        self.client = client
        self.pattern = re.compile(sensor_pattern, re.IGNORECASE)
        self.dry_run = dry_run
        self.exit_speed = exit_speed
        self.log = log
        self.last_speed: int | None = None
        self.last_sample: Sample | None = None
        self.last_error: str | None = None

    def step(self) -> Decision:
        client = self.client
        policy = self.policy
        pattern = self.pattern
        dry_run = self.dry_run
        self.last_error = None
        power_on = chassis_power_on(client)
        try:
            self.last_sample = cpu_temperature_sample(client.read_sensors(), pattern)
        except (IpmiError, SensorParseError) as exc:
            self.last_sample = None
            self.last_error = str(exc)
            self.log.error("读取传感器失败：%s", exc)
            sample = None
        else:
            sample = self.last_sample
        decision = policy.decide(sample, power_on=power_on)
        self.apply(decision, client=client, dry_run=dry_run)
        return decision

    def apply(
        self,
        decision: Decision,
        client: SensorClient | None = None,
        dry_run: bool | None = None,
    ) -> None:
        client = self.client if client is None else client
        dry_run = self.dry_run if dry_run is None else dry_run
        if decision.speed == self.last_speed:
            self.log.debug("维持风扇 %s%%（%s）", decision.speed, decision.reason)
            return
        if dry_run:
            self.log.info("未写入风扇 %s%%（%s）", decision.speed, decision.reason)
            self.last_speed = decision.speed
            return
        client.set_speed(decision.speed)
        self.last_speed = decision.speed
        self.log.info("写入风扇 %s%%（%s）", decision.speed, decision.reason)

    def shutdown(self) -> None:
        if self.dry_run or self.exit_speed is None or self.last_speed == self.exit_speed:
            return
        self.log.info("退出，风扇设为 %s%%", self.exit_speed)
        try:
            self.client.set_speed(self.exit_speed)
        except IpmiError as exc:
            self.log.error("退出时写入风扇失败：%s", exc)
            return
        self.last_speed = self.exit_speed


def _text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{name} 需要字符串")
    text = value.strip()
    return text or None


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} 需要数字")
    return float(value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} 需要整数")
    return value


def _speed(value: object, name: str = "speed") -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} 需要 0–100 的整数")
    if isinstance(value, float) and not value.is_integer():
        raise ConfigError(f"{name} 需要 0–100 的整数")
    speed = int(value)
    if not 0 <= speed <= 100:
        raise ConfigError(f"{name} 超出 0–100：{speed}")
    return speed


def _bands(items: object) -> tuple[Band, ...]:
    if not isinstance(items, list) or not items:
        raise ConfigError("fan_speeds 至少要有一档")
    bands: list[Band] = []
    for item in items:
        if not isinstance(item, dict):
            raise ConfigError("fan_speeds 的每一档都要是映射")
        span = item.get("temp_range")
        if not isinstance(span, list) or len(span) != 2:
            raise ConfigError("temp_range 需要 [最低, 最高]")
        try:
            low = float(span[0])
            high = float(span[1])
        except (TypeError, ValueError) as exc:
            raise ConfigError("temp_range 需要两个数") from exc
        if low >= high:
            raise ConfigError(f"温度区间无效：{low:g}..{high:g}")
        bands.append(Band(low, high, _speed(item.get("speed"))))
    bands.sort(key=lambda band: (band.low, band.high))
    for prev, nxt in zip(bands, bands[1:]):
        if nxt.low < prev.high:
            raise ConfigError(
                f"温度区间重叠：{prev.low:g}..{prev.high:g} 与 {nxt.low:g}..{nxt.high:g}"
            )
        if nxt.low > prev.high:
            raise ConfigError(f"温度区间有空档：{prev.high:g} 到 {nxt.low:g}")
    return tuple(bands)


def parse_config(
    data: object,
    environ: Mapping[str, str],
    require_password: bool = True,
) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是映射")
    ipmi = data.get("ipmi")
    if not isinstance(ipmi, dict):
        raise ConfigError("缺少 ipmi 配置")

    interface = ipmi.get("interface", "lanplus")
    if interface not in {"lanplus", "lan", "open"}:
        raise ConfigError("interface 只能是 lanplus、lan 或 open")
    host = _text(ipmi.get("host"), "ipmi.host")
    username = _text(ipmi.get("username"), "ipmi.username")
    password = _text(environ.get("IPMI_PASSWORD"), "IPMI_PASSWORD") or _text(
        ipmi.get("password"), "ipmi.password"
    )
    if interface == "open":
        password = None
    elif not host:
        raise ConfigError("lanplus/lan 需要 ipmi.host")
    elif not username:
        raise ConfigError("lanplus/lan 需要 ipmi.username")
    elif not password:
        if require_password:
            raise ConfigError("缺少密码：在配置里写 ipmi.password，或导出 IPMI_PASSWORD")
        password = None

    interval = _number(data.get("interval", 10), "interval")
    hysteresis = _number(data.get("hysteresis", 2), "hysteresis")
    timeout = _number(data.get("command_timeout", 20), "command_timeout")
    na_grace = _integer(data.get("na_grace", 2), "na_grace")
    fallback = _speed(data.get("fallback_speed", 100), "fallback_speed")
    min_speed_on = _speed(data.get("min_speed_on", 10), "min_speed_on")
    boot_speed = _speed(data.get("boot_speed", 20), "boot_speed")
    if interval <= 0:
        raise ConfigError("interval 必须大于 0")
    if hysteresis < 0:
        raise ConfigError("hysteresis 不能为负")
    if timeout <= 0:
        raise ConfigError("command_timeout 必须大于 0")
    if na_grace < 0:
        raise ConfigError("na_grace 不能为负")
    if min_speed_on > fallback:
        raise ConfigError("min_speed_on 不能高于 fallback_speed")
    if boot_speed > fallback:
        raise ConfigError("boot_speed 不能高于 fallback_speed")
    if "exit_speed" not in data:
        exit_speed: int | None = 100
    elif data["exit_speed"] is None:
        exit_speed = None
    else:
        exit_speed = _speed(data["exit_speed"], "exit_speed")

    pattern = data.get("sensor_pattern", DEFAULT_SENSOR_PATTERN)
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError("sensor_pattern 需要正则字符串")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"sensor_pattern 无效：{exc}") from exc

    return Config(
        target=IpmiTarget(interface, host, username, password),
        bands=_bands(data.get("fan_speeds")),
        interval=interval,
        hysteresis=hysteresis,
        fallback_speed=fallback,
        min_speed_on=min_speed_on,
        boot_speed=boot_speed,
        exit_speed=exit_speed,
        sensor_pattern=pattern,
        command_timeout=timeout,
        na_grace=na_grace,
    )


def default_config_path() -> Path:
    if LOCAL_CONFIG.is_file():
        return LOCAL_CONFIG
    return DEFAULT_CONFIG


def load_config(
    path: Path,
    environ: Mapping[str, str] | None = None,
    require_password: bool = True,
) -> Config:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置 {path}：{exc.strerror}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 无法解析：{exc}") from exc
    return parse_config(
        data,
        os.environ if environ is None else environ,
        require_password=require_password,
    )


@contextlib.contextmanager
def single_instance(config_path: Path) -> Iterator[None]:
    digest = hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"hr650x-fan-{digest}.lock"
    handle = path.open("a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise AlreadyRunning(f"已经有一个调节进程在使用 {config_path}") from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按 CPU 温度调节联想 HR650X 风扇")
    parser.add_argument("-c", "--config", type=Path)
    parser.add_argument("--interval", type=float, help="覆盖配置里的轮询秒数")
    parser.add_argument("--once", action="store_true", help="只调节一轮")
    parser.add_argument("--dry-run", action="store_true", help="读温度并打印目标转速，不写 BMC")
    parser.add_argument("--check", action="store_true", help="只检查配置，不连接 BMC")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(None if argv is None else list(argv))


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _request_stop(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.config is None:
        args.config = default_config_path()
    configure_logging(args.verbose)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.check:
        LOG.info("配置可用，共 %d 档", len(config.bands))
        return 0
    if shutil.which("ipmitool") is None:
        print("找不到 ipmitool", file=sys.stderr)
        return 127

    interval = config.interval
    if args.interval is not None:
        if args.interval <= 0:
            print("间隔必须大于 0", file=sys.stderr)
            return 2
        interval = args.interval

    where = "本机" if config.target.interface == "open" else config.target.host
    LOG.info(
        "目标 %s，间隔 %gs，回差 %g°C，在线最低 %s%%，失败回退 %s%%",
        where,
        interval,
        config.hysteresis,
        config.min_speed_on,
        config.fallback_speed,
    )
    policy = FanPolicy(
        config.bands,
        config.hysteresis,
        config.fallback_speed,
        config.min_speed_on,
        config.na_grace,
        config.boot_speed,
    )
    controller = Controller(
        policy=policy,
        client=IpmiClient(config.target, config.command_timeout),
        sensor_pattern=config.sensor_pattern,
        dry_run=args.dry_run,
        exit_speed=config.exit_speed,
        log=LOG,
    )
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    with single_instance(args.config):
        try:
            while True:
                try:
                    controller.step()
                except IpmiError as exc:
                    LOG.error("写入风扇失败：%s", exc)
                if args.once:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            LOG.info("停止")
        finally:
            controller.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
