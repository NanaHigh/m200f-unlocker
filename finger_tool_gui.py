#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finger_tool_gui.py —— M200F 指纹 U 盘解锁（图形界面版，自包含，跨平台后端）
================================================================

深色科技风 + 雷达呼吸动画；按平台自动选择 SCSI 后端：
  Windows   : ScsiDevice（IOCTL_SCSI_PASS_THROUGH, \\.\PHYSICALDRIVE%d）
  Linux     : SgBackend（/dev/sgX 的 SG_IO ioctl，纯 ctypes，零依赖）
  macOS/其它: UsbBulkBackend（pyusb 直接组 CBW/CSW，需 libusb）

用法：
    python finger_tool_gui.py                # 启动界面并自动检测/解锁
    python finger_tool_gui.py --selftest     # 只检测设备并打印结果（不开窗口）

说明：协议在 Windows 与 Linux（SG_IO）上经过真机验证；
      macOS 后端（pyusb）已实现但尚未真机验证。

English summary:
  Self-contained tkinter GUI for the Hikvision M200F fingerprint USB drive.
  Dark "radar" theme; auto-selects the SCSI backend per platform
  (Windows DeviceIoControl / Linux SG_IO / macOS pyusb CBW-CSW).
  Verified on Windows and Linux (SG_IO); macOS backend (pyusb) implemented
  but not yet validated.
