#!/bin/bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
LOG="$ROOT/源码测试日志-v1.9.8.txt"
exec > >(tee "$LOG") 2>&1

finish() {
  echo
  read -r -p "按回车键关闭此窗口……" _
}
trap finish EXIT

if [[ ! -d .venv-build ]]; then
  python3 -m venv .venv-build
fi
source .venv-build/bin/activate
python -m pip install -r requirements.txt
python app.py
