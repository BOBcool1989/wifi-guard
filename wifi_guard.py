# -*- coding: utf-8 -*-
"""
WiFi 守护程序（WiFiGuard）
=====================================================================
功能：
  1. 无线网络开启时，持续探测本机是否真正可以联网（不是只连上 AP）。
  2. 一旦检测到“连上 WiFi 但上不了网”，自动切换到其它已保存的无线网，
     逐个尝试直到恢复联网，从而保证网络始终联通。
  3. 开机自启动（写入当前用户注册表 Run 项，无需管理员权限）。
  4. 右下角系统托盘图标：可一键开启/暂停守护、立即检测、查看状态、退出。

依赖：pystray、Pillow（托盘图标与界面）。
说明：所有 netsh 命令普通用户即可执行，无需管理员。
=====================================================================
"""

import os
import sys
import time
import socket
import logging
import subprocess
import threading
import urllib.request
from datetime import datetime

import pystray
from pystray import MenuItem as Item
from PIL import Image, ImageDraw

# Windows 注册表（开机自启用）；非 Windows 占位，避免导入报错
if os.name == "nt":
    import winreg
else:  # pragma: no cover
    class _DummyReg:
        HKEY_CURRENT_USER = 0
        KEY_SET_VALUE = 0
        REG_SZ = 0
        @staticmethod
        def OpenKey(*a, **k): raise OSError
        @staticmethod
        def QueryValueEx(*a, **k): raise OSError
        @staticmethod
        def SetValueEx(*a, **k): pass
        @staticmethod
        def DeleteValue(*a, **k): pass
    winreg = _DummyReg()

# ====================== 1. 配置区（可按需修改） ======================

# 检测间隔（秒）：每隔这么久探测一次联网状态。
CHECK_INTERVAL = 15

# 切换 WiFi 后，等待其建立连接并获取 IP 的时长（秒）。
# 不宜过长：连接命令被系统接受后，通常 3-5 秒就能建立关联并拿到 IP。
CONNECT_WAIT = 5

# 单次连接最长等待秒数：每 1 秒查一次网卡状态，connected 即停止等待。
# 这样既不会刚"正在连接"就切下一个，也不会死等。
CONNECT_MAX_WAIT = 10

# 整个候选列表最多重试几轮（给瞬态故障容错，不能一次失败就放弃）。
MAX_ROUNDS = 3

# 联网探测使用的地址：
#   - gstatic 的 generate_204 在“真能上网”时返回 HTTP 204，最可靠；
#   - 百度 HTTPS 成功说明没有被 captive portal（认证页）拦截。
TEST_URLS = [
    "http://www.gstatic.com/generate_204",
    "http://connectivitycheck.gstatic.com/generate_204",
    "https://www.baidu.com",
]

# 期望优先尝试的 WiFi 列表（按你心仪的顺序填写 SSID）。
# 留空 [] 表示“使用系统已保存的全部 WiFi，按 netsh 返回顺序尝试”。
PREFERRED_ORDER = []

# 注册表开机自启项名称。
REG_APP_NAME = "WiFiGuard"
REG_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ====================== 日志配置 ======================
# 日志文件路径：放在 exe/脚本 同级目录的 wifi_guard.log
# 打包后是 exe 所在目录；脚本运行时是脚本所在目录。
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_BASE_DIR, "wifi_guard.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8",
)
log = logging.getLogger("WiFiGuard")
# 关掉 PIL 的 DEBUG 日志，避免刷屏
logging.getLogger("PIL").setLevel(logging.WARNING)

# ====================== 2. 运行状态（全局共享） ======================

state = {
    "enabled": True,          # 守护是否开启
    "stop": False,            # 退出标志
    "status_text": "初始化…", # 当前状态文字
    "status_color": "gray",   # 图标颜色：green/red/yellow/gray
    "trigger": threading.Event(),  # 用于“立即检测”打断等待
    "last_ssid": None,        # 上次成功读到的 SSID，用于 netsh 偶发空值时兜底
}
blacklist = set()  # 本轮断网时已确认“连上也没网”的 SSID，联网恢复后清空

# ====================== 3. 系统命令封装 ======================

def run_cmd(cmd, timeout=20):
    """
    执行 shell 命令并返回合并后的输出文本（兼容中英文系统）。
    注意：打包成 --noconsole exe 后，subprocess.run(capture_output=True)
    在某些情况下拿不到子进程输出，因此改用显式 PIPE + CREATE_NO_WINDOW，
    并在 cmd 前加 chcp 65001 切到 UTF-8，避免中文系统 GBK 乱码导致解析失败。
    """
    try:
        # 切到 UTF-8 代码页，避免中文系统 GBK 编码问题
        full_cmd = f"chcp 65001 >nul 2>&1 & {cmd}"
        res = subprocess.run(
            full_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            # --noconsole 打包时避免弹黑窗；有控制台时无影响
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (res.stdout or "") + (res.stderr or "")
        log.debug("run_cmd: %s | rc=%s | out=%r", cmd, res.returncode, out[:200])
        return out
    except Exception as e:
        log.warning("run_cmd 异常: %s | %s", cmd, e)
        return ""


def _split_kv(line):
    """
    把 'Key : Value' 形式的行拆成 (key, value)；不符合返回 (None, None)。
    同时支持半角冒号 ':' 和全角冒号 '：'（中文系统 netsh 输出常用全角）。
    """
    # 优先半角，其次全角
    for sep in (":", "："):
        if sep in line:
            key, val = line.split(sep, 1)
            return key.strip().lower(), val.strip()
    return None, None


def get_wlan_interface():
    """
    读取无线网卡信息。
    返回 dict（含 name/state/ssid）或 None（无无线网卡 / 网卡被禁用）。
    兼容中英文系统键名：name/名称、state/状态、ssid/SSID。
    """
    out = run_cmd("netsh wlan show interfaces")
    # 没有无线接口（网卡禁用或未安装）时 netsh 会提示无接口
    if ("There is no wireless interface" in out) or ("没有无线接口" in out):
        log.info("无线网卡未启用（netsh 提示无接口）")
        return None
    info = {"name": None, "state": None, "ssid": None}
    # 各字段对应的中英文键名集合（已小写）
    name_keys = {"name", "名称"}
    state_keys = {"state", "状态"}
    ssid_keys = {"ssid"}  # 中英文都是 SSID
    for line in out.splitlines():
        key, val = _split_kv(line)
        if key is None:
            continue
        # 注意：必须精确匹配键名，否则 BSSID 等含 "ssid" 子串会被误判
        if key in name_keys and info["name"] is None:
            info["name"] = val
        elif key in state_keys:
            info["state"] = val
        elif key in ssid_keys and val:  # SSID 字段可能为空（未连接）
            info["ssid"] = val
    log.info("网卡信息: name=%s state=%s ssid=%s",
             info["name"], info["state"], info["ssid"])
    return info


def get_current_ssid(retries=3):
    """
    获取当前已连接的 WiFi 名称，未连接返回 None。
    netsh 在刚切换完的瞬间，SSID 字段可能为空，因此最多重试 retries 次。
    """
    for _ in range(retries):
        iface = get_wlan_interface()
        if iface and iface.get("ssid"):
            ssid = iface["ssid"]
            state["last_ssid"] = ssid  # 记下来兜底
            return ssid
        time.sleep(0.3)
    # 重试都失败：用上次的兜底，避免显示"未知"
    return state.get("last_ssid")


def get_saved_profiles():
    """
    获取系统已保存的 WiFi 配置文件名（SSID）列表。
    若配置了 PREFERRED_ORDER，把优先网排在前面，其余按系统顺序追加。
    """
    out = run_cmd("netsh wlan show profiles")
    profiles = []
    for line in out.splitlines():
        key, val = _split_kv(line)
        # 中英文系统分别识别（键名精确匹配，规避误判）
        if key in ("all user profile", "所有用户配置文件") and val:
            if val not in profiles:
                profiles.append(val)

    # 若用户指定了优先顺序，重排：优先网在前，其余保持原序追加
    if PREFERRED_ORDER:
        preferred = [s for s in PREFERRED_ORDER if s in profiles]
        rest = [s for s in profiles if s not in preferred]
        profiles = preferred + rest
    return profiles


def get_signal_map():
    """
    扫描当前可见的 WiFi，返回 {ssid: 信号百分比(0-100)}。
    netsh wlan show networks mode=bssid 输出含多段 SSID 块，每段有 Signal 行。
    用于给候选网按信号强弱排序，优先连信号好的。
    扫不到的网返回值缺省，排序时按 0 处理。

    注意：netsh 输出里 SSID 块标题形如 "SSID 1 : xxx"，key 是 "ssid 1"，
    所以这里用 startswith("ssid") 判断，而不是精确匹配。
    """
    out = run_cmd("netsh wlan show networks mode=bssid")
    sig_map = {}
    cur_ssid = None
    for line in out.splitlines():
        key, val = _split_kv(line)
        if key is None:
            continue
        # SSID 块标题：key 形如 "ssid 1" / "ssid 2"，用 startswith 识别
        if key.startswith("ssid") and val:
            cur_ssid = val
            sig_map.setdefault(cur_ssid, 0)
        elif key in ("signal", "信号") and cur_ssid and val:
            # 形如 "86%"，取数字
            pct = val.replace("%", "").replace("％", "").strip()
            try:
                sig_map[cur_ssid] = max(sig_map.get(cur_ssid, 0), int(pct))
            except ValueError:
                pass
    log.info("可见网络信号: %s", sig_map)
    return sig_map


def sort_profiles_by_signal(profiles, visible_only=True):
    """
    把已保存的配置文件列表按信号强度降序排列。
    信号好的优先试，搜不到的（sig 缺省）按 0 排在后面、保持原相对顺序。
    若 visible_only=True，只返回当前可见的网络（排除扫不到的），避免盲连不存在的 WiFi。
    """
    sig = get_signal_map()
    # 稳定排序：先按是否可见分桶，可见的按信号降序，不可见的保持原序
    visible = [(s, sig.get(s, 0)) for s in profiles if s in sig]
    invisible = [s for s in profiles if s not in sig]
    visible.sort(key=lambda x: x[1], reverse=True)
    if visible_only:
        ordered = [s for s, _ in visible]
        log.info("按信号排序后候选（仅可见）: %s",
                 [(s, sig.get(s, 0)) for s in ordered])
        log.info("已排除不可见网络 (%d个): %s", len(invisible), invisible)
        return ordered
    ordered = [s for s, _ in visible] + invisible
    log.info("按信号排序后候选: %s",
             [(s, sig.get(s, 0)) for s in ordered])
    return ordered


def connect_wifi(ssid):
    """
    尝试连接到指定 SSID（需已保存过密码）。
    返回 True 表示 netsh 命令本身执行成功（不代表一定连上了，只是命令没报错）；
    返回 False 表示命令直接被拒绝（配置文件不存在/不匹配接口等），应跳过此网。
    """
    iface = get_wlan_interface()
    ifname = iface["name"] if iface and iface.get("name") else ""
    try:
        if ifname:
            cmd = f'netsh wlan connect name="{ssid}" interface="{ifname}"'
        else:
            cmd = f'netsh wlan connect name="{ssid}"'
        log.info("执行连接: %s", cmd)
        # 与 run_cmd 同样的处理：chcp 65001 + 显式 PIPE + CREATE_NO_WINDOW
        full_cmd = f"chcp 65001 >nul 2>&1 & {cmd}"
        res = subprocess.run(
            full_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (res.stdout or "") + (res.stderr or "")
        log.info("连接结果: ssid=%s rc=%s out=%r", ssid, res.returncode, out[:200])
        # netsh connect 成功时通常无输出或提示"连接请求已成功完成"
        if res.returncode != 0:
            return False
        fail_hints = ("没有分配", "无法连接", "未找到", "not found",
                      "cannot", "失败", "error", "拒绝")
        if any(h in out.lower() for h in [h.lower() for h in fail_hints]):
            return False
        return True
    except Exception as e:
        log.warning("连接异常: ssid=%s %s", ssid, e)
        return False


def wait_for_connection(target_ssid, max_wait=CONNECT_MAX_WAIT):
    """
    连接命令发出后，轮询网卡状态，等到真正 connected 且 SSID 变成目标网。
    每 1 秒查一次，避免"刚发出连接就切下一个"的问题。
    返回 True 表示已连上目标网，False 表示超时仍未连上。
    """
    log.info("等待连接 %s（最长 %s 秒）...", target_ssid, max_wait)
    for i in range(max_wait):
        time.sleep(1)
        iface = get_wlan_interface()
        if iface is None:
            continue
        st = (iface.get("state") or "").lower()
        ssid = iface.get("ssid")
        if "connected" in st and ssid == target_ssid:
            log.info("✓ 已连上 %s（第 %d 秒）", target_ssid, i + 1)
            return True
        if "connected" in st and ssid and ssid != target_ssid:
            # 连上了别的网，可能 Windows 自动连的，也算失败
            log.info("连上了 %s 而非 %s，视为失败", ssid, target_ssid)
            return False
        # 否则继续等（disconnected / associating 等）
    log.warning("等待 %s 超时（%s 秒未连上）", target_ssid, max_wait)
    return False


def check_internet():
    """
    探测是否真正可以联网，返回 True/False。
    重点：规避 captive portal（酒店/公司认证页）误判——
      - generate_204 端点正常应返回 204 且响应体为空；
        若返回 200 且带 HTML，多半是被认证页劫持，不算联网。
      - HTTPS 百度返回 200 且响应体较短时也疑似劫持，校验长度。
    """
    for url in TEST_URLS:
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                body = resp.read(2048)  # 只读前 2KB 判断
                if url.endswith("/generate_204"):
                    # 204 且无响应体才算真联网
                    if status == 204 and not body:
                        log.info("联网探测: %s -> 204 无体, 判定联网", url)
                        return True
                    log.debug("联网探测: %s -> status=%s body=%dB, 继续",
                              url, status, len(body))
                else:
                    # HTTPS 百度：200 且响应体像正常网页（>512B）才算
                    if status == 200 and len(body) > 512:
                        log.info("联网探测: %s -> 200 body=%dB, 判定联网",
                                 url, len(body))
                        return True
                    log.debug("联网探测: %s -> status=%s body=%dB, 继续",
                              url, status, len(body))
        except Exception as e:
            log.debug("联网探测: %s 异常 %s", url, e)
            continue
    # HTTP 全失败时，用 DNS 解析兜底判断（能解析说明至少链路通）
    try:
        socket.gethostbyname("www.baidu.com")
        log.info("联网探测: HTTP 全失败但 DNS 解析成功, 判定联网")
        return True
    except Exception as e:
        log.warning("联网探测: 全部失败, DNS 也失败 %s", e)
        return False


# ====================== 4. 核心守护逻辑 ======================

def ensure_connectivity():
    """
    保证联网：当前若能上网直接返回 True；
    否则把当前（失败）SSID 拉黑，按信号强度排序依次尝试其它已保存 WiFi。
    整个候选列表跑 MAX_ROUNDS 轮（默认 3 轮），给瞬态故障容错。
    每个网连完用 wait_for_connection 等真连上再探测，避免切换太快。
    """
    log.info("=== ensure_connectivity 开始 ===")
    if check_internet():
        blacklist.clear()  # 联网恢复，清空黑名单
        log.info("当前已联网，清空黑名单")
        return True

    profiles = get_saved_profiles()
    log.info("已保存配置文件: %d 个", len(profiles))
    if not profiles:
        log.warning("没有任何已保存配置文件")
        return False

    current = get_current_ssid()
    log.info("当前 SSID: %s", current)

    # 跑 MAX_ROUNDS 轮，每轮清黑名单重来（但本轮内仍记录失败的网避免重复试）
    for round_no in range(1, MAX_ROUNDS + 1):
        log.info("====== 第 %d/%d 轮 ======", round_no, MAX_ROUNDS)
        # 按信号强度排序候选（每轮重新扫，信号会变；默认只看可见网络）
        ordered = sort_profiles_by_signal(profiles)
        log.info("本轮候选 (%d个): %s", len(ordered), ordered)
        if not ordered:
            log.warning("本轮无可尝试的WiFi（可见网络与已保存配置无交集）")
            continue

        for candidate in ordered:
            if state["stop"]:
                return False
            # 本轮内已试过失败的跳过；当前网也跳过
            if candidate in blacklist or candidate == current:
                log.debug("跳过(黑名单或当前): %s", candidate)
                continue
            log.info("尝试: %s", candidate)
            set_status(f"切换至 {candidate}…", "yellow")
            # 先看连接命令本身是否被系统接受
            cmd_ok = connect_wifi(candidate)
            if not cmd_ok:
                # 配置文件不可用，本轮跳过，下轮重试
                log.info("连接命令被拒，跳过: %s", candidate)
                blacklist.add(candidate)
                continue
            # 命令接受了，轮询等真正 connected（最长 CONNECT_MAX_WAIT 秒）
            connected = wait_for_connection(candidate)
            if not connected:
                log.info("✗ %s 未能在超时内连上，拉黑继续", candidate)
                blacklist.add(candidate)
                continue
            # 已连上目标网，探测是否真联网
            if check_internet():
                blacklist.clear()
                log.info("✓✓ 切换成功并联网: %s", candidate)
                return True
            log.info("✗ %s 连上但仍无法联网，拉黑继续", candidate)
            blacklist.add(candidate)

        # 本轮所有候选都失败，清空黑名单准备下一轮重来
        log.warning("第 %d 轮全部失败，清空黑名单准备下一轮", round_no)
        blacklist.clear()
        # 下一轮仍把"当前连着的失败网"拉黑，避免立刻切回
        if current:
            blacklist.add(current)
        # 轮间小憩，给无线服务喘口气
        if round_no < MAX_ROUNDS:
            log.info("轮间等待 3 秒...")
            time.sleep(3)

    log.warning("====== %d 轮全部尝试完毕，均无法联网 ======", MAX_ROUNDS)
    return False


def set_status(text, color):
    """
    更新状态文字与托盘图标颜色。
    注意：pystray 在 Windows 上不保证跨线程更新图标线程安全，
    因此这里用 try 包裹，偶发失败不影响守护主流程。
    """
    state["status_text"] = text
    state["status_color"] = color
    _refresh_icon()


def _refresh_icon():
    """安全地刷新托盘图标与菜单（在守护线程中调用）。"""
    if not icon_ref:
        return
    try:
        icon_ref.icon = make_icon(state["status_color"])
        icon_ref.title = f"WiFi守护 · {state['status_text']}"
        # 通知托盘重绘菜单，让动态状态项文字刷新
        icon_ref.update_menu()
    except Exception:
        pass


def do_check():
    """执行一次检测与切换。"""
    log.info("====== do_check 开始 ======")
    set_status("检测中…", "yellow")
    iface = get_wlan_interface()
    if iface is None:
        log.warning("无线网卡未启用")
        set_status("无线网卡未启用", "gray")
        return

    state_text = iface.get("state") or ""
    if "disconnected" in state_text.lower():
        log.info("网卡状态: disconnected（未连任何网），将尝试连接已保存网络")

    ok = ensure_connectivity()
    if ok:
        ssid = get_current_ssid()
        if ssid:
            log.info("do_check 结果: 已联网 (%s)", ssid)
            set_status(f"已联网（{ssid}）", "green")
        else:
            log.info("do_check 结果: 已联网（SSID 读不到）")
            set_status("已联网", "green")
    else:
        log.warning("do_check 结果: 无法联网")
        set_status("无法联网（已尝试所有网络）", "red")


def guard_loop():
    """
    后台守护线程主循环。
    暂停/恢复语义：恢复守护时立即触发一次检测（不丢失意图）；
    暂停期间若收到 trigger，记一个 pending，恢复后补检测。
    """
    pending_after_resume = False
    while not state["stop"]:
        if state["enabled"]:
            try:
                do_check()
            except Exception as e:
                set_status(f"异常：{e}", "red")
            # 等待一个检测周期，或被“立即检测”提前唤醒
            fired = state["trigger"].wait(CHECK_INTERVAL)
            state["trigger"].clear()
            if not state["enabled"] and fired:
                # 暂停期间被唤醒，标记恢复后要补检测
                pending_after_resume = True
        else:
            set_status("已暂停", "gray")
            state["trigger"].wait()  # 阻塞直到被唤醒
            state["trigger"].clear()
            # 被唤醒：要么是恢复守护，要么是暂停期间的"立即检测"
            if state["enabled"]:
                pending_after_resume = False  # 直接进下一轮检测
            elif pending_after_resume:
                # 仍在暂停，但等恢复后补一次
                pass


# ====================== 5. 系统托盘图标 ======================

_icon_cache = {}  # 颜色 → PIL.Image，避免每次状态变更都重建图标


def make_icon(color):
    """根据状态颜色生成托盘图标（彩色圆 + 白色 WiFi 弧）。带缓存。"""
    if color in _icon_cache:
        return _icon_cache[color]
    palette = {
        "green": (0, 160, 80, 255),
        "red": (200, 40, 40, 255),
        "yellow": (220, 180, 40, 255),
        "gray": (130, 130, 130, 255),
    }
    fill = palette.get(color, palette["gray"])
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([6, 6, 58, 58], fill=fill)  # 背景圆
    cx, cy = 32, 46
    for r in (20, 13, 6):  # 三道白色弧线
        d.arc([cx - r, cy - r, cx + r, cy + r], start=200, end=340,
              fill="white", width=4)
    d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill="white")  # 中心圆点
    _icon_cache[color] = img
    return img


def toggle_enabled(icon, item):
    state["enabled"] = not state["enabled"]
    # 无论开启还是暂停都唤醒线程：开启→立即检测，暂停→跳出阻塞改显示"已暂停"
    state["trigger"].set()
    icon.update_menu()


def trigger_now(icon, item):
    state["trigger"].set()


def toggle_autostart(icon, item):
    on = is_autostart_on()
    set_autostart(not on)


def quit_app(icon, item):
    state["stop"] = True
    state["trigger"].set()
    icon.stop()


def status_text(item):
    """返回当前状态文字，用于菜单动态展示。"""
    return f"状态：{state['status_text']}"


def _noop(icon, item):
    """空操作：给纯展示型菜单项占位用。"""
    pass


def build_menu():
    return pystray.Menu(
        # 纯展示项：text 用回调动态返回，enabled=False 不可点击
        Item(status_text, _noop, enabled=False),
        Item("启停守护", toggle_enabled,
             checked=lambda item: state["enabled"]),
        Item("立即检测", trigger_now),
        Item("开机自启动", toggle_autostart,
             checked=lambda item: is_autostart_on()),
        pystray.Menu.SEPARATOR,
        Item("退出", quit_app),
    )


icon_ref = None  # 全局引用，便于 set_status 更新图标

# ====================== 6. 开机自启动（注册表） ======================

def _target_command():
    """生成开机自启要执行的命令。打包成 exe 后直接用 exe 路径。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # 脚本形态：用 pythonw 跑，避免弹出黑窗口
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def is_autostart_on():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH) as key:
            winreg.QueryValueEx(key, REG_APP_NAME)
        return True
    except Exception:
        return False


def set_autostart(on):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH,
                           0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, REG_APP_NAME, 0,
                                  winreg.REG_SZ, _target_command())
            else:
                try:
                    winreg.DeleteValue(key, REG_APP_NAME)
                except Exception:
                    pass
    except Exception as e:
        set_status(f"自启设置失败：{e}", "red")


# ====================== 7. 程序入口 ======================

def main():
    global icon_ref
    log.info("================ WiFiGuard 启动 ================")
    log.info("版本: v5 (带日志) | frozen=%s | exe=%s | 脚本=%s",
             getattr(sys, "frozen", False), sys.executable, __file__)
    log.info("日志文件: %s", LOG_FILE)
    # 先做一次检测，让托盘图标立刻反映真实状态
    threading.Thread(target=guard_loop, daemon=True).start()

    icon = pystray.Icon(
        "WiFiGuard",
        make_icon("yellow"),
        f"WiFi守护 · {state['status_text']}",
        build_menu(),
    )
    icon_ref = icon
    icon.run()


if __name__ == "__main__":
    main()
