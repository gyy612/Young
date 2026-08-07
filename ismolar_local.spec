# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_dynamic_libs

block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=collect_dynamic_libs("sounddevice"),
    datas=[("assets", "assets")],
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
    [],
    exclude_binaries=True,
    name="ismolar-interpreter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ismolar-interpreter",
)

app = BUNDLE(
    coll,
    name="ismolar-interpreter.app",
    icon="assets/app_icon.icns",
    bundle_identifier="com.ismolar.interpreter",
    info_plist={
        "CFBundleDisplayName": "ísmolar 同声传译",
        "CFBundleName": "ísmolar 同声传译",
        "CFBundleShortVersionString": "1.9.6",
        "CFBundleVersion": "1.9.6",
        "NSMicrophoneUsageDescription": "ísmolar 同声传译需要使用麦克风进行实时语音识别和翻译。",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
