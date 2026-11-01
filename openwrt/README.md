# 部署为 OpenWrt / QWRT 插件

把本项目跑在 OpenWrt 路由器上，作为 LuCI 菜单插件（iframe 嵌入自带 Web 界面）。

## 依赖

```sh
opkg update
opkg install ipmitool python3 python3-yaml
```

`python3` 比 `python3-light` 省事（含 logging/urllib 等）。`webbrowser` 模块在路由器上缺失，`server.py` 已改为可选导入。

## 安装

```sh
mkdir -p /opt/hr650x
# 把 ipmi_manager.py server.py web/ HR650X.yaml 拷到 /opt/hr650x/
cp HR650X.yaml /opt/hr650x/HR650X.local.yaml   # 再按需编辑 BMC 地址

cp openwrt/init.d/hr650x /etc/init.d/hr650x
chmod +x /etc/init.d/hr650x
/etc/init.d/hr650x enable
/etc/init.d/hr650x start
```

LuCI 插件：

```sh
cp openwrt/luci/controller/hr650x.lua /usr/lib/lua/luci/controller/
mkdir -p /usr/lib/lua/luci/view/hr650x
cp openwrt/luci/view/hr650x/index.htm /usr/lib/lua/luci/view/hr650x/
rm -f /tmp/luci-indexcache /tmp/luci-modulecache/*
/etc/init.d/uhttpd restart
```

## 访问

- 直接：`http://<路由器IP>:8765/`
- LuCI：**服务 → HR650X 风扇**

## 说明

- 服务监听 `0.0.0.0:8765`，OpenWrt 默认防火墙只对 LAN 放行，WAN 访问不到。
- 在界面里填 BMC 地址/账号/密码并保存后，写回 `/opt/hr650x/HR650X.local.yaml`。
- 管理：`/etc/init.d/hr650x start|stop|restart`
