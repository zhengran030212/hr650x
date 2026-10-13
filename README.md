# HR650X 风扇调节

用 IPMI 调节联想 ThinkSystem HR650X / HR630X 的风扇占空比。温度用 CPU 表面传感器 `CPU1_Temp`，不用 DTS。

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
| `CPU1_Temp` | CPU 表面温度，调节用这个 |
| `CPU2_Temp` 为 0 | 该槽位没有 CPU，忽略 |

程序只认 `CPUx_Temp`。读数为 0 的槽位不算有效温度。

## 转速

升温立刻换档。降温要低于该档下沿再减去 `hysteresis`（默认 2°C）才降档。占空比没变就不再写 BMC。

| CPU 温度 | 风扇 |
| --- | --- |
| 0–5°C | 2% |
| 5–45°C | 20% |
| 45–55°C | 30% |
| 55–65°C | 40% |
| 65–75°C | 55% |
| 75–85°C | 70% |
| 85–100°C | 100% |

这套分档按表面温度 `CPU1_Temp` 来定。这台机器轻载时表面大约 35°C，DTS 会高出三十多度，不拿来调节。

0–5°C 那档只在判定关机后使用。有有效温度时，转速不低于 `min_speed_on`（默认 10%）。

传感器命令失败，或主机已开机但表面温度还没有读数时，使用 `boot_speed`（默认 20%）。SEL 里从进入 S0 到认出内存大约 90 秒，这段时间温度是空的。已经按温度调过之后又读不到，就保持上一次转速，不拉到 100%。主机关机且没有温度时，才走 0–5°C 那一档。温度超过曲线上限时才用 `fallback_speed`。

分档来自 [Vision0220/HR650X-IPMI-Auto-Fan](https://github.com/Vision0220/HR650X-IPMI-Auto-Fan)。先用 `--dry-run --once` 确认读得到 `CPU1_Temp`，再让它写转速。

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
