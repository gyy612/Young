#!/bin/bash
set -Eeuo pipefail
echo "正在重置麦克风权限……"
tccutil reset Microphone com.ismolar.interpreter || true
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone" || true
echo
echo "完成。重新打开 App 时请选择“允许”。"
read -r -p "按回车键关闭此窗口……" _
