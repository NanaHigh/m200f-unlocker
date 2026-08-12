# Hikvision M200F Fingerprint USB Drive Unlocker (Reverse-Engineered)

## Why this project exists

The M200F is a fingerprint-encrypted USB flash drive. When plugged in, it
exposes three logical units: a virtual CD-ROM (read-only, containing the
official FingerTool.exe), a public partition, and an encrypted partition
that only unlocks after fingerprint verification.

The problem: the fingerprint sensor's wake-up and verification flow can only
be triggered by the official Windows management software. There is no usable
tool on Linux / macOS, so accessing the encrypted partition required a
Windows machine every time.

This project replays the exact SCSI command sequence the official software
sends to the hardware, so you can unlock your own drive with your own
fingerprint on any platform. It is purely for interoperability with devices
you own — the fingerprint matching itself is still performed by the device
firmware; nothing is bypassed or cracked.

How it was done: USB capture (USBPcap) of the official FingerTool session,
then cross-checked with static analysis (capstone + Unicorn emulation).

> ⚠️ **Verification status**: the protocol and tools have been verified on
> **Windows only**. Linux/macOS binaries can be built, but the SCSI path has
> not been validated on those platforms yet.

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

# Linux / macOS (buildable, not yet validated)
```

Both files are self-contained (no dependency on each other) and use only the
Python standard library; `environment.yml` provides the minimal environment
(Python 3.9 + tk).

## Packaging & release

Local build: `python build.py` (repo root).

## License

This project is open source under the MIT License.
