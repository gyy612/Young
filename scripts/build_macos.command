#!/bin/bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="1.9.9"
APP_ASCII="ismolar-interpreter"
APP_DISPLAY="ísmolar 同声传译"
APP_PATH="$ROOT/dist/${APP_ASCII}.app"
DMG_PATH="$ROOT/dist/ismolar-interpreter-macOS-local-v${VERSION}.dmg"
LOG_PATH="$ROOT/本地打包日志-v1.9.9.txt"

exec > >(tee "$LOG_PATH") 2>&1

pause_on_exit() {
  echo
  read -r -p "按回车键关闭此窗口……" _
}

on_error() {
  code=$?
  echo
  echo "========================================"
  echo "制作失败，错误代码：$code"
  echo "请把“本地打包日志-v1.9.9.txt”发给我。"
  echo "========================================"
  pause_on_exit
  exit "$code"
}
trap on_error ERR

clear
echo "========================================"
echo "ísmolar 同声传译 v${VERSION}"
echo "Mac 本地一键制作 DMG · 签名修复版"
echo "========================================"
echo

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "这个脚本只能在 macOS 上运行。"
  exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "当前脚本用于 Apple Silicon Mac（M1/M2/M3/M4）。"
  echo "检测到架构：$(uname -m)"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有找到 Python 3。"
  exit 1
fi

echo "Mac 架构：$(uname -m)"
echo "Python：$(python3 --version)"
echo

VENV="$ROOT/.venv-build"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "第 1 步：创建本地打包环境……"
  python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

echo "第 2 步：安装或检查依赖……"
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt

echo "第 3 步：检查代码和架构……"
python -m py_compile app.py xfyun_client.py
python - <<'PY'
import platform
import PySide6
import sounddevice

print("Python 架构：", platform.machine())
print("PySide6：", PySide6.__version__)
print("sounddevice：", sounddevice.__version__)
if platform.machine() != "arm64":
    raise SystemExit("Python 不是 arm64 架构")
PY

echo "第 4 步：生成英文路径 App……"
rm -rf build dist dmg_stage
python -m PyInstaller --noconfirm --clean ismolar_local.spec

if [[ ! -d "$APP_PATH" ]]; then
  echo "没有找到生成的 App：$APP_PATH"
  exit 1
fi

echo "第 5 步：验证内置版本和麦克风权限……"
PLIST="$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Print :CFBundleDisplayName" "$PLIST"
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$PLIST"
/usr/libexec/PlistBuddy -c "Print :NSMicrophoneUsageDescription" "$PLIST"

echo "第 6 步：清理扩展属性……"
xattr -cr "$APP_PATH" || true

echo "第 7 步：只签名 App 外层……"
# PyInstaller 已分别处理内部二进制。这里不要使用 --deep，
# 避免 macOS 26 在中文/复杂 Bundle 上触发 Bus error。
codesign --force --sign - --timestamp=none "$APP_PATH"

echo "第 8 步：严格验证签名……"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
spctl --assess --type execute --verbose=4 "$APP_PATH" || true

echo "第 9 步：制作 DMG……"
mkdir -p dmg_stage

# App 本体保持英文文件名以确保签名稳定；
# Finder 中会依据 CFBundleDisplayName 显示中文名称。
ditto "$APP_PATH" "dmg_stage/${APP_ASCII}.app"
ln -s /Applications "dmg_stage/Applications"

hdiutil create \
  -volname "${APP_DISPLAY} ${VERSION}" \
  -srcfolder dmg_stage \
  -ov \
  -format UDZO \
  "$DMG_PATH"

rm -rf dmg_stage

echo
echo "========================================"
echo "制作成功！"
echo "DMG：$DMG_PATH"
echo "========================================"
echo
echo "安装后第一次打开：右键 App → 打开。"
open -R "$DMG_PATH"
pause_on_exit
