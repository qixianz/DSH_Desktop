@echo off
rem DeepSeek Harness window exe rebuild (PyInstaller)
rem spec + launcher live in window\; output: ..\Deepseek Harness.exe
rem Uses the venv created by 00_env.bat (Build\.venv); run 00_env.bat first.
cd /d "%~dp0"

rem Auto-clone the backend repo when missing (fresh machine bootstrap).
rem Default location: <Build parent>\Source. Shallow clone (--depth 1) is
rem enough for building; drop the flag if full history is needed.
set "REPO_DEFAULT=%~dp0..\Source"
if not exist "%REPO_DEFAULT%\.git" (
  if not exist "%REPO_DEFAULT%" (
    echo Repository not found, cloning deepseek-harness ...
    git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git "%REPO_DEFAULT%"
    if errorlevel 1 (
      echo [FAILED] git clone failed.
      pause
      exit /b 1
    )
  ) else (
    echo [FAILED] %REPO_DEFAULT% exists but is not a git repository.
    pause
    exit /b 1
  )
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

