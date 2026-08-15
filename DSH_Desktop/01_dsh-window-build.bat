@echo off
rem DeepSeek Harness window exe rebuild (PyInstaller)
rem spec + launcher live in window\; output: ..\Deepseek Harness.exe
rem Uses the venv created by 00_env.bat (DSH_Desktop\.venv); run 00_env.bat first.
cd /d "%~dp0"

rem Auto-clone the backend repo when missing (fresh machine bootstrap).
rem Any folder in the root dir holding package.json + .git counts as the
rem repo (e.g. deepseek-harness); if none is found, clones with
rem the default name (deepseek-harness) into the root dir.
set "ROOT=%~dp0.."
set "REPO_FOUND="
for /d %%d in ("%ROOT%\*") do (
  if exist "%%d\package.json" if exist "%%d\.git" set "REPO_FOUND=%%d"
)
if not defined REPO_FOUND (
  echo Repository not found, cloning deepseek-harness into %ROOT% ...
  pushd "%ROOT%"
  git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git
  set "CLONE_OK=%errorlevel%"
  popd
  if not "%CLONE_OK%"=="0" (
    echo [FAILED] git clone failed.
    pause
    exit /b 1
  )
  set "REPO_NAME=deepseek-harness"
) else (
  echo Repository found: %REPO_FOUND%
)

rem === shared paths file: all values relative to ROOT (%ROOT%) ===
rem Written here, read by 00_env.bat / resolve_repo.py / webview2_launcher.py.
for %%a in ("%~dp0.") do set "BUILD_NAME=%%~nxa"
if defined REPO_FOUND for %%a in ("%REPO_FOUND%") do set "REPO_NAME=%%~nxa"
> "%~dp0paths.env" (
  echo ROOT=.
  echo BUILD_DIR=%BUILD_NAME%
  echo WINDOW_DIR=%BUILD_NAME%\window
  echo VENV_DIR=%BUILD_NAME%\.venv
  echo REPO_DIR=%REPO_NAME%
  echo EXE_NAME=Deepseek Harness.exe
)
rem read back (skip ROOT= so the absolute %ROOT% stays intact)
for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0paths.env") do (
  if /i "%%a"=="WINDOW_DIR" set "WINDOW_REL=%%b"
  if /i "%%a"=="VENV_DIR" set "VENV_REL=%%b"
  if /i "%%a"=="REPO_DIR" set "REPO_NAME=%%b"
  if /i "%%a"=="EXE_NAME" set "EXE_NAME=%%b"
)
echo Paths file written: %~dp0paths.env

set "VENV_PY=%ROOT%\%VENV_REL%\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo [FAILED] venv not found: %VENV_PY%
  echo          Run 00_env.bat first - it creates %VENV_REL% and installs
  echo          PyInstaller and pywebview into it.
  pause
  exit /b 1
)

rem kill running exe first (file lock would fail the build)
taskkill /IM "%EXE_NAME%" /F >nul 2>&1

echo Building DeepSeek Harness window exe ...
"%VENV_PY%" -m PyInstaller --noconfirm --clean --distpath "%ROOT%" --workpath "%~dp0pyinstaller-build" "%ROOT%\%WINDOW_REL%\Deepseek Harness.spec"
if errorlevel 1 (
  echo.
  echo [BUILD FAILED] check error output above.
  pause
  exit /b 1
)
rem drop the PyInstaller work dir (intermediate files only, rebuilt on each run)
rmdir /s /q "%~dp0pyinstaller-build" >nul 2>&1
echo.
echo [OK] done: %ROOT%%EXE_NAME%

