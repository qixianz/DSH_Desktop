@echo off
setlocal
rem DeepSeek Harness window launcher - TEST RUN (no packaging)
rem
rem Runs the window launcher directly from source with the venv Python
rem (no PyInstaller build needed). Unlike the packaged exe, this test
rem instance runs ALONGSIDE an already-open DSH:
rem   [1] DSH_SINGLE_INSTANCE=<test mutex>  -> bypasses single-instance guard
rem       (the launcher would otherwise exit when DSH is already running)
rem   [2] DSH_PORT=<3081>                   -> separate backend port, so the
rem       test instance never kills/reuses the real DSH backend (port 3080)
rem   [3] DSH_DEMO_UPDATE=1 (uncomment)     -> demo the "new update" badge
rem       without touching the network
rem
rem If the venv is missing/unusable, run 00_env.bat first (one-click setup).
cd /d "%~dp0"

rem ============ [1] venv check ============
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo [FAILED] venv not found: %VENV_PY%
  echo          Run 00_env.bat once to create it and install dependencies.
  pause
  exit /b 1
)
"%VENV_PY%" -c "import clr, webview" >nul 2>&1
if errorlevel 1 (
  echo [FAILED] venv is missing pythonnet/webview.
  echo          Run 00_env.bat once to install dependencies.
  pause
  exit /b 1
)
echo Python deps OK.

rem ============ [2] test-mode isolation ============
rem Single instance: give this test its own mutex name so it does NOT
rem collide with an already-running DSH (which owns the default mutex).
set "DSH_SINGLE_INSTANCE=Local\DSH_Desktop_TestInstance"
rem Backend port: use a separate port so this instance never mistakes the
rem real DSH backend for a stale process and kills it.
set "DSH_PORT=3081"
rem Optional: simulate an available update (badge demo, no network).
rem set "DSH_DEMO_UPDATE=1"

rem ============ [3] run the launcher ============
echo Starting DSH TEST window (mutex: %DSH_SINGLE_INSTANCE%, port: %DSH_PORT%) ...
echo NOTE: the real DSH keeps running untouched.
cd /d "%~dp0window"
"%VENV_PY%" webview2_launcher.py
set "RC=%ERRORLEVEL%"
cd /d "%~dp0"
if not "%RC%"=="0" if not "%RC%"=="1" (
  echo.
  echo [EXITED] launcher returned code %RC%
  pause
)
endlocal