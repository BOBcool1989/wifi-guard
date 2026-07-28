# WiFi 守护程序（WiFiGuard）

无线网络开着却上不了网时，自动切换到其它已保存的 WiFi，保证始终联网。
带右下角系统托盘图标（启停 / 立即检测 / 状态 / 退出），支持开机自启动。

适用于 Windows 10 / 11，普通用户权限即可运行（无需管理员）。

## 功能

- **自动探测真联网**：不是只看"连上 AP"，而是用 gstatic 的 204 探测 + 百度 HTTPS 兜底，能识别"连着 WiFi 却上不了网 / 被认证页（captive portal）拦截"的情况。
- **断网自动切网**：连着却上不了网时，把当前网络拉黑，依次尝试其它**已保存**的 WiFi，连上后重新探测，直到恢复联网。
- **托盘图标开关**：右下角系统托盘，图标颜色即状态（🟢已联网 / 🔴全失败 / 🟡检测中 / ⚪暂停或网卡禁用）；右键菜单可「启停守护」「立即检测」「开机自启动」「退出」。
- **开机自启**：菜单里勾选即可写入注册表 `HKCU\...\Run`（不需要管理员权限）。
- **详细日志**：所有操作记录到 `wifi_guard.log`（exe 同级目录），便于排查。

## 快速开始

### 方式一：直接用打包好的 exe

1. 下载 [Releases](../../releases) 里的 `wifi_guard.exe`
2. 双击运行
3. 右键托盘图标可开启「开机自启动」

### 方式二：从源码运行

```bash
pip install pystray pillow
python wifi_guard.py
```

### 打包成 exe

```bash
python build_exe.py
```

产物：`dist/wifi_guard.exe`（单文件、无黑窗口）。

## 工作原理

1. 每 `CHECK_INTERVAL` 秒探测一次是否真正能联网。
2. 若连着 WiFi 却上不了网，把当前网络拉黑，依次尝试其它已保存的 WiFi。
3. `netsh wlan connect` 命令被系统拒绝（配置文件不可用）时立即跳过，不白等。
4. 找到能联网的网络后清空黑名单，恢复正常状态。

## 配置

`wifi_guard.py` 顶部：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CHECK_INTERVAL` | 15 | 检测间隔（秒） |
| `CONNECT_WAIT` | 5 | 切换后等待联网的秒数 |
| `PREFERRED_ORDER` | `[]` | 期望优先尝试的 SSID 顺序；留空则用系统全部已保存 WiFi |
| `TEST_URLS` | gstatic + 百度 | 联网探测地址 |
| `LOG_FILE` | exe 同级 `wifi_guard.log` | 日志文件路径 |

## 托盘图标颜色

| 颜色 | 含义 |
|------|------|
| 🟢 绿 | 已联网 |
| 🔴 红 | 所有网络都失败 |
| 🟡 黄 | 检测中 / 切换中 |
| ⚪ 灰 | 已暂停 / 网卡未启用 |

## 常见问题

**Q：切换的 WiFi 必须已保存过密码吗？**
A：是的。程序不会凭空连未知网络，只连系统里已保存过密码的 WiFi。

**Q：为什么显示"已尝试所有网络"但没切？**
A：看 `wifi_guard.log`。常见原因：① 当前没有任何已保存网络能真正上网；② `netsh wlan connect` 对某些配置文件返回"没有分配给指定接口的配置文件"（配置文件归属别的接口或已过期），这些会被自动跳过。

**Q：开机自启动怎么关？**
A：托盘图标 → 取消「开机自启动」，或删除注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下的 `WiFiGuard` 项。

## 技术栈

- Python 3
- [pystray](https://pypi.org/project/pystray/) — 系统托盘
- [Pillow](https://pypi.org/project/Pillow/) — 图标生成
- `netsh wlan` — WiFi 状态查询与切换
- Windows 注册表 — 开机自启

## License

MIT
