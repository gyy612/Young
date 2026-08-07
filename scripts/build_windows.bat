@echo off
setlocal
cd /d "%~dp0\.."

set VERSION=1.9.5
echo ========================================
echo ismolar interpreter v%VERSION% - Windows build
echo ========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo Python not found. Install Python 3.12 from https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist .venv-build-win (
  echo [1/4] Creating build venv...
  python -m venv .venv-build-win
)

call .venv-build-win\Scripts\activate.bat

echo [2/4] Installing dependencies...
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency install failed. Check network.
  pause
  exit /b 1
)

echo [3/4] Checking code...
python -m py_compile app.py xfyun_client.py
if errorlevel 1 (
  echo py_compile failed.
  pause
  exit /b 1
)

echo [4/4] Building portable EXE...
python -m PyInstaller --noconfirm --clean ismolar_windows.spec
if errorlevel 1 (
  echo PyInstaller failed.
  pause
  exit /b 1
)

echo.
echo Portable EXE: dist\ismolar-interpreter.exe
echo.

where iscc >nul 2>&1
if errorlevel 1 (
  echo Inno Setup not found - skip installer. Install Inno Setup 6 to build setup.exe.
) else (
  echo Building installer with Inno Setup...
  iscc scripts\installer_windows.iss
)

echo.
echo Done.
pause
