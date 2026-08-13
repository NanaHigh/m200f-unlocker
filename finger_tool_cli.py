#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finger_tool_cli.py —— M200F 指纹 U 盘解锁（命令行版，自包含，跨平台后端）
================================================================

自包含实现，按平台自动选择 SCSI 后端：
  Windows   : ScsiDevice（IOCTL_SCSI_PASS_THROUGH, \\.\PHYSICALDRIVE%d）
  Linux     : SgBackend（/dev/sgX 的 SG_IO ioctl，纯 ctypes，零依赖）
  macOS/其它: UsbBulkBackend（pyusb 直接组 CBW/CSW，需 libusb）

用法：
    python finger_tool_cli.py               # 自动检测设备并进入指纹解锁循环
    python finger_tool_cli.py --detect      # 只检测设备并打印盘号/固件/Secure 卷
    python finger_tool_cli.py --scsi <hex> [datalen]   # 手动发一条 SCSI 命令

说明：协议在 Windows 与 Linux（SG_IO）上经过真机验证；
      macOS 后端（pyusb）已实现但尚未真机验证。

English summary:
  Self-contained CLI unlocker for the Hikvision M200F fingerprint USB drive.
  Auto-selects the SCSI backend per platform: Windows DeviceIoControl,
  Linux /dev/sgX SG_IO (pure ctypes), macOS/others pyusb CBW/CSW.
  Verified on Windows and Linux (SG_IO); macOS backend (pyusb) implemented
  but not yet validated.
