# Hikvision M200F 指纹 U 盘解锁工具（逆向版）

## 为什么做这个逆向

我有一把海康威视 M200F 指纹加密 U 盘。插入电脑后，它会呈现为三个逻辑
单元：只读的虚拟 CD-ROM（存放官方 FingerTool.exe）、无需验证即可访问的
公共分区，以及必须通过指纹验证才能解锁的加密分区。麻烦在于：指纹传感器
的唤醒与验证流程只能由 Windows 下的官方管理软件触发，Linux 和 macOS 上
没有任何可用的替代工具；而加密分区断电即重新锁定，所以这块加密区实际上
只能在 Windows 机器上使用。

这个项目的目标是**复刻官方软件发给硬件的那条指令序列**，从而在任何平台
上都能用自己的指纹解锁自己的 U 盘（纯互操作用途，不涉及任何破解/绕过
授权——指纹比对本身仍由设备固件完成）。

方法：对官方 FingerTool 抓 USB 包（USBPcap）还原协议，并用静态分析
（capstone + Unicorn 模拟）交叉验证。

> ✅ **验证状态**：**Windows** 与 **Linux**（`/dev/sgX` SG_IO 通路，Debian/VMware
> 环境实测）均经过真机验证；**macOS**（pyusb 兜底）已实现但尚未验证。

## 设备信息（真机确认）

| 项目 | 值 |
|---|---|
| VID / PID | 0x21C4 / 0x8381（Longsys 江波龙） |
| 主控 / 固件 | DM8381，CODEV06.46（2018-07-13） |
| 接口 | Mass Storage / SCSI / Bulk-Only（EP OUT 0x02, IN 0x82） |
| LUN 0 | 公共分区兼指纹/加密控制器（所有厂商命令都在此处读写） |
| LUN 1 | 加密分区（指纹验证后由系统挂载为卷标 Secure 的卷） |
| LUN 2 | 虚拟 CD-ROM（1.98MB，存放文档和FingerTool） |

## 确认的逆向结果（指令序列）

所有命令都是 16 字节 CDB，走 LUN 0，方向 IN，SCSI 直通下发：

### 1. 初始化（必须先完整执行，否则传感器停在错误态）

```text
READ10 LUN0 (LBA0,16块)       读 8192B
A1 00 00 00 80 00 ...         读 128B   固件标识 "DM8381 CODEV06.46"
A1 01 00 04 1D 00 ...         读 1053B  配置块（含厂商/型号/串号）
A1 02 00 00 01 00 ...         读 1B     状态（LUN0/LUN2/LUN1 各一次）
A1 00 00 00 02 00 ...         读 512B   扩展块（关键，缺失则 90 2F 报错）
90 29 / 90 2B / 90 26         状态查询
```

### 2. 指纹验证状态机（90 2F，应答 3 字节）

```text
主机发 参数01 -> 应答 01 02 FF   空闲，等待手指
主机发 参数01 -> 应答 02 03 FF   手指已放上
主机发 参数02 -> 应答 03 04 FF   开始采集
主机发 参数03 -> 应答 04 05 FF   采集中
主机发 参数04 -> 应答 05 06 FF   本轮扫描完成
主机发 参数05 -> 应答 03 04 FF   未匹配，自动重扫（最多约 6 轮）
主机发 参数05 -> 应答 0E 00 XX   指纹编号 XX 验证成功！（终态）
主机发 参数05 -> 应答 FF FD FF   识别失败（终态）
```

**关键结论**：`0E 00 XX` 的末字节是匹配到的指纹编号，不是错误码；
验证成功后固件内部直接解锁加密分区，**无需任何额外的"开启"命令**，
Windows 会挂出卷标为 Secure 的盘符（具体盘符由系统分配）。

## 使用

```bash
# Windows（真机验证）
python finger_tool_cli.py                 # 命令行：自动检测 -> 指纹解锁循环
python finger_tool_cli.py --detect        # 只检测设备
python finger_tool_gui.py                 # 图形界面（深色雷达动画版）
python finger_tool_gui.py --selftest      # 检测自检

# Linux（SG_IO 已验证；授权 /dev/sgX 见下方「Linux 快速上手」）
# macOS（pyusb 兜底，未验证；需 brew install libusb）
```

两个文件均为自包含实现（不互相依赖），仅使用 Python 标准库；
`environment.yml` 提供最小运行环境（python 3.9 + tk）。

## Linux 快速上手

```bash
# 1) 一次性授权 /dev/sgX（写 udev 规则 + 加载 sg 模块 + 重载，需 root）
echo 'SUBSYSTEM=="scsi_generic", ATTRS{idVendor}=="21c4", ATTRS{idProduct}=="8381", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-m200f.rules
sudo modprobe sg
sudo udevadm control --reload && sudo udevadm trigger

# 2) 重新插拔 U 盘（虚拟机请保持 USB 透传）

# 3) 验证与解锁（普通用户即可）
python3 finger_tool_cli.py --detect   # 应打印 /dev/sgX 与固件标识
python3 finger_tool_cli.py            # 放手指解锁（0E 00 XX = 成功）
python3 finger_tool_gui.py            # 图形界面
```

若 `lsusb` 看不到 `21c4:8381`，说明虚拟机未把 U 盘透传进来，先解决透传。

## Android

Android 原生版本位于 [`android/`](android/)，版本号与当前发布线保持为
`0.0.2`。它使用 Android USB Host 和 Bulk-Only Transport，界面与 Python
GUI 的状态机一致，需要 Android 8.0+、USB OTG 和可供电的 OTG 转接头。

本地 release 构建使用 `android/NanaHigh.jks`。签名文件和
`android/signing.properties` 已加入忽略列表，不应提交到仓库。GitHub Actions
会在配置 `ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、
`ANDROID_KEY_ALIAS`、`ANDROID_KEY_PASSWORD` 四个 Secrets 后自动构建并发布
签名 APK；未配置时仍会构建 debug APK。

## 打包与发布

本机构建：`python build.py`（仓库根目录）；Android 构建：在 `android/`
目录运行 `gradle assembleDebug` 或 `gradle assembleRelease`；APK 文件名包含
版本号，例如 `m200f-unlocker-0.0.2-release.apk`。

## 免责声明

本项目仅用于在用户自有设备上实现跨平台互操作；指纹模板的录入/验证仍由
设备固件完成，本工具不存储或绕开任何凭据。

## 开源许可

本项目以 MIT 许可证开源。
