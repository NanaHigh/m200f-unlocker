#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linux_diag.py —— Linux 侧环境/依赖诊断（自包含，纯标准库）
================================================================

在 Linux 虚拟机上用 sudo 运行，逐项检查 M200F 解锁工具所需条件：
  1) Python / 系统信息
  2) pyusb / libusb 是否可用（pyusb 兜底后端的前置）
  3) lsusb 是否能看到 21c4:8381
  4) /dev/sg* 与 /dev/sd[a-z] 节点
  5) 对每个节点发 INQUIRY 和 A1 00（厂商命令），打印原始应答

用法：
  sudo python3 linux_diag.py

"""

import ctypes
import glob
import os
import shutil
import subprocess
import sys


def hx(b):
    return " ".join("%02X" % x for x in b[:32])


def run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return (out.stdout + out.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return "(%s)" % e


# ---- 1) 环境 ----
print("== 1) 环境 ==")
print("python:", sys.version.split()[0], "| platform:", sys.platform)
print("uname:", run(["uname", "-a"]))

# ---- 2) pyusb / libusb ----
print("\n== 2) pyusb / libusb ==")
try:
    import usb.core
    print("pyusb: OK (%s)" % usb.core.__version__ if hasattr(usb.core, "__version__") else "pyusb: OK")
except Exception as e:  # noqa: BLE001
    print("pyusb: 不可用 - %s（pip install pyusb）" % e)
print("libusb 动态库:", run(["ldconfig", "-p"]) and
      "\n".join(l for l in run(["ldconfig", "-p"]).splitlines() if "libusb" in l) or "(未找到)")
print("sg3_utils(sg_raw):", shutil.which("sg_raw") or "(未安装，可选)")

# ---- 3) lsusb ----
print("\n== 3) lsusb ==")
out = run(["lsusb"])
print(out if out else "(无 lsusb 输出)")
if "21c4" in out.lower():
    print(">>> 找到 VID 21c4 设备")
else:
    print(">>> 未在 lsusb 中看到 21c4（设备没枚举到，或虚拟机未透传 USB）")

# ---- 4) 设备节点 ----
print("\n== 4) 设备节点 ==")
sgs = sorted(glob.glob("/dev/sg*"))
sds = sorted(glob.glob("/dev/sd[a-z]"))
print("/dev/sg*:", sgs or "(无 —— 需 sudo modprobe sg 后重插，或 usb-storage 未绑定)")
print("/dev/sd[a-z]:", sds or "(无)")
print("lsblk:", run(["lsblk", "-o", "NAME,TRAN,MODEL,LABEL"]))
print("lsscsi -g:", run(["lsscsi", "-g"]) or "(未安装 lsscsi，可选)")


# ---- 5) SG_IO 探测 ----
class SgIoHdr(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p), ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint), ("info", ctypes.c_uint),
    ]


def sg_io(path, cdb, datalen):
    """对 path 发 SG_IO，返回 (ok, data, sense, err)。"""
    libc = ctypes.CDLL(None, use_errno=True)
    # 显式声明，避免请求号按 32 位 int 截断
    libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
    libc.ioctl.restype = ctypes.c_int
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError as e:
        return False, b"", b"", "open: %s" % e
    try:
        sense = (ctypes.c_ubyte * 32)()
        cdb_buf = (ctypes.c_ubyte * 16)()
        for i, b in enumerate(cdb):
            cdb_buf[i] = b
        data = (ctypes.c_ubyte * max(datalen, 1))()
        sg = SgIoHdr()
        sg.interface_id = ord("S")
        sg.dxfer_direction = 2 if datalen else 0
        sg.cmd_len = len(cdb)
        sg.mx_sb_len = 32
        sg.dxfer_len = datalen
        sg.dxferp = ctypes.cast(data, ctypes.c_void_p)
        sg.cmdp = ctypes.cast(cdb_buf, ctypes.c_void_p)
        sg.sbp = ctypes.cast(sense, ctypes.c_void_p)
        sg.timeout = 5000
        ioctl_nr = 0x2285   # Linux sg.h 中 SG_IO 的固定魔数
        if libc.ioctl(fd, ioctl_nr, ctypes.byref(sg)) != 0:
            return False, b"", b"", "ioctl errno=%d" % ctypes.get_errno()
        return True, bytes(data), bytes(sense[:sg.sb_len_wr]), "status=0x%02x" % sg.status
    finally:
        os.close(fd)


INQ = bytes([0x12, 0, 0, 0, 0x24, 0]) + b"\x00" * 10
A1 = bytes.fromhex("A1000000800000000000000000000000")

print("\n== 5) SG_IO 逐节点探测 ==")
for p in sgs + sds:
    ok, data, sense, err = sg_io(p, INQ, 0x24)
    vendor = ""
    if ok and len(data) >= 8:
        vendor = "".join(chr(c) if 32 <= c < 127 else "." for c in data[8:24]).strip()
    print("%s  INQUIRY ok=%s vendor='%s' sense=%s %s" % (p, ok, vendor, hx(sense), err))
    if ok and data:
        ok2, data2, sense2, err2 = sg_io(p, A1, 128)
        mark = "  <== 有 DM8381" if b"DM8381" in data2 else ""
        print("        A1 00  ok=%s 前16=%s sense=%s %s%s"
              % (ok2, hx(data2[:16]), hx(sense2), err2, mark))
