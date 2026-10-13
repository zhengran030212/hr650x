import logging
import re
import unittest

from pathlib import Path

from ipmi_manager import (
    Band,
    ConfigError,
    Controller,
    Decision,
    FanPolicy,
    IpmiClient,
    IpmiError,
    IpmiTarget,
    Sample,
    build_ipmi_args,
    format_duty,
    ipmi_env,
    load_config,
    parse_config,
    parse_sensor_output,
)


def curve():
    return (
        Band(0, 5, 2),
        Band(5, 40, 10),
        Band(40, 45, 14),
        Band(45, 50, 20),
        Band(50, 60, 50),
        Band(60, 80, 80),
        Band(80, 100, 100),
    )


def policy(**kwargs):
    params = {
        "bands": curve(),
        "hysteresis": 2,
        "fallback_speed": 100,
        "min_speed_on": 10,
        "na_grace": 2,
    }
    params.update(kwargs)
    return FanPolicy(**params)


def online(temp, matched=2, valid=2):
    return Sample(temp=temp, matched=matched, valid=valid)


def offline():
    return Sample(temp=None, matched=2, valid=0)


PATTERN = re.compile(r"^CPU ?[0-9]+ Temp$", re.IGNORECASE)

SENSORS = """
CPU1 Temp        | 46.000     | degrees C  | ok    | na | na | na | na | 95.000 | na
CPU2 Temp        | 41.000     | degrees C  | ok    | na | na | na | na | 95.000 | na
CPU1 VRD Temp    | 70.000     | degrees C  | ok    | na | na | na | na | na | na
PCH Temp         | 55.000     | degrees C  | ok    | na | na | na | na | na | na
""".strip()


def minimal_config(**overrides):
    data = {
        "ipmi": {"host": "10.0.0.1", "username": "ADMIN", "password": "secret"},
        "fan_speeds": [
            {"temp_range": [0, 40], "speed": 10},
            {"temp_range": [40, 100], "speed": 80},
        ],
    }
    data.update(overrides)
    return data