"""

import argparse
import ctypes
import glob
import os
import re
import struct
import sys
import time
from ctypes import wintypes as wt

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
    """Bytes -> 'AB CD EF' hex string."""
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
    """Linux / macOS: send SCSI commands through /dev/sgX with the SG_IO ioctl.

    Equivalent to what sg_raw does under the hood. Requires access to the sg
    node (udev rule or root). The `lun` argument is accepted for interface
    compatibility but is not selectable via SG_IO (node is per-device).
    """

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
    """All platforms: build CBW/CSW directly over libusb (pyusb).

    Requires libusb to be available; on Linux the device must be released from
    usb-storage first (sudo modprobe -r usb-storage) or via a udev rule.
    """

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
    """Enumerate physical drives and return USB mass-storage index list (Windows)."""
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
    """Find the drive letter whose volume label is "Secure" (Windows only)."""
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


last_detect_diag = []   # 最近一次探测的逐节点诊断（失败时给用户看）


def detect_m200f():
    """Auto-select backend per platform and return (backend, firmware).
    On failure, last_detect_diag holds per-node diagnostics."""
    global last_detect_diag
    last_detect_diag = []

    def probe(b):
        try:
            b.open()
        except OSError as e:
            last_detect_diag.append("%s: 打开失败 %s" % (b.label, e))
            return None
        try:
            data = b.scsi(parse_hex(A1_00_FW_CDB), 128, timeout=5)
            if FW_MARKER in data:
                return "".join(chr(x) if 32 <= x < 127 else "" for x in data[:64]).strip()
            last_detect_diag.append("%s: A1 00 应答无 DM8381（%s）"
                                    % (b.label, hx(data[:16])))
        except OSError as e:
            last_detect_diag.append("%s: A1 00 失败 %s" % (b.label, e))
        finally:
            b.close()
        return None

    if os.name == "nt":
        for idx in enumerate_usb_drives():
            fw = probe(ScsiDevice(idx))
            if fw:
                return ScsiDevice(idx), fw
    if sys.platform.startswith("linux"):
        # 优先 /dev/sgX；多 LUN 时 sg 节点可能只映射 CD-ROM，故再扫 /dev/sd[a-z]
        for p in sorted(glob.glob("/dev/sg*")) + sorted(glob.glob("/dev/sd[a-z]")):
            fw = probe(SgBackend(p))
            if fw:
                return SgBackend(p), fw
    try:
        fw = probe(UsbBulkBackend())
        if fw:
            return UsbBulkBackend(), fw
    except Exception as e:  # noqa: BLE001
        last_detect_diag.append("pyusb: %s" % e)
    return None, None


# ===========================================================================
# Unlock protocol (confirmed on hardware)
# ===========================================================================
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

IDLE_RESP = b"\x01\x02\xff"
STATE_RESPS = (b"\x02\x03\xff", b"\x03\x04\xff", b"\x04\x05\xff", b"\x05\x06\xff")


def wake_unlock(backend, log=print):
    """Full init + unlimited fingerprint unlock loop.
    Returns 0=success / 1=failure / 2=aborted."""
    for lun, cdb, length in INIT_CMDS:
        try:
            backend.scsi(parse_hex(cdb), length, lun=lun)
            log("  init LUN%d %s" % (lun, cdb[:3]), flush=True)
        except OSError as e:
            log("  init LUN%d %s skipped: %s" % (lun, cdb[:3], e), flush=True)

    def poll(param):
        return backend.scsi(bytes([0x90, 0x2F, 0, 0, 0, 0, param]) + b"\x00" * 9, 3)

    for _ in range(3):
        poll(0x00)
        time.sleep(0.05)
    log("初始化完成，请把手指放到传感器上（不限时，Ctrl+C 退出）。", flush=True)

    def reset_and_wait():
        while True:
            for _ in range(3):
                poll(0x00)
                time.sleep(0.05)
            rr = poll(0x01)
            if rr == IDLE_RESP or rr[:1] == b"\x0e":
                return
            time.sleep(0.35)

    attempt = 0
    finger_down = False
    while True:
        r = poll(0x01)
        if r == IDLE_RESP:
            finger_down = False
            time.sleep(0.35)
            continue
        if r[:1] == b"\x0e":
            log("设备已处于验证成功状态，加密分区应已解锁。", flush=True)
            return 0
        if r[:2] == b"\xff\xfd":
            log("识别失败，复位后继续等待...", flush=True)
            reset_and_wait()
            finger_down = False
            continue
        if r in STATE_RESPS:
            if finger_down:
                time.sleep(0.35)
                continue
            finger_down = True
            attempt += 1
            log("检测到手指（第 %d 次），正在识别..." % attempt, flush=True)
            try:
                poll(0x02)
                for _ in range(6):
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
                        vol = find_secure_volume()
                        log(">>> 指纹 %d 验证成功！加密分区已解锁%s。"
                            % (fid, "（%s Secure）" % vol if vol else ""), flush=True)
                        return 0
                    if rr[:2] == b"\xff\xfd":
                        log("第 %d 次识别失败。" % attempt, flush=True)
                        break
                    if rr == b"\x03\x04\xff":
                        continue
                    log("未知应答 %s（本轮结束）" % hx(rr), flush=True)
                    break
            except OSError as e:
                log("识别流程出错：%s" % e, flush=True)
            reset_and_wait()
            finger_down = False
            continue
        time.sleep(0.3)


def platform_hints():
    if os.name == "nt":
        return "请确认 U 盘已插入且未被其他程序独占（如 FingerTool.exe）。"
    if sys.platform.startswith("linux"):
        return ("Linux：确认 /dev/sgX 存在且有权限（udev 规则或 sudo）；"
                "如无 /dev/sgX，先 sudo modprobe sg 再重插；"
                "USB 兜底需先 sudo modprobe -r usb-storage。")
    if sys.platform == "darwin":
        return "macOS：请先安装 libusb（brew install libusb）并确认设备未被占用。"
    return "当前平台暂不支持。"


def main():
    ap = argparse.ArgumentParser(description="M200F 指纹 U 盘解锁 CLI（跨平台）")
    ap.add_argument("--detect", action="store_true", help="只检测设备并退出")
    ap.add_argument("--scsi", nargs="+", metavar="HEX",
                    help="手动发送一条 SCSI 命令（可附加末尾 datalen）")
    args = ap.parse_args()

    backend, fw = detect_m200f()
    if backend is None:
        print("未检测到 M200F。%s" % platform_hints())
        for line in last_detect_diag:
            print("  " + line)
        return 1
    print("检测到 M200F：%s" % backend.label)
    print("固件: %s" % fw)
    if args.detect:
        print("Secure 卷: %s" % (find_secure_volume() or "(未找到)"))
        return 0

    if args.scsi:
        parts = args.scsi
        try:
            datalen = int(parts[-1]) if parts[-1].isdigit() else 0
            if datalen:
                parts = parts[:-1]
            cdb = parse_hex("".join(parts))
        except ValueError as e:
            print("CDB 解析失败：%s" % e)
            return 1
        try:
            data = backend.scsi(cdb, datalen)
            print("应答 %d B: %s" % (len(data), hx(data)))
        except OSError as e:
            print("SCSI 失败：%s" % e)
            return 1
        return 0

    try:
        backend.open()
    except OSError as e:
        print("打开设备失败：%s" % e)
        return 1
    try:
        return wake_unlock(backend)
    except KeyboardInterrupt:
        print("\n已中止。")
        return 2
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
