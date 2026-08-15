@echo off
rem DeepSeek Harness Build: one-click dependency check / install
rem Usage: double-click this file, or run it from a command line.
rem
rem Pure-ASCII file: works under any code page (GBK / UTF-8 / ...).
rem Designed for portability: on any machine that has Python + Node.js,
rem run this once to:
rem   [1] check Python
rem   [2] create/reuse a venv at Build\.venv, then check / install
rem       PyInstaller + pywebview inside the venv (needed by the exe)
rem   [3] check OpenSSL DLLs (required for building the exe)
rem   [4] set up the backend (Node): resolve repo via project-config.json,
rem       ensure pnpm, then run "pnpm install" (build happens automatically
rem       on exe start when the backend source changed)
rem After that, run dsh-window-build.bat to build Deepseek Harness.exe
cd /d "%~dp0"

echo ============================================================
echo  [1/4] Checking Python
echo ============================================================
where python >nul 2>&1
if errorlevel 1 (
  echo [FAILED] python not found on PATH.
  echo          Install Python 3.10+ first, and tick
  echo          "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)
python --version
if errorlevel 1 (
  echo [FAILED] python cannot run.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  [2/4] Creating venv + checking/installing PyInstaller+pywebview
echo ============================================================
rem The venv is fixed at Build\.venv, isolated from the global Python
rem (does not pollute the system pip). All pip installs below use it.
set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo Creating virtual environment: %VENV_DIR%
  python -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [FAILED] venv creation failed.
    echo          Make sure Python 3.10+ includes the venv module.
    pause
    exit /b 1
  )
  echo Virtual environment created.
) else (
  echo Virtual environment already exists: %VENV_DIR%
)
"%VENV_PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo PyInstaller not found in venv, installing via pip ...
  "%VENV_PY%" -m pip install --upgrade pyinstaller
  if errorlevel 1 (
    echo [FAILED] PyInstaller install failed.
    echo          Check network connection or pip mirror.
    pause
    exit /b 1
  )
) else (
  echo PyInstaller already installed:
  "%VENV_PY%" -m PyInstaller --version
)
"%VENV_PY%" -c "import webview" >nul 2>&1
if errorlevel 1 (
  echo pywebview not found in venv, installing via pip ...
  "%VENV_PY%" -m pip install pywebview
  if errorlevel 1 (
    echo [FAILED] pywebview install failed.
    echo          Check network connection or pip mirror.
    pause
    exit /b 1
  )
) else (
  echo pywebview already installed.
)

echo.
echo ============================================================
echo  [3/4] Checking OpenSSL DLLs (required for building)
echo ============================================================
"%VENV_PY%" -c "import ssl; print('OpenSSL:', ssl.OPENSSL_VERSION)"
"%VENV_PY%" -c "import os,sys,glob; d=os.path.join(sys.base_prefix,'DLLs'); f=sorted(glob.glob(os.path.join(d,'libssl-*.dll'))+glob.glob(os.path.join(d,'libcrypto-*.dll'))); print('\n'.join(f) if f else 'NOT FOUND in %%s' %% d)"
if errorlevel 1 (
  echo [FAILED] No OpenSSL DLL found under the Python env DLLs dir.
  echo          Set env var DEEPSEEK_SSL_BIN to a directory that
  echo          contains libssl/libcrypto DLLs, then retry.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  [4/4] Setting up backend (Node repo via project-config.json)
echo ============================================================
rem Resolve repository path: project-config.json projectPath (override) or
rem auto-detect a sibling dir of Build/ containing package.json + .git
rem (fallback: <Build parent>\Source). resolve_repo.py prints one line.
for /f "usebackq delims=" %%i in (`python resolve_repo.py`) do set "REPO_DIR=%%i"
if not defined REPO_DIR (
  echo [FAILED] Cannot resolve repository path from project-config.json.
  pause
  exit /b 1
)
echo Repository: %REPO_DIR%
if not exist "%REPO_DIR%\package.json" (
  echo [FAILED] No package.json found in %REPO_DIR%.
  echo          Check projectPath in project-config.json.
  pause
  exit /b 1
)

rem Ensure pnpm is available (Node 18+ ships corepack)
where pnpm >nul 2>&1
if errorlevel 1 (
  echo pnpm not found, enabling via corepack ...
  call corepack enable pnpm >nul 2>&1
  if errorlevel 1 (
    echo corepack failed, installing pnpm via npm ...
    call npm install -g pnpm
    if errorlevel 1 (
      echo [FAILED] pnpm install failed.
      pause
      exit /b 1
    )
    rem refresh PATH for this session (npm global bin dir)
    for /f "usebackq delims=" %%g in (`npm config get prefix`) do set "PATH=%%g;%PATH%"
  )
)
where pnpm >nul 2>&1
if errorlevel 1 (
  echo [FAILED] pnpm still unavailable.
  echo          Restart this console, or run: npm install -g pnpm
  pause
  exit /b 1
)
echo pnpm:
call pnpm --version

cd /d "%REPO_DIR%"
echo.
echo Installing backend dependencies (pnpm install) ...
call pnpm install
if errorlevel 1 (
  echo [FAILED] pnpm install failed.
  pause
  exit /b 1
)
cd /d "%~dp0"

echo.
echo ============================================================
echo  [OK] Dependencies ready (venv: Build\.venv). The backend
echo  will be built automatically on first exe run
echo  (Deepseek Harness.exe).
echo ============================================================

