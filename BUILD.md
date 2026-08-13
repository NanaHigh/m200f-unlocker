# 打包指南 / Build Guide

## 本机构建（当前平台）/ Local build (current OS)

```bash
pip install pyinstaller

python build.py              # onedir（推荐）
python build.py --onefile    # 单文件版
```

产物：`dist/finger_tool_gui/`（或 `.exe`/`.app`）、`dist/finger_tool_cli/`。

## 跨平台构建 / Cross-platform builds

PyInstaller 不能交叉编译，请用 `.github/workflows/build.yml`：
推送 `v*` tag 后自动构建 Windows / Linux / macOS x64 并上传到 GitHub Release。

## 平台注意 / Platform notes

- **Windows**：主通路 SCSI 直通，无需额外驱动（已验证）。
- **Linux**：需要 `/dev/sgX` 权限（udev 规则或 sudo）；通路未验证。
- **macOS**：未签名应用首次需右键打开或 `xattr -dr com.apple.quarantine`。
- 杀软误报：onedir 误报率低于 onefile；正式分发建议代码签名。