"""

import argparse
import ctypes
import glob
import math
import os
import queue
import re
import struct
import sys
import threading
import time
from ctypes import wintypes as wt

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import scrolledtext
    HAVE_TK = True
except Exception as e:  # noqa: BLE001
    HAVE_TK = False
    TK_IMPORT_ERR = e

try:
    import usb.core
    HAVE_PYUSB = True
except Exception:  # noqa: BLE001
    HAVE_PYUSB = False


IOCTL_SCSI_PASS_THROUGH = 0x0004D004
SCSI_IOCTL_DATA_OUT = 0
SCSI_IOCTL_DATA_IN = 1
A1_00_FW_CDB = "A1 00 00 00 80 00 00 00 00 00 00 00 00 00 00 00"
FW_MARKER = b"DM8381"


def hx(b):
    return " ".join("%02X" % x for x in b)


def parse_hex(text):
    return bytes.fromhex(re.sub(r"\s+", "", text.strip().replace("0x", "")))


# ===========================================================================
# Backend 1: Windows SCSI pass-through (IOCTL_SCSI_PASS_THROUGH)
# ===========================================================================
if os.name == "nt":
    class SCSI_PASS_THROUGH(ctypes.Structure):
        """ntddscsi.h struct (natural alignment; DataBufferOffset is ULONG_PTR)."""
        _fields_ = [
            ("Length", wt.USHORT),
            ("ScsiStatus", wt.BYTE),
            ("PathId", wt.BYTE),
            ("TargetId", wt.BYTE),
            ("Lun", wt.BYTE),
            ("CdbLength", wt.BYTE),
            ("SenseInfoLength", wt.BYTE),
            ("DataIn", wt.BYTE),
            ("DataTransferLength", wt.ULONG),
            ("TimeOutValue", wt.ULONG),
            ("DataBufferOffset", ctypes.c_size_t),
            ("SenseInfoOffset", wt.ULONG),
            ("Cdb", wt.BYTE * 16),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.restype = wt.HANDLE
    _CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                             ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    _DeviceIoControl = _kernel32.DeviceIoControl
    _DeviceIoControl.restype = wt.BOOL
    _DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.restype = wt.BOOL
    _CloseHandle.argtypes = [wt.HANDLE]

    class STORAGE_PROPERTY_QUERY(ctypes.Structure):
        _fields_ = [("PropertyId", wt.ULONG), ("QueryType", wt.ULONG),
                    ("AdditionalParameters", wt.BYTE)]

    class STORAGE_DEVICE_DESCRIPTOR(ctypes.Structure):
        _fields_ = [
            ("Version", wt.ULONG), ("Size", wt.ULONG),
            ("DeviceType", wt.BYTE), ("DeviceTypeModifier", wt.BYTE),
            ("RemovableMedia", wt.BYTE), ("CommandQueueing", wt.BYTE),
            ("VendorIdOffset", wt.ULONG), ("ProductIdOffset", wt.ULONG),
            ("ProductRevisionOffset", wt.ULONG), ("SerialNumberOffset", wt.ULONG),
            ("BusType", wt.BYTE), ("RawPropertiesLength", wt.ULONG),
            ("RawDeviceProperties", wt.BYTE),
        ]

    class ScsiDevice:
        """Thin wrapper around one physical drive handle for SCSI pass-through."""

        def __init__(self, drive_index):
            self.path = r"\\.\PHYSICALDRIVE%d" % drive_index
            self.handle = None

        @property
        def label(self):
            return self.path

        def open(self):
            self.handle = _CreateFileW(
                self.path, 0xC0000000, 0x3, None, 3, 0, None)
            if self.handle == ctypes.c_void_p(-1).value:
                raise OSError(ctypes.get_last_error(),
                              "open %s failed (run as admin?)" % self.path)
            return self

        def close(self):
            if self.handle:
                _CloseHandle(self.handle)
                self.handle = None

        def scsi(self, cdb, datalen=0, direction=SCSI_IOCTL_DATA_IN,
                 timeout=10, lun=0):
            if not self.handle:
                self.open()
            hdr = ctypes.sizeof(SCSI_PASS_THROUGH)
            data_off = (hdr + 0x20 + 15) & ~15
            buf = (ctypes.c_ubyte * (data_off + datalen))()
            spt = SCSI_PASS_THROUGH.from_buffer(buf)
            spt.Length = hdr
            spt.CdbLength = len(cdb)
            spt.SenseInfoLength = 0x20
            spt.DataIn = direction
            spt.Lun = lun
            spt.DataTransferLength = datalen
            spt.TimeOutValue = timeout
            spt.DataBufferOffset = data_off
            spt.SenseInfoOffset = hdr
            for i, b in enumerate(cdb):
                spt.Cdb[i] = b
            returned = wt.DWORD(0)
            ok = _DeviceIoControl(self.handle, IOCTL_SCSI_PASS_THROUGH,
                                  buf, len(buf), buf, len(buf),
                                  ctypes.byref(returned), None)
            if not ok:
                raise OSError(ctypes.get_last_error(), "DeviceIoControl failed")
            return bytes(buf[data_off:data_off + datalen]) if datalen else b""


# ===========================================================================
# Backend 2: Linux SG_IO via /dev/sgX (pure ctypes, no dependencies)
# ===========================================================================
class SgBackend:
    """Linux / macOS: send SCSI commands through /dev/sgX with the SG_IO ioctl."""

    class SgIoHdr(ctypes.Structure):
        """linux/uapi/scsi/sg.h: struct sg_io_hdr."""
        _fields_ = [
            ("interface_id", ctypes.c_int),
            ("dxfer_direction", ctypes.c_int),
            ("cmd_len", ctypes.c_ubyte),
            ("mx_sb_len", ctypes.c_ubyte),
            ("iovec_count", ctypes.c_ushort),
            ("dxfer_len", ctypes.c_uint),
            ("dxferp", ctypes.c_void_p),
            ("cmdp", ctypes.c_void_p),
            ("sbp", ctypes.c_void_p),
            ("timeout", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("pack_id", ctypes.c_int),
            ("usr_ptr", ctypes.c_void_p),
            ("status", ctypes.c_ubyte),
            ("masked_status", ctypes.c_ubyte),
            ("msg_status", ctypes.c_ubyte),
            ("sb_len_wr", ctypes.c_ubyte),
            ("host_status", ctypes.c_ushort),
            ("driver_status", ctypes.c_ushort),
            ("resid", ctypes.c_int),
            ("duration", ctypes.c_uint),
            ("info", ctypes.c_uint),
        ]

    DXFER_NONE, DXFER_TO_DEV, DXFER_FROM_DEV = 0, 1, 2

    def __init__(self, path):
        self.path = path
        self.fd = None

    @property
    def label(self):
        return self.path

    def open(self):
        self.fd = os.open(self.path, os.O_RDWR)
        return self

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def scsi(self, cdb, datalen=0, direction=SCSI_IOCTL_DATA_IN,
             timeout=10, lun=0):
        if self.fd is None:
            self.open()
        libc = ctypes.CDLL(None, use_errno=True)
        # 必须显式声明，否则 ioctl 请求号会按 32 位 int 截断（0xC0505310 会传错）
        libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        libc.ioctl.restype = ctypes.c_int
        sense = (ctypes.c_ubyte * 32)()
        cdb_buf = (ctypes.c_ubyte * 16)()
        for i, b in enumerate(cdb):
            cdb_buf[i] = b
        data = (ctypes.c_ubyte * max(datalen, 1))() if datalen else None
        sg = self.SgIoHdr()
        sg.interface_id = ord("S")
        sg.dxfer_direction = {
            SCSI_IOCTL_DATA_OUT: self.DXFER_TO_DEV,
            SCSI_IOCTL_DATA_IN: self.DXFER_FROM_DEV,
        }.get(direction, self.DXFER_NONE)
        sg.cmd_len = len(cdb)
        sg.mx_sb_len = 32
        sg.dxfer_len = datalen
        sg.dxferp = ctypes.cast(data, ctypes.c_void_p) if data else None
        sg.cmdp = ctypes.cast(cdb_buf, ctypes.c_void_p)
        sg.sbp = ctypes.cast(sense, ctypes.c_void_p)
        sg.timeout = timeout * 1000
        # Linux sg.h 中 SG_IO 是固定魔数 0x2285（不是按结构体大小计算的 _IOWR）
        ioctl_nr = 0x2285
        if libc.ioctl(self.fd, ioctl_nr, ctypes.byref(sg)) != 0:
            raise OSError(ctypes.get_errno(), "SG_IO failed on %s" % self.path)
        return bytes(data) if datalen else b""


# ===========================================================================
# Backend 3: USB Bulk (CBW/CSW) via pyusb — fallback for macOS / others
# ===========================================================================
class UsbBulkBackend:
    """All platforms: build CBW/CSW directly over libusb (pyusb)."""

    def __init__(self, vid=0x21C4, pid=0x8381, ep_out=0x02, ep_in=0x82):
        self.vid, self.pid = vid, pid
        self.ep_out, self.ep_in = ep_out, ep_in
        self.dev = None
        self.tag = 0
        self.last_status = 0

    @property
    def label(self):
        return "USB %04X:%04X" % (self.vid, self.pid)

    def open(self):
        if not HAVE_PYUSB:
            raise RuntimeError("pyusb not available (pip install pyusb)")
        self.dev = usb.core.find(idVendor=self.vid, idProduct=self.pid)
        if self.dev is None:
            raise RuntimeError("USB device %04X:%04X not found" % (self.vid, self.pid))
        self.dev.set_configuration()
        return self

    def close(self):
        self.dev = None

    def scsi(self, cdb, datalen=0, direction=SCSI_IOCTL_DATA_IN,
             timeout=10, lun=0):
        if self.dev is None:
            self.open()
        flags = 0x80 if direction == SCSI_IOCTL_DATA_IN else 0x00
        cbw = (b"USBC" + struct.pack("<II", self.tag, datalen) +
               bytes([flags, lun & 0x0F, 16]) + cdb.ljust(16, b"\x00"))
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        self.dev.write(self.ep_out, cbw, timeout=timeout * 1000)
        data = b""
        if datalen and direction == SCSI_IOCTL_DATA_IN:
            got = 0
            while got < datalen:
                chunk = bytes(self.dev.read(
                    self.ep_in, min(16384, datalen - got), timeout=timeout * 1000))
                if not chunk:
                    break
                data += chunk
                got += len(chunk)
            data = data[:datalen]
        csw = bytes(self.dev.read(self.ep_in, 13, timeout=timeout * 1000))
        self.last_status = csw[12] if len(csw) > 12 else -1
        return data


# ===========================================================================
# Detection / volume helpers
# ===========================================================================
def enumerate_usb_drives():
    if os.name != "nt":
        return []
    out = []
    query = STORAGE_PROPERTY_QUERY()
    query.PropertyId = 0
    query.QueryType = 0
    for i in range(16):
        dev = ScsiDevice(i)
        try:
            dev.open()
            buf = (ctypes.c_ubyte * 1024)()
            returned = wt.DWORD(0)
            ok = _DeviceIoControl(dev.handle, 0x002D1400,
                                  ctypes.byref(query), ctypes.sizeof(query),
                                  buf, ctypes.sizeof(buf),
                                  ctypes.byref(returned), None)
            if ok and returned.value >= ctypes.sizeof(STORAGE_DEVICE_DESCRIPTOR):
                desc = STORAGE_DEVICE_DESCRIPTOR.from_buffer_copy(
                    bytes(buf[:returned.value]))
                if desc.BusType == 7:
                    out.append(i)
        except OSError:
            pass
        finally:
            dev.close()
    return out


def find_secure_volume():
    if os.name != "nt":
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = k32.GetVolumeInformationW
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                   ctypes.POINTER(ctypes.c_uint32),
                   ctypes.POINTER(ctypes.c_uint32),
                   ctypes.POINTER(ctypes.c_uint32),
                   ctypes.c_wchar_p, ctypes.c_uint32]
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        name = ctypes.create_unicode_buffer(256)
        if fn(d + ":\\", name, 256, None, None, None, None, 0):
            if name.value.strip().upper() == "SECURE":
                return d + ":"
    return None


def detect_m200f():
    """Auto-select backend per platform and return (backend, firmware)."""
    def probe(b):
        b.open()
        try:
            data = b.scsi(parse_hex(A1_00_FW_CDB), 128, timeout=5)
            if FW_MARKER in data:
                return "".join(chr(x) if 32 <= x < 127 else "" for x in data[:64]).strip()
        finally:
            b.close()
        return None

    if os.name == "nt":
        for idx in enumerate_usb_drives():
            fw = probe(ScsiDevice(idx))
            if fw:
                return ScsiDevice(idx), fw
    if sys.platform.startswith("linux"):
        for p in sorted(glob.glob("/dev/sg*")):
            try:
                fw = probe(SgBackend(p))
            except OSError:
                continue
            if fw:
                return SgBackend(p), fw
    try:
        fw = probe(UsbBulkBackend())
        if fw:
            return UsbBulkBackend(), fw
    except Exception:  # noqa: BLE001
        pass
    return None, None


def short_label(backend):
    label = backend.label
    if label.startswith(r"\\.\PHYSICALDRIVE"):
        return "盘: " + label.rsplit("DRIVE", 1)[-1]
    return label


def platform_hints():
    if os.name == "nt":
        return "请确认 U 盘已插入且未被其他程序独占（如 FingerTool.exe）。"
    if sys.platform.startswith("linux"):
        return ("Linux：确认 /dev/sgX 存在且有权限（udev 规则或 sudo）；"
                "USB 兜底需先 sudo modprobe -r usb-storage。")
    if sys.platform == "darwin":
        return "macOS：请先安装 libusb（brew install libusb）并确认设备未被占用。"
    return "当前平台暂不支持。"


# ===========================================================================
# Dark theme
# ===========================================================================
C = {
    "bg": "#121212", "card": "#1E1E1E", "ink": "#E0E0E0", "sub": "#9E9E9E",
    "border": "#2C2C2C", "accent": "#00A8FF", "accent_l": "#003C5A",
    "ok": "#00E676", "ok_l": "#004D28", "warn": "#FFCA28", "warn_l": "#594508",
    "err": "#FF5252", "err_l": "#5A1616", "idle": "#5C5C5C", "idle_l": "#242424",
    "log_bg": "#181818", "log_fg": "#A0A0A0", "log_dim": "#616161",
}

FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 16, "bold")
FONT_STATUS = ("Microsoft YaHei UI", 18, "bold")
FONT_BADGE = ("Microsoft YaHei UI", 9, "bold")
FONT_MONO = ("Consolas", 10)

IDLE_RESP = b"\x01\x02\xff"
STATE_RESPS = (b"\x02\x03\xff", b"\x03\x04\xff", b"\x04\x05\xff", b"\x05\x06\xff")

INIT_CMDS = [
    (0, "28 00 00 00 00 00 00 00 10 00 00 00 00 00 00 00", 8192),
    (0, "A1 00 00 00 80 00 00 00 00 00 00 00 00 00 00 00", 128),
    (0, "A1 01 00 04 1D 00 00 00 00 00 00 00 00 00 00 00", 1053),
    (0, "A1 02 00 00 01 00 00 00 00 00 00 00 00 00 00 00", 1),
    (2, "A1 02 00 00 01 00 00 00 00 00 00 00 00 00 00 00", 1),
    (1, "A1 02 00 00 01 00 00 00 00 00 00 00 00 00 00 00", 1),
    (0, "A1 00 00 00 02 00 00 00 00 00 00 00 00 00 00 00", 512),
    (0, "90 29 00 00 00 00 00 00 00 00 00 00 00 00 00 00", 1),
    (0, "90 2B 00 00 00 00 00 00 00 00 00 00 00 00 00 00", 35),
    (0, "90 26 00 00 00 00 00 00 00 00 00 00 00 00 00 00", 1),
]


def state_color(text):
    if "成功" in text:
        return C["ok"], C["ok_l"]
    if "失败" in text or "检测不到" in text or "错误" in text:
        return C["err"], C["err_l"]
    if "检测中" in text:
        return C["warn"], C["warn_l"]
    if "请放上指纹" in text or "等待" in text:
        return C["accent"], C["accent_l"]
    return C["idle"], C["idle_l"]


# ===========================================================================
# Rounded-corner widgets (Canvas-drawn)
# ===========================================================================
def round_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
           x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class RoundedCard(tk.Canvas):
    def __init__(self, master, radius=12, pad=16, height=None,
                 bg_color=C["card"], **kw):
        super().__init__(master, bg=C["bg"], highlightthickness=0, bd=0, **kw)
        self.radius, self.pad = radius, pad
        self.bg_color = bg_color
        self.inner = tk.Frame(self, bg=bg_color)
        self._win = self.create_window(0, 0, anchor="nw", window=self.inner)
        if height:
            self.configure(height=height)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, e):
        self.delete("bg")
        w, h = e.width, e.height
        round_rect(self, 0, 0, w, h, self.radius, fill=self.bg_color, tags="bg")
        self.coords(self._win, self.pad, self.pad)
        self.itemconfigure(self._win,
                           width=max(w - 2 * self.pad, 20),
                           height=max(h - 2 * self.pad, 20))
        self.tag_lower("bg")


class RoundedChip(tk.Canvas):
    def __init__(self, master, text, bg=C["border"], fg=C["ink"],
                 radius=12, pad_x=14, height=26, font=FONT_BADGE):
        super().__init__(master, bg=C["card"], highlightthickness=0,
                         bd=0, height=height, width=60)
        self.text, self.bg, self.fg = text, bg, fg
        self.radius, self.pad_x, self.font = radius, pad_x, font
        self.configure(width=self._measure_width())
        self.bind("<Configure>", self._draw)

    def _measure_width(self):
        return tkfont.Font(font=self.font).measure(self.text) + self.pad_x * 2 + 6

    def _draw(self, e=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        round_rect(self, 0, 0, w, h, self.radius, fill=self.bg)
        self.create_text(w // 2, h // 2, text=self.text, fill=self.fg,
                         font=self.font)

    def set_text(self, text):
        self.text = text
        self.configure(width=self._measure_width())
        self._draw()


class RoundedButton(tk.Canvas):
    def __init__(self, master, text, fg, bg, command, radius=12,
                 width=110, height=40, font=FONT_BOLD):
        super().__init__(master, bg=C["bg"], highlightthickness=0,
                         bd=0, width=width, height=height, cursor="hand2")
        self.text, self.fg, self.bg = text, fg, bg
        self.radius, self.font, self.command = radius, font, command
        self._hovered = False
        self.bind("<Configure>", self._draw)
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda _: self._hover(True))
        self.bind("<Leave>", lambda _: self._hover(False))

    def _draw(self, e=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        fill = self._brighten(self.bg) if self._hovered else self.bg
        round_rect(self, 0, 0, w, h, self.radius, fill=fill)
        self.create_text(w // 2, h // 2, text=self.text, fill=self.fg,
                         font=self.font)

    def _hover(self, on):
        self._hovered = on
        self._draw()

    @staticmethod
    def _brighten(hex_color, amount=30):
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        return "#%02x%02x%02x" % (min(255, r + amount), min(255, g + amount),
                                  min(255, b + amount))


# ===========================================================================
# Main window
# ===========================================================================
class FingerGUI:
    def __init__(self, root):
        self.root = root
        self.msg_q = queue.Queue()
        self.stop_event = threading.Event()
        self.pulse_job = None
        self.pulse_on = False
        self.pulse_phase = 0.0
        self.pulse_interval = 30
        self._current_status = "正在检测设备..."
        self._current_colors = (C["idle"], C["idle_l"])

        root.title("M200F Unlocker")
        root.configure(bg=C["bg"])
        root.geometry("640x780")
        root.minsize(580, 700)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self.root.after(100, self._poll_queue)
        threading.Thread(target=self._worker_main, daemon=True).start()

    def _build_ui(self):
        outer = tk.Frame(self.root, bg=C["bg"])
        outer.pack(fill="both", expand=True, padx=20, pady=20)

        head = RoundedCard(outer, height=96)
        head.pack(fill="x", pady=(0, 16))
        tk.Label(head.inner, text="M200F Fingerprint Unlocker", font=FONT_TITLE,
                 bg=C["card"], fg=C["ink"]).pack(anchor="w", padx=8, pady=(12, 4))
        tk.Label(head.inner, text="HIKVISION 安全 U 盘 · 硬件免驱解锁",
                 font=FONT, bg=C["card"], fg=C["sub"]).pack(anchor="w", padx=8)

        card = RoundedCard(outer, height=360)
        card.pack(fill="x", pady=(0, 16))

        self.canvas = tk.Canvas(card.inner, width=200, height=200,
                                bg=C["card"], highlightthickness=0)
        self.canvas.pack(pady=(20, 8))
        self._draw_indicator_ring(C["idle"], C["idle_l"], 0)

        self.status_var = tk.StringVar(value=self._current_status)
        self.status_label = tk.Label(card.inner, textvariable=self.status_var,
                                     font=FONT_STATUS, bg=C["card"], fg=C["ink"])
        self.status_label.pack(pady=(4, 12))

        chips = tk.Frame(card.inner, bg=C["card"])
        chips.pack()
        self.chip_drive = RoundedChip(chips, "盘号: --")
        self.chip_drive.pack(side="left", padx=6)
        self.chip_attempt = RoundedChip(chips, "尝试: 0")
        self.chip_attempt.pack(side="left", padx=6)

        log_card = RoundedCard(outer, bg_color=C["log_bg"])
        log_card.pack(fill="both", expand=True)
        tk.Label(log_card.inner, text="Terminal Log", font=FONT_BADGE,
                 bg=C["log_bg"], fg=C["sub"]).pack(anchor="w", pady=(0, 4))
        self.log = scrolledtext.ScrolledText(
            log_card.inner, state="disabled", font=FONT_MONO, wrap="word",
            bg=C["log_bg"], fg=C["log_fg"], insertbackground=C["log_fg"],
            relief="flat", highlightthickness=0, bd=0)
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("dim", foreground=C["log_dim"])

        btns = tk.Frame(outer, bg=C["bg"])
        btns.pack(fill="x", pady=(16, 0))
        self.quit_btn = RoundedButton(btns, "退出 (Exit)", C["card"], C["err"],
                                      self.on_close, width=120)
        self.quit_btn.pack(side="right")

    def _draw_indicator_ring(self, main, light, glow):
        """Radar ripple + breathing center dot; glow is 0.0..1.0."""
        cv = self.canvas
        cv.delete("all")
        cx = cy = 100
        base_r = 60
        for i in range(4, 0, -1):
            r = base_r + (i * 8 * glow)
            alpha = ((5 - i) / 5.0) * glow
            cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                           outline=self._blend(C["card"], light, alpha), width=2)
        cv.create_oval(cx - base_r, cy - base_r, cx + base_r, cy + base_r,
                       outline=main, width=3)
        center_r = 10 + (6 * glow)
        cv.create_oval(cx - center_r, cy - center_r, cx + center_r, cy + center_r,
                       fill=main, outline=main)

    @staticmethod
    def _blend(c1, c2, t):
        t = max(0.0, min(1.0, t))
        r1, g1, b1 = (int(c1[i:i + 2], 16) for i in (1, 3, 5))
        r2, g2, b2 = (int(c2[i:i + 2], 16) for i in (1, 3, 5))
        return "#%02x%02x%02x" % (int(r1 + (r2 - r1) * t),
                                  int(g1 + (g2 - g1) * t),
                                  int(b1 + (b2 - b1) * t))

    def _pulse(self):
        """Breathing animation loop scheduled via root.after()."""
        if not self.pulse_on:
            return
        self.pulse_phase += 0.12
        glow = (math.sin(self.pulse_phase) + 1) / 2
        main, light = self._current_colors
        self._draw_indicator_ring(main, light, glow)
        self.pulse_job = self.root.after(self.pulse_interval, self._pulse)

    def _start_pulse(self):
        if self.pulse_job:
            self.root.after_cancel(self.pulse_job)
        self.pulse_on = True
        self.pulse_phase = 0.0
        self._pulse()

    def _stop_pulse(self):
        self.pulse_on = False
        if self.pulse_job:
            self.root.after_cancel(self.pulse_job)
            self.pulse_job = None
        main, light = self._current_colors
        self._draw_indicator_ring(main, light, 0.0)

    def _log(self, text, dim=False):
        self.msg_q.put(("log", (text, dim)))

    def _status(self, text):
        self.msg_q.put(("status", text))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "log":
                    text, dim = payload
                    self.log.configure(state="normal")
                    self.log.insert("end", text + "\n", ("dim",) if dim else ())
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "status":
                    self._update_status(payload)
                elif kind == "chip":
                    key, text = payload
                    chip = {"drive": self.chip_drive,
                            "attempt": self.chip_attempt}[key]
                    chip.set_text(text)
                elif kind == "close":
                    self.root.after(0, self._auto_close, payload)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_queue)

    def _update_status(self, text):
        if self._current_status == text:
            return
        self._current_status = text
        self.status_var.set(text)
        main, light = state_color(text)
        self._current_colors = (main, light)
        self.status_label.configure(fg=main)
        if "检测中" in text or "请放上指纹" in text:
            self.pulse_interval = 25 if "检测中" in text else 40
            if not self.pulse_on:
                self._start_pulse()
        else:
            self._stop_pulse()

    def _auto_close(self, seconds):
        self.quit_btn.unbind("<Button-1>")
        if seconds <= 0:
            self._log("10 秒已到，自动退出。")
            self.root.destroy()
            return
        self._log("%d 秒后自动退出..." % seconds, dim=True)
        self.root.after(1000, self._auto_close, seconds - 1)

    def on_close(self):
        self.stop_event.set()
        self._stop_pulse()
        self.root.destroy()

    # ---------------- Background worker thread ----------------
    def _reset_and_wait(self, poll):
        """After a failure: reset with param 00 and wait until the sensor is idle."""
        logged = False
        while not self.stop_event.is_set():
            for _ in range(3):
                try:
                    poll(0x00)
                except OSError:
                    pass
                time.sleep(0.05)
            rr = poll(0x01)
            if rr == IDLE_RESP or rr[:1] == b"\x0e":
                return True
            if rr in STATE_RESPS:
                if not logged:
                    self._log("手指仍按着，请抬起...", dim=True)
                    logged = True
                time.sleep(0.35)
                continue
            if not logged:
                self._log("设备未回到空闲（应答 %s），继续复位..." % hx(rr), dim=True)
                logged = True
            time.sleep(1.0)
        return False

    def _worker_main(self):
        """Detect device -> init -> unlimited fingerprint unlock loop."""
        self._log("正在检测 M200F 设备...", dim=True)
        backend, fw = detect_m200f()
        if backend is None:
            self._status("检测不到设备")
            self._log("未检测到 M200F（VID 21C4 / PID 8381）。")
            self._log(platform_hints())
            self.msg_q.put(("close", 10))
            return

        self.msg_q.put(("chip", ("drive", short_label(backend))))
        self._log("检测到 M200F：%s（动态探测）" % backend.label)
        self._log("固件标识: %s" % fw, dim=True)
        self._log("开始完整初始化...")

        try:
            backend.open()
        except OSError as e:
            self._status("打开设备失败")
            self._log("打开设备失败：%s" % e)
            self.msg_q.put(("close", 10))
            return

        for lun, cdb, length in INIT_CMDS:
            try:
                data = backend.scsi(parse_hex(cdb), length, lun=lun)
                self._log("  init LUN%d: %d B" % (lun, len(data)), dim=True)
            except OSError as e:
                self._log("  init LUN%d %s skipped: %s" % (lun, cdb[:3], e), dim=True)

        def poll(param):
            return backend.scsi(
                bytes([0x90, 0x2F, 0, 0, 0, 0, param]) + b"\x00" * 9, 3)

        for _ in range(3):
            poll(0x00)
            time.sleep(0.05)

        self._log("初始化完成。请把手指放到传感器上（不限时）。")
        attempt = 0
        finger_down = False
        while not self.stop_event.is_set():
            try:
                r = poll(0x01)
            except OSError as e:
                self._status("轮询失败")
                self._log("轮询失败：%s" % e)
                self.msg_q.put(("close", 10))
                return

            if r == IDLE_RESP:
                finger_down = False
                if self._current_status != "请放上指纹":
                    self._status("请放上指纹")
                time.sleep(0.35)
                continue

            if r[:1] == b"\x0e":
                self._status("指纹已验证成功")
                self._log("设备处于已验证状态，加密分区应已解锁。")
                return

            if r[:2] == b"\xff\xfd":
                self._status("失败")
                self._log("识别失败，正在复位...")
                if not self._reset_and_wait(poll):
                    return
                finger_down = False
                self._status("请放上指纹")
                continue

            if r in STATE_RESPS:
                if finger_down:
                    time.sleep(0.35)
                    continue
                finger_down = True
                attempt += 1
                self.msg_q.put(("chip", ("attempt", "尝试: %d" % attempt)))
                self._log("检测到手指，正在识别...")
                self._status("检测中")

                failed = False
                try:
                    poll(0x02)
                    for _cycle in range(6):
                        poll(0x03)
                        deadline = time.time() + 3
                        while time.time() < deadline:
                            rr = poll(0x04)
                            if rr != b"\x04\x05\xff":
                                break
                            time.sleep(0.04)
                        rr = poll(0x05)
                        if rr[:1] == b"\x0e":
                            fid = rr[2] if len(rr) > 2 else 0
                            self._status("成功")
                            vol = find_secure_volume()
                            if vol:
                                self._log(">>> 指纹 %d 成功！已解锁（%s Secure）。"
                                          % (fid, vol))
                            else:
                                self._log(">>> 指纹 %d 成功！加密分区已解锁。" % fid)
                            return
                        if rr[:2] == b"\xff\xfd":
                            failed = True
                            self._log("第 %d 次识别失败。" % attempt)
                            break
                        if rr == b"\x03\x04\xff":
                            continue
                        self._log("未知应答 %s（本轮结束）" % hx(rr))
                        break
                except OSError as e:
                    failed = True
                    self._log("识别流程出错：%s" % e)

                self._status("失败" if failed else "请放上指纹")
                if not self._reset_and_wait(poll):
                    return
                finger_down = False
                self._status("请放上指纹")
                continue

            self._log("轮询应答 %s" % hx(r), dim=True)
            time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser(description="M200F 指纹 U 盘解锁 GUI（跨平台）")
    ap.add_argument("--selftest", action="store_true",
                    help="只检测设备/卷并打印，不开窗口")
    args = ap.parse_args()
    if args.selftest:
        backend, fw = detect_m200f()
        vol = find_secure_volume()
        if backend is None:
            print("未检测到 M200F。%s" % platform_hints())
            return 1
        print("检测到 M200F：%s，固件: %s" % (backend.label, fw))
        print("Secure 卷: %s" % (vol or "(未找到)"))
        return 0
    if not HAVE_TK:
        print("tkinter 不可用：%r" % TK_IMPORT_ERR)
        return 1
    root = tk.Tk()
    FingerGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
