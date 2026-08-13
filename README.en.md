# Hikvision M200F Fingerprint USB Drive Unlocker (Reverse-Engineered)

## Why this project exists

I own a Hikvision M200F fingerprint-encrypted USB drive. When plugged in, it
presents three logical units: a read-only virtual CD-ROM that holds the
official FingerTool.exe, a public partition accessible without
authentication, and an encrypted partition that only unlocks after
fingerprint verification. The catch is that the sensor's wake-up and
verification flow can only be triggered by the official Windows software —
there is no usable alternative on Linux or macOS, and the encrypted
partition re-locks on every power loss, so in practice it can only be used
on a Windows machine.

This project replays the exact SCSI command sequence the official software
sends to the hardware, so you can unlock your own drive with your own
fingerprint on any platform. It is purely for interoperability with devices
you own — the fingerprint matching itself is still performed by the device
firmware; nothing is bypassed or cracked.

How it was done: USB capture (USBPcap) of the official FingerTool session,
then cross-checked with static analysis (capstone + Unicorn emulation).

> ⚠️ **Verification status**: the protocol and tools have been verified on
> **Windows and Linux** (SG_IO via /dev/sgX, tested on a Debian/VMware setup)
> are verified; **macOS** (pyusb fallback) is implemented but not yet validated.

## Device facts (confirmed on hardware)

| Item | Value |
|---|---|
| VID / PID | 0x21C4 / 0x8381 (Longsys) |
| Controller / firmware | DM8381, CODEV06.46 (2018-07-13) |
| Interface | Mass Storage / SCSI / Bulk-Only (EP OUT 0x02, IN 0x82) |
| LUN 0 | Fingerprint / security controller (all vendor commands go here) |
| LUN 1 | Data partition (mounted by the OS as a volume labeled "Secure" after verification) |
| LUN 2 | Virtual CD-ROM (2048-byte sectors, hosts FingerTool) |

## Confirmed reverse-engineered protocol

All commands are 16-byte CDBs sent via SCSI pass-through to LUN 0, direction IN.

### 1. Initialization (must run completely, otherwise the sensor stays in an error state)

```text
READ10 LUN0 (LBA0, 16 blocks)    read 8192 B
A1 00 00 00 80 00 ...            read 128 B   firmware ID "DM8381 CODEV06.46"
A1 01 00 04 1D 00 ...            read 1053 B  config block (vendor/model/serial)
A1 02 00 00 01 00 ...            read 1 B     status (LUN0/LUN2/LUN1 once each)
A1 00 00 00 02 00 ...            read 512 B   extended block (critical; if skipped, 90 2F returns an error)
90 29 / 90 2B / 90 26            status queries
```

### 2. Fingerprint verification state machine (90 2F, 3-byte response)

```text
host sends param 01 -> 01 02 FF    idle, waiting for a finger
host sends param 01 -> 02 03 FF    finger detected
host sends param 02 -> 03 04 FF    capture started
host sends param 03 -> 04 05 FF    capturing
host sends param 04 -> 05 06 FF    scan cycle complete
host sends param 05 -> 03 04 FF    no match, auto re-scan (up to ~6 cycles)
host sends param 05 -> 0E 00 XX    fingerprint XX verified! (terminal success)
host sends param 05 -> FF FD FF    verification failed (terminal)
```

**Key finding**: the last byte of `0E 00 XX` is the matched fingerprint ID,
not an error code. After a successful match the firmware unlocks the
encrypted partition internally — **no extra "open" command is needed**. The
OS mounts the volume labeled "Secure" (drive letter assigned by the system).

## Usage

```bash
# Windows (verified on hardware)
python finger_tool_cli.py                 # CLI: auto-detect -> unlock loop
python finger_tool_cli.py --detect        # detection only
python finger_tool_gui.py                 # GUI (dark radar-animation theme)
python finger_tool_gui.py --selftest      # GUI self-test (no window)

# Linux (SG_IO verified; grant /dev/sgX access — see "Linux quick start")
# macOS (pyusb fallback, unverified; needs: brew install libusb)
```

Both files are self-contained (no dependency on each other) and use only the
Python standard library; `environment.yml` provides the minimal environment
(Python 3.9 + tk).

## Linux quick start

```bash
# 1) One-time setup: udev rule for /dev/sgX + load sg module (needs root)
echo 'SUBSYSTEM=="scsi_generic", ATTRS{idVendor}=="21c4", ATTRS{idProduct}=="8381", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-m200f.rules
sudo modprobe sg
sudo udevadm control --reload && sudo udevadm trigger

# 2) Re-plug the drive (in a VM, keep USB passthrough enabled)

# 3) Verify and unlock (no sudo needed afterwards)
python3 finger_tool_cli.py --detect   # should print /dev/sgX and firmware ID
python3 finger_tool_cli.py            # place finger; 0E 00 XX = success
python3 finger_tool_gui.py            # GUI
```

If `lsusb` does not show `21c4:8381`, the VM has not passed the USB device
through — fix the passthrough first.

## Packaging & release

Local build: `python build.py` (repo root).

## License

This project is open source under the MIT License.
