# -*- mode: python ; coding: utf-8 -*-
# Windows 绿色版（单文件 EXE）打包配置。
# 必须在 Windows 上运行 PyInstaller；macOS 无法交叉编译 EXE。

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=collect_dynamic_libs("sounddevice")
    + collect_dynamic_libs("azure.cognitiveservices.speech"),
    datas=[("assets", "assets")]
    + collect_data_files("azure.cognitiveservices.speech"),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ismolar-interpreter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/app_icon.ico",
)
