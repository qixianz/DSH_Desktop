@echo off
setlocal EnableDelayedExpansion
rem DeepSeek Harness window exe ONE-CLICK rebuild (PyInstaller)
rem
rem Repo fetch / backend deps / backend build are all handled by the exe
rem at startup (clone / pnpm install / pnpm build live in webview2_launcher.py).
rem This script ONLY packages the window exe:
rem   [1] write the shared paths file (paths.env)
rem   [2] venv: auto-create DSH_Desktop\.venv, auto-install PyInstaller+pywebview
rem   [3] kill any running exe and wait for the file lock to be released
rem   [4] PyInstaller build -> ..\DSH_Desktop.exe
rem
rem No need to run 00_env.bat first: this script is self-sufficient.
rem 00_env.bat stays for a manual full dependency setup.
cd /d "%~dp0"

rem ============ [1] shared paths file: all values relative to ROOT ============
rem Repo is fixed to the default layout deepseek-harness (matches the exe's
rem auto-detect / fallback). If missing, the exe clones it on first launch.
set "ROOT=%~dp0.."
for %%a in ("%~dp0.") do set "BUILD_NAME=%%~nxa"
set "EXE_NAME=DSH_Desktop.exe"
> "%~dp0paths.env" (
  echo ROOT=.
  echo BUILD_DIR=%BUILD_NAME%
  echo WINDOW_DIR=%BUILD_NAME%\window
  echo VENV_DIR=%BUILD_NAME%\.venv
  echo REPO_DIR=deepseek-harness
  echo EXE_NAME=%EXE_NAME%
)
echo Paths file written: %~dp0paths.env

rem ============ [2] venv + Python deps: auto-create / auto-install ============
set "VENV_PY=%ROOT%\%BUILD_NAME%\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo Creating virtual environment: %ROOT%\%BUILD_NAME%\.venv
  python -m venv "%ROOT%\%BUILD_NAME%\.venv"
  if errorlevel 1 (
    echo [FAILED] venv creation failed.
    echo          Install Python 3.10+ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  echo Virtual environment created.
) else (
  echo Virtual environment found: %VENV_PY%
)
"%VENV_PY%" -c "import PyInstaller, webview" >nul 2>&1
if errorlevel 1 (
  if exist "%~dp0requirements.txt" (
    echo Installing Python deps from requirements.txt ...
    "%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
  ) else (
    echo requirements.txt not found, installing latest PyInstaller + pywebview ...
    "%VENV_PY%" -m pip install --upgrade pyinstaller pywebview
  )
  if errorlevel 1 (
    echo [FAILED] Python dependency install failed.
    echo          Check network connection or pip mirror.
    pause
    exit /b 1
  )
) else (
  echo Python deps already installed.
)

rem ============ [3] kill running exe, then wait for the file lock ============
taskkill /IM "%EXE_NAME%" /F >nul 2>&1
set /a WAIT_TICKS=0
:WAIT_EXE_LOOP
tasklist /FI "IMAGENAME eq %EXE_NAME%" 2>nul | find /i "%EXE_NAME%" >nul
if errorlevel 1 goto EXE_DEAD
set /a WAIT_TICKS+=1
if %WAIT_TICKS% GEQ 15 (
  echo [WARN] %EXE_NAME% still running after ~15s; build may fail if the file is locked.
  goto EXE_DEAD
)
ping -n 2 127.0.0.1 >nul
goto WAIT_EXE_LOOP
:EXE_DEAD

rem ============ [4] PyInstaller build ============
echo Building DSH_Desktop window exe ...
"%VENV_PY%" -m PyInstaller --noconfirm --clean --distpath "%ROOT%" --workpath "%~dp0pyinstaller-build" "%ROOT%\%BUILD_NAME%\window\DSH_Desktop.spec"
if errorlevel 1 (
  echo.
  echo [BUILD FAILED] check error output above.
  pause
  exit /b 1
)
rem drop the PyInstaller work dir (intermediate files only, rebuilt on each run)
rmdir /s /q "%~dp0pyinstaller-build" >nul 2>&1
echo.
echo [OK] done: %ROOT%\%EXE_NAME%
endlocal