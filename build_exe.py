# -*- coding: utf-8 -*-
"""
一键打包脚本：把 wifi_guard.py 打包成单文件 exe（无控制台窗口）。
运行：python build_exe.py
产物：dist/wifi_guard.exe
依赖：pyinstaller（脚本会自动安装）
"""
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "wifi_guard.py")
DIST = os.path.join(HERE, "dist")


def main():
    # 确保 pyinstaller 可用
    try:
        subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                       check=True, capture_output=True)
    except Exception:
        print("正在安装 PyInstaller …")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"],
                       check=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",        # 单文件
        "--noconsole",      # 无黑窗口（托盘程序）
        "--name", "wifi_guard",
        "--distpath", DIST,
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        SCRIPT,
    ]
    print("开始打包：", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"\n打包完成！exe 位于：{os.path.join(DIST, 'wifi_guard.exe')}")


if __name__ == "__main__":
    main()
