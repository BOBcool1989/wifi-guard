import sys
sys.path.insert(0, r"C:\Users\winda\WorkBuddy\2026-07-28-08-51-05\wifi-guard")
import wifi_guard as w

# 1) 半角冒号解析当前 SSID
w.run_cmd = lambda c, t=20: """Name                   : WLAN
State                  : connected
SSID                   : MyHomeWiFi
BSSID                  : 00:11:22:33:44:55"""
assert w.get_current_ssid() == "MyHomeWiFi", w.get_current_ssid()

# 2) 全角冒号解析（中文系统）
w.run_cmd = lambda c, t=20: """名称                   ：WLAN
状态                   ：已连接
SSID                   ：MyHomeWiFi
BSSID                  ：00:11:22:33:44:55"""
iface = w.get_wlan_interface()
assert iface["ssid"] == "MyHomeWiFi", iface
assert iface["state"] == "已连接", iface

# 3) 全角冒号解析已保存列表
w.run_cmd = lambda c, t=20: """用户配置文件
-------------
    所有用户配置文件     ：MyHomeWiFi
    所有用户配置文件     ：Office5G
    所有用户配置文件     ：CafeFree"""
profiles = w.get_saved_profiles()
assert profiles == ["MyHomeWiFi", "Office5G", "CafeFree"], profiles

# 4) PREFERRED_ORDER 优先排序生效
w.PREFERRED_ORDER = ["CafeFree", "Office5G"]
w.run_cmd = lambda c, t=20: """    所有用户配置文件     : MyHomeWiFi
    所有用户配置文件     : Office5G
    所有用户配置文件     : CafeFree"""
profiles = w.get_saved_profiles()
assert profiles == ["CafeFree", "Office5G", "MyHomeWiFi"], profiles
w.PREFERRED_ORDER = []

# 5) 无网卡
w.run_cmd = lambda c, t=20: "There is no wireless interface on the system"
assert w.get_wlan_interface() is None

# 6) 联网探测不崩溃
assert isinstance(w.check_internet(), bool)

# 7) ensure_connectivity 一次遍历：模拟当前网失败，应切到下一个成功的
w.blacklist.clear()
w.PREFERRED_ORDER = []
w.get_saved_profiles = lambda: ["Bad", "Good", "Ugly"]
w.get_current_ssid = lambda: "Bad"
calls = []
def fake_connect(ssid): calls.append(ssid)
w.connect_wifi = fake_connect
# 模拟：Bad 已连(拉黑)→切 Good→Good 能上网
seq = {"Good": True}
w.check_internet = lambda: seq.get(w.get_current_ssid_now(), False)
w.get_current_ssid_now = lambda: calls[-1] if calls else "Bad"
ok = w.ensure_connectivity()
assert ok is True, ok
assert calls == ["Good"], calls

# 8) 全部失败时返回 False 且都试过
w.blacklist.clear()
w.get_saved_profiles = lambda: ["Bad", "Good", "Ugly"]
w.get_current_ssid = lambda: "Bad"
calls.clear()
w.check_internet = lambda: False
w.connect_wifi = fake_connect
ok = w.ensure_connectivity()
assert ok is False, ok
assert calls == ["Good", "Ugly"], calls  # Bad 是 current 被跳过

print("ALL_LOGIC_TESTS_PASSED")
