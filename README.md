# HR650X 风扇调节

用 IPMI 调节联想 ThinkSystem HR650X / HR630X 的风扇占空比。温度优先读 `CPU1_DTS`。没有 DTS 时，再按配置里的传感器名匹配。

命令是 BMC 的 OEM 原始命令：

```text
raw 0x2e 0x30 0x00 0x00 <百分比>
```

百分比用十进制，范围 0–100。`ipmitool` 会把带前导 0 的数当成八进制，所以 10% 要写成 `10`。

同一套命令也用在固件相同的 HR630X 上。不同 BMC 固件的原始命令可能不一样。

## 准备

```bash
# Debian / Ubuntu
sudo apt install ipmitool
python3 -m pip install -r requirements.txt
```

macOS 用 `brew install ipmitool`。

把 `HR650X.yaml` 复制成 `HR650X.local.yaml`，填上 BMC 地址、用户和密码。`HR650X.local.yaml` 已在 `.gitignore` 里，不会被提交。密码也可以只放在环境变量里：

```bash
export IPMI_PASSWORD='BMC 密码'
```

环境变量优先于配置文件。在服务器本机、并且已经加载 `ipmi_si` 时，把 `interface` 改成 `open`，这时不需要地址和密码。

## 界面

```bash
python3 server.py
```

浏览器打开打印出来的地址，默认是 `http://127.0.0.1:8765/`。只监听本机。8765 被占用时加 `--port`。

`--demo` 用模拟温度，不连接 BMC。界面上的「模拟，不写 BMC」作用相同。

界面可以读取温度、手动写入转速、按曲线自动调节，以及保存曲线。保存时如果配置里有密码，会写回 `HR650X.local.yaml`。

## 命令行

```bash
python3 ipmi_manager.py --check
python3 ipmi_manager.py --dry-run --once
python3 ipmi_manager.py
```

`--check` 只验证配置。`--dry-run` 读取温度并打印目标转速，不写 BMC。不加参数时每 10 秒调节一次。

停掉进程时，风扇设为 `exit_speed`。示例配置是 20%。设成 `null` 则保持当前转速。

紧急拉满：

```bash
ipmitool -I lanplus -H "$BMC_HOST" -U "$BMC_USER" -E raw 0x2e 0x30 0x00 0x00 100
```

## 温度

这台 BMC 上：

| 传感器 | 含义 |
| --- | --- |
| `CPU1_DTS` | CPU 数字温度传感器，调节用这个 |
| `CPU1_Temp` | 另一路温度，通常低于 DTS |
| `CPU2_DTS` 为 0 | 该槽位没有 CPU，忽略 |

程序见到 `CPUx_DTS` 就只用 DTS。读数为 0 的槽位不算有效温度。

## 转速

升温立刻换档。降温要低于该档下沿再减去 `hysteresis`（默认 2°C）才降档。占空比没变就不再写 BMC。

| CPU 温度 | 风扇 |
| --- | --- |
| 0–5°C | 2% |
| 5–40°C | 10% |
| 40–45°C | 14% |
| 45–50°C | 20% |
| 50–60°C | 50% |
| 60–80°C | 80% |
| 80–100°C | 100% |

0–5°C 那档只在判定关机后使用。有有效温度时，转速不低于 `min_speed_on`（默认 10%）。

传感器命令失败时写入 `fallback_speed`（默认 100%）。机器已经在线后，CPU 温度连续 `na_grace` 次全是 `na`，这几轮维持高转速，之后才走关机档。

分档来自 [Vision0220/HR650X-IPMI-Auto-Fan](https://github.com/Vision0220/HR650X-IPMI-Auto-Fan)。先用 `--dry-run --once` 确认读得到 `CPU1_DTS`，再让它写转速。

## systemd

密码放在 root 可读的 `/etc/hr650x-fan.env`：

```text
IPMI_PASSWORD=BMC 密码
```

```ini
[Service]
ExecStart=/usr/bin/python3 /opt/hr650x/ipmi_manager.py -c /opt/hr650x/HR650X.local.yaml
EnvironmentFile=/etc/hr650x-fan.env
Restart=on-failure
RestartSec=5
```

`systemctl stop` 会把风扇设为 `exit_speed`。