class ParseSensorTest(unittest.TestCase):
    def test_surface_temperature_ignores_dts(self):
        text = """
CPU1_Temp        | 37.000     | degrees C  | ok
CPU2_Temp        | 0.000      | degrees C  | ok
CPU1_DTS         | 65.000     | degrees C  | ok
CPU2_DTS         | 0.000      | degrees C  | ok
CPU1_VR_Temp     | 36.000     | degrees C  | ok
"""
        from ipmi_manager import cpu_temperature_sample

        sample = cpu_temperature_sample(text, PATTERN)
        self.assertEqual(sample.temp, 37.0)
        self.assertEqual(sample.valid, 1)
        self.assertEqual(sample.readings, (("CPU1_Temp", 37.0),))

    def test_surface_temperature_is_used_when_dts_is_missing(self):
        text = """
CPU1_Temp | 37.000 | degrees C | ok
CPU1_DTS | na | degrees C | na
CPU2_DTS | 0.000 | degrees C | ok
"""
        from ipmi_manager import cpu_temperature_sample

        sample = cpu_temperature_sample(text, PATTERN)
        self.assertEqual(sample.temp, 37.0)
        self.assertEqual(sample.valid, 1)
        self.assertEqual(sample.matched, 1)

    def test_related_sensors_and_fan_duty(self):
        from ipmi_manager import parse_fan_duty, parse_related_sensors

        text = """
CPU1_Temp | 35.000 | degrees C | ok
Inlet_Temp | 26.000 | degrees C | ok
FAN1_F_Speed | na | RPM | na
FAN1_R_Speed | 2800.000 | RPM | ok
CPU1_VCCIN | 1.780 | Volts | ok
"""
        sensors = dict((name, value) for name, value, _unit in parse_related_sensors(text))
        self.assertEqual(sensors["CPU1_Temp"], 35.0)
        self.assertNotIn("CPU1_DTS", sensors)
        self.assertEqual(sensors["Inlet_Temp"], 26.0)
        self.assertIsNone(sensors["FAN1_F_Speed"])
        self.assertEqual(sensors["FAN1_R_Speed"], 2800.0)
        self.assertNotIn("CPU1_VCCIN", sensors)
        self.assertEqual(parse_fan_duty("fb 14 1e 3c"), (20, 30, 60))

    def test_uses_reading_column_and_ignores_thresholds_and_vrd(self):
        sample = parse_sensor_output(SENSORS, PATTERN)
        self.assertEqual(sample, Sample(temp=46.0, matched=2, valid=2))

    def test_spaced_cpu_name_and_integer_reading(self):
        text = "CPU 1 Temp | 45 | degrees C | ok | na | 90.000 | na"
        sample = parse_sensor_output(text, PATTERN)
        self.assertEqual(sample.temp, 45.0)

    def test_disabled_sensor_is_skipped_when_another_reading_exists(self):
        text = "CPU1 Temp | 42.5 | degrees C | ok\nCPU2 Temp | disabled | | na"
        sample = parse_sensor_output(text, PATTERN)
        self.assertEqual(sample, Sample(temp=42.5, matched=2, valid=1))

    def test_all_na_is_a_sample_with_no_valid_readings(self):
        text = "CPU1 Temp | na | degrees C | na\nCPU2 Temp | na | | na"
        sample = parse_sensor_output(text, PATTERN)
        self.assertEqual(sample.valid, 0)
        self.assertIsNone(sample.temp)

    def test_unmatched_sensors_name_the_nearby_labels(self):
        with self.assertRaises(Exception) as caught:
            parse_sensor_output("PCH Temp | 40 | degrees C | ok", PATTERN)
        self.assertIn("PCH Temp", str(caught.exception))

    def test_garbage_reading_fails(self):
        with self.assertRaises(Exception):
            parse_sensor_output("CPU1 Temp | hot | degrees C | ok", PATTERN)


class FanPolicyTest(unittest.TestCase):
    def test_curve_and_online_minimum(self):
        fan = policy()
        self.assertEqual(fan.decide(online(3)).speed, 10)
        self.assertEqual(fan.decide(online(30)).speed, 10)
        self.assertEqual(fan.decide(online(42)).speed, 14)
        self.assertEqual(fan.decide(online(70)).speed, 80)

    def test_increase_is_immediate_and_decrease_waits_for_hysteresis(self):
        fan = policy()
        self.assertEqual(fan.decide(online(46)).speed, 20)
        self.assertEqual(fan.decide(online(70)).speed, 80)
        self.assertEqual(fan.decide(online(59)).speed, 80)
        self.assertEqual(fan.decide(online(57)).speed, 50)

    def test_hold_near_lower_edge_then_release(self):
        fan = policy()
        self.assertEqual(fan.decide(online(46)).speed, 20)
        self.assertEqual(fan.decide(online(44)).speed, 20)
        self.assertEqual(fan.decide(online(42)).speed, 14)

    def test_above_curve_sticks_until_hysteresis_clears(self):
        fan = policy(
            bands=(Band(0, 80, 30), Band(80, 100, 80)),
            fallback_speed=100,
            min_speed_on=10,
        )
        self.assertEqual(fan.decide(online(101)).speed, 100)
        self.assertEqual(fan.decide(online(99)).speed, 100)
        self.assertEqual(fan.decide(online(97)).speed, 80)

    def test_initial_na_while_off_uses_off_speed(self):
        fan = policy()
        decision = fan.decide(offline(), power_on=False)
        self.assertEqual(decision.speed, 2)
        self.assertEqual(fan.decide(online(46), power_on=True).speed, 20)

    def test_boot_without_temperature_holds_boot_speed(self):
        fan = policy()
        first = fan.decide(offline(), power_on=True)
        second = fan.decide(None, power_on=True)
        self.assertEqual(first.speed, 20)
        self.assertEqual(second.speed, 20)
        self.assertIn("开机中", first.reason)

    def test_missing_temperature_after_online_keeps_the_last_speed(self):
        fan = policy()
        self.assertEqual(fan.decide(online(70), power_on=True).speed, 80)
        self.assertEqual(fan.decide(offline(), power_on=True).speed, 80)
        self.assertEqual(fan.decide(None, power_on=True).speed, 80)

    def test_power_off_drops_to_the_off_band(self):
        fan = policy()
        fan.decide(online(70), power_on=True)
        self.assertEqual(fan.decide(offline(), power_on=False).speed, 2)
        self.assertEqual(fan.decide(offline(), power_on=True).speed, 20)

    def test_read_failure_does_not_ramp_to_full_speed(self):
        fan = policy()
        fan.decide(online(46), power_on=True)
        decision = fan.decide(None, power_on=None)
        self.assertEqual(decision.speed, 20)


class CommandTest(unittest.TestCase):
    def test_password_stays_in_the_environment(self):
        target = IpmiTarget("lanplus", "192.168.1.10", "ADMIN", "s'ecret")
        args = build_ipmi_args(target, ["sensor"])
        self.assertEqual(
            args,
            ["ipmitool", "-I", "lanplus", "-H", "192.168.1.10", "-U", "ADMIN", "-E", "sensor"],
        )
        self.assertNotIn("s'ecret", args)
        self.assertEqual(ipmi_env(target, {})["IPMI_PASSWORD"], "s'ecret")

    def test_chassis_power_status(self):
        from ipmi_manager import parse_chassis_power

        self.assertTrue(parse_chassis_power("Chassis Power is on\n"))
        self.assertFalse(parse_chassis_power("Chassis Power is off\n"))
        self.assertIsNone(parse_chassis_power("error"))

    def test_local_interface_has_no_credentials(self):
        args = build_ipmi_args(IpmiTarget("open"), ["sensor"])
        self.assertEqual(args, ["ipmitool", "-I", "open", "sensor"])

    def test_duty_is_decimal_without_a_leading_zero(self):
        self.assertEqual(format_duty(10), "10")
        self.assertEqual(format_duty(100), "100")
        seen = {}

        def runner(args, env, timeout):
            seen["args"] = args
            seen["env"] = env
            seen["timeout"] = timeout
            return ""

        client = IpmiClient(
            IpmiTarget("lanplus", "10.0.0.8", "ADMIN", "pw"),
            15,
            runner,
            environ={},
        )
        client.set_speed(10)
        self.assertEqual(
            seen["args"],
            [
                "ipmitool", "-I", "lanplus", "-H", "10.0.0.8", "-U", "ADMIN", "-E",
                "raw", "0x2e", "0x30", "0x00", "0x00", "10",
            ],
        )
        self.assertEqual(seen["env"], {"IPMI_PASSWORD": "pw"})
        self.assertEqual(seen["timeout"], 15)
        self.assertEqual(repr(client.target), (
            "IpmiTarget(interface='lanplus', host='10.0.0.8', "
            "username='ADMIN', password='***')"
        ))


class ConfigTest(unittest.TestCase):
    def test_environment_password_overrides_the_file(self):
        config = parse_config(minimal_config(), {"IPMI_PASSWORD": "from-env"})
        self.assertEqual(config.target.password, "from-env")

    def test_remote_without_host_or_password_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {"ipmi": {"username": "ADMIN", "password": "x"}, "fan_speeds": minimal_config()["fan_speeds"]},
                {},
            )
        with self.assertRaises(ConfigError):
            parse_config(
                {"ipmi": {"host": "10.0.0.1", "username": "ADMIN"}, "fan_speeds": minimal_config()["fan_speeds"]},
                {},
            )

    def test_open_interface_does_not_keep_a_password(self):
        data = minimal_config()
        data["ipmi"] = {"interface": "open", "password": "secret"}
        config = parse_config(data, {"IPMI_PASSWORD": "from-env"})
        self.assertIsNone(config.target.password)
        self.assertEqual(config.exit_speed, 100)

    def test_overlap_and_gap_are_rejected(self):
        overlap = minimal_config(fan_speeds=[
            {"temp_range": [0, 50], "speed": 10},
            {"temp_range": [40, 100], "speed": 80},
        ])
        gap = minimal_config(fan_speeds=[
            {"temp_range": [0, 40], "speed": 10},
            {"temp_range": [50, 100], "speed": 80},
        ])
        with self.assertRaises(ConfigError):
            parse_config(overlap, {})
        with self.assertRaises(ConfigError):
            parse_config(gap, {})

    def test_explicit_null_exit_speed_keeps_the_last_duty(self):
        config = parse_config(minimal_config(exit_speed=None), {})
        self.assertIsNone(config.exit_speed)

    def test_shipped_config_loads(self):
        path = Path(__file__).resolve().parents[1] / "HR650X.yaml"
        config = load_config(path, {"IPMI_PASSWORD": "test"})
        self.assertEqual(config.target.host, "192.168.1.100")
        self.assertEqual(config.target.username, "ADMIN")
        self.assertEqual(len(config.bands), 7)
        self.assertEqual(config.bands[0].speed, 2)
        self.assertEqual(config.min_speed_on, 10)
        self.assertEqual(config.exit_speed, 20)
        self.assertEqual(config.boot_speed, 20)


class FakeClient:
    def __init__(self, readings):
        self.readings = list(readings)
        self.sets = []
        self.fail_set = False

    def read_sensors(self):
        item = self.readings.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def set_speed(self, speed):
        if self.fail_set:
            raise IpmiError("denied")
        self.sets.append(speed)


def controller(client, **kwargs):
    params = {
        "policy": policy(),
        "client": client,
        "sensor_pattern": PATTERN.pattern,
        "dry_run": False,
        "exit_speed": 100,
        "log": logging.getLogger("hr650x-test"),
    }
    params.update(kwargs)
    return Controller(**params)


class ControllerTest(unittest.TestCase):
    def test_writes_only_when_duty_changes_and_ignores_vrd(self):
        hot = "CPU1 Temp | 70 | degrees C | ok\nCPU2 Temp | 68 | degrees C | ok"
        client = FakeClient([SENSORS, SENSORS, hot])
        fan = controller(client)
        self.assertEqual(fan.step().speed, 20)
        self.assertEqual(fan.step().speed, 20)
        self.assertEqual(fan.step().speed, 80)
        self.assertEqual(client.sets, [20, 80])

    def test_dry_run_and_null_exit_do_not_write(self):
        client = FakeClient([SENSORS])
        fan = controller(client, dry_run=True, exit_speed=None)
        fan.step()
        fan.shutdown()
        self.assertEqual(client.sets, [])
        self.assertEqual(fan.last_speed, 20)

    def test_shutdown_raises_fans_when_they_were_low(self):
        client = FakeClient([SENSORS])
        fan = controller(client)
        fan.step()
        fan.shutdown()
        self.assertEqual(client.sets, [20, 100])

    def test_failed_write_is_retried(self):
        client = FakeClient([SENSORS, SENSORS])
        client.fail_set = True
        fan = controller(client)
        with self.assertRaises(IpmiError):
            fan.step()
        self.assertIsNone(fan.last_speed)
        client.fail_set = False
        fan.step()
        self.assertEqual(client.sets, [20])

    def test_sensor_failure_while_running_keeps_the_current_speed(self):
        client = FakeClient([SENSORS, IpmiError("timeout")])
        fan = controller(client)
        fan.step()
        decision = fan.step()
        self.assertEqual(decision.speed, 20)
        self.assertEqual(client.sets, [20])


if __name__ == "__main__":
    unittest.main()
