#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py —— 用 PyInstaller 打包 M200F 指纹 U 盘工具（GUI + CLI）
================================================================

本仓库自包含：在仓库根目录运行即可。

用法：
    python build.py                # 打包 finger_tool_gui + finger_tool_cli（onedir）
    python build.py --onefile      # 单文件版（注意杀软误报/启动略慢）
    python build.py --gui-only     # 只打包 GUI
    python build.py --cli-only     # 只打包 CLI

产物：dist/finger_tool_gui/（或 .exe/.app）与 dist/finger_tool_cli/。

English summary:
  Self-contained PyInstaller build script for this repository.
  Run from the repo root; output lands in dist/.
  Note: PyInstaller cannot cross-compile - build on each target OS,
  or use .github/workflows/build.yml (Windows/Linux/macOS matrix).
"""

import argparse
import os
import subprocess
import sys


def pyinstaller(args):
    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm"] + args
    print(">>> " + " ".join(cmd))
    subprocess.check_call(cmd)


def build_gui(onefile):
    args = ["--name", "finger_tool_gui"]
    if os.name == "nt" or sys.platform == "darwin":
        args.append("--windowed")     # no console window on Win/macOS
    if onefile:
        args.append("--onefile")
    args.append("finger_tool_gui.py")
    pyinstaller(args)


def build_cli(onefile):
    args = ["--name", "finger_tool_cli", "--console"]
    if onefile:
        args.append("--onefile")
    args.append("finger_tool_cli.py")
    pyinstaller(args)


def main():
    ap = argparse.ArgumentParser(description="Build M200F tools with PyInstaller")
    ap.add_argument("--onefile", action="store_true")
    ap.add_argument("--gui-only", action="store_true")
    ap.add_argument("--cli-only", action="store_true")
    args = ap.parse_args()
    if not (args.gui_only or args.cli_only):
        args.gui_only = args.cli_only = True
    if args.gui_only:
        build_gui(args.onefile)
    if args.cli_only:
        build_cli(args.onefile)
    print("\nDone. Artifacts are in dist/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
