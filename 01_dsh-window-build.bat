@echo off
rem DeepSeek Harness window exe rebuild (PyInstaller)
rem spec + launcher live in window\; output: ..\Deepseek Harness.exe
rem Uses the venv created by 00_env.bat (Build\.venv); run 00_env.bat first.
cd /d "%~dp0"

rem Auto-clone the backend repo when missing (fresh machine bootstrap).
rem Any folder in the root dir holding package.json + .git counts as the
rem repo (e.g. Source or deepseek-harness); if none is found, clones with
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
) else (
  echo Repository found: %REPO_FOUND%
)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo [FAILED] venv not found: %VENV_PY%
  echo          Run 00_env.bat first - it creates Build\.venv and installs
  echo          PyInstaller and pywebview into it.
  pause
  exit /b 1
)

rem kill running exe first (file lock would fail the build)
taskkill /IM "Deepseek Harness.exe" /F >nul 2>&1

echo Building DeepSeek Harness window exe ...
"%VENV_PY%" -m PyInstaller --noconfirm --clean --distpath ".." --workpath "pyinstaller-build" "window\Deepseek Harness.spec"
if errorlevel 1 (
  echo.
  echo [BUILD FAILED] check error output above.
  pause
  exit /b 1
)
rem drop the PyInstaller work dir (intermediate files only, rebuilt on each run)
rmdir /s /q "pyinstaller-build" >nul 2>&1
echo.
echo [OK] done: ..\Deepseek Harness.exe

