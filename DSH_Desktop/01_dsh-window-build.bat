@echo off
setlocal EnableDelayedExpansion
rem DeepSeek Harness window exe ONE-CLICK rebuild (PyInstaller)
rem
rem Double-click this file ONCE and it produces ..\DSH_Desktop.exe.
rem Every prerequisite is handled automatically in this single run:
rem   [1] backend repo: auto-detect, or git clone deepseek-harness when missing
rem   [2] write the shared paths file (paths.env)
rem   [3] venv: auto-create DSH_Desktop\.venv, auto-install PyInstaller+pywebview
rem   [4] backend deps: pnpm install only when node_modules is missing (fresh clone)
rem   [5] kill any running exe and wait for the file lock to be released
rem   [6] PyInstaller build -> ..\DSH_Desktop.exe
rem
rem No need to run 00_env.bat first any more: this script is self-sufficient.
rem 00_env.bat stays for a manual full dependency setup.
cd /d "%~dp0"

rem ============ [1] backend repo: auto-detect, or clone when missing ============
rem Any folder in the root dir holding package.json + .git counts as the
rem repo (e.g. deepseek-harness); if none is found, clones with the
rem default name (deepseek-harness) into the root dir.
set "ROOT=%~dp0.."
set "REPO_FOUND="
for /d %%d in ("%ROOT%\*") do (
  if exist "%%d\package.json" if exist "%%d\.git" set "REPO_FOUND=%%d"
)
if defined REPO_FOUND (
  echo Repository found: %REPO_FOUND%
  for %%a in ("%REPO_FOUND%") do set "REPO_NAME=%%~nxa"
) else (
  echo Repository not found, cloning deepseek-harness into %ROOT% ...
  pushd "%ROOT%"
  git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git
  set "CLONE_OK=!errorlevel!"
  popd
  if not "!CLONE_OK!"=="0" (
    echo [FAILED] git clone failed.
    pause
    exit /b 1
  )
  set "REPO_NAME=deepseek-harness"
)

rem ============ [2] shared paths file: all values relative to ROOT ============
rem Written here, read by 00_env.bat / resolve_repo.py / webview2_launcher.py.
for %%a in ("%~dp0.") do set "BUILD_NAME=%%~nxa"
> "%~dp0paths.env" (
  echo ROOT=.
  echo BUILD_DIR=%BUILD_NAME%
  echo WINDOW_DIR=%BUILD_NAME%\window
  echo VENV_DIR=%BUILD_NAME%\.venv
  echo REPO_DIR=%REPO_NAME%
  echo EXE_NAME=DSH_Desktop.exe
)
rem read back (skip ROOT= so the absolute %ROOT% stays intact)
for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0paths.env") do (
  if /i "%%a"=="WINDOW_DIR" set "WINDOW_REL=%%b"
  if /i "%%a"=="VENV_DIR" set "VENV_REL=%%b"
  if /i "%%a"=="REPO_DIR" set "REPO_NAME=%%b"
  if /i "%%a"=="EXE_NAME" set "EXE_NAME=%%b"
)
echo Paths file written: %~dp0paths.env

rem ============ [3] venv + Python deps: auto-create / auto-install ============
set "VENV_PY=%ROOT%\%VENV_REL%\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo Creating virtual environment: %ROOT%\%VENV_REL%
  python -m venv "%ROOT%\%VENV_REL%"
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

rem ============ [4] backend deps: only when node_modules is missing ============
set "REPO_FULL=%ROOT%\%REPO_NAME%"
if not exist "%REPO_FULL%\node_modules" (
  where pnpm >nul 2>&1
  if errorlevel 1 call corepack enable pnpm >nul 2>&1
  where pnpm >nul 2>&1
  if errorlevel 1 (
    echo [WARN] pnpm unavailable; skipping backend install, exe build continues.
  ) else (
    echo Installing backend dependencies via pnpm ...
    pushd "%REPO_FULL%"
    call pnpm install
    set "PNPM_OK=!errorlevel!"
    popd
    if not "!PNPM_OK!"=="0" (
      echo [WARN] pnpm install failed; exe build continues.
    )
  )
) else (
  echo Backend deps already present.
)

rem ============ [5] kill running exe, then wait for the file lock ============
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

rem ============ [6] PyInstaller build ============
echo Building DSH_Desktop window exe ...
"%VENV_PY%" -m PyInstaller --noconfirm --clean --distpath "%ROOT%" --workpath "%~dp0pyinstaller-build" "%ROOT%\%WINDOW_REL%\DSH_Desktop.spec"
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
