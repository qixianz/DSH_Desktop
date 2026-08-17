@echo off
setlocal EnableDelayedExpansion
rem DeepSeek Harness release packager - 00release.bat
rem
rem Double-click ONCE to build a ready-to-distribute release package:
rem
rem   release\DSH_Desktop\            <- release root (= exe BASE)
rem     DSH_Desktop.exe           <- compiled exe (PyInstaller onefile)
rem     DSH_Desktop\                  <- support dir (window\ + paths.env + rebuild scripts)
rem     DSH_Desktop\portable\git\     <- bundled MinGit (portable git)
rem     DSH_Desktop\portable\node\    <- bundled Node.js (portable node)
rem     DSH_Desktop\portable\pnpm\    <- bundled pnpm.exe (standalone)
rem
rem NOTE: the backend repo is NOT bundled (slimmer installer). On first launch
rem the receiver fetches it from the official GitHub repo using the bundled
rem needs NO git, NO node and NO pnpm installed - the receiver
rem needs NO git, NO node and NO pnpm installed, but DOES need network on the
rem very first launch.
rem
rem When Inno Setup 6 is present, step [8] additionally produces a real
rem installer: "DSH_Desktop Setup.exe" (double-click to install,
rem start-menu/desktop shortcuts, uninstall entry).
rem
rem Requires a dev checkout next to this file:
rem   <root>\DSH_Desktop\  (window\, .venv) and <root>\deepseek-harness\
rem venv / PyInstaller are auto-created when missing.
cd /d "%~dp0"




set "REL_DIR=%~dp0"
set "DEV_ROOT=%~dp0.."
set "DEV_BUILD=%DEV_ROOT%\DSH_Desktop"
set "OUT=%REL_DIR%DSH_Desktop"
set "REPO_SRC=%DEV_ROOT%\deepseek-harness"




rem ============ [1] dev environment check ============
if not exist "%DEV_BUILD%\window\webview2_launcher.py" (
  echo [FAILED] dev build dir not found: %DEV_BUILD%
  echo          Expected DSH_Desktop\window\webview2_launcher.py next to release\.
  pause
  exit /b 1
)
if not exist "%DEV_BUILD%\window\DSH_Desktop.spec" (
  echo [FAILED] spec not found: %DEV_BUILD%\window\DSH_Desktop.spec
  pause
  exit /b 1
)




rem ============ [2] venv + Python deps: auto-create / auto-install ============
set "VENV_PY=%DEV_BUILD%\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo Creating virtual environment: %DEV_BUILD%\.venv
  python -m venv "%DEV_BUILD%\.venv"
  if errorlevel 1 (
    echo [FAILED] venv creation failed.
    echo          Install Python 3.10+ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  echo Virtual environment created.
)
"%VENV_PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  if exist "%DEV_BUILD%\requirements.txt" (
    echo Installing Python deps from requirements.txt ...
    "%VENV_PY%" -m pip install -r "%DEV_BUILD%\requirements.txt"
  ) else (
    echo Installing latest PyInstaller + pywebview ...
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




rem ============ [3] prepare output dir ============
if exist "%OUT%" (
  echo Cleaning old release package: %OUT%
  rmdir /s /q "%OUT%" >nul 2>&1
  if exist "%OUT%" echo [WARN] old release partially locked, build may fail.
)
mkdir "%OUT%" >nul 2>&1




rem ============ [4] build exe (no taskkill: never kills a running copy) ============
echo Building DSH_Desktop.exe into %OUT% ...
"%VENV_PY%" -m PyInstaller --noconfirm --clean --distpath "%OUT%" --workpath "%REL_DIR%_pyinstaller-build" "%DEV_BUILD%\window\DSH_Desktop.spec"
if errorlevel 1 (
  echo.
  echo [BUILD FAILED] check the error output above.
  echo          Close any running DSH_Desktop.exe, then rerun this script.
  pause
  exit /b 1
)
if not exist "%OUT%\DSH_Desktop.exe" (
  echo [FAILED] exe not produced: %OUT%\DSH_Desktop.exe
  pause
  exit /b 1
)
echo Exe built OK.




rem ============ [5] backend repo: NOT bundled (slimmer installer) ============
rem Release package no longer bundles the backend repo (node_modules + build
rem artifacts are the size hog). On first launch the launcher fetches it from
rem https://github.com/deepseek-ai/deepseek-harness via bundled git, then
rem pnpm install + build (bundled node/pnpm). First launch needs network.
if not exist "%REPO_SRC%\package.json" echo [WARN] local dev repo missing at %REPO_SRC%.
echo Backend repo NOT bundled: receiver fetches it on first launch (needs network).




rem ============ [5.5] bundled portable git (MinGit): receiver needs no git ============
set "PGIT_DIR=%OUT%\DSH_Desktop\portable\git"
set "PGIT_CACHE=%~dp0portable\git"
if exist "%PGIT_DIR%\cmd\git.exe" goto GIT_READY
if exist "%PGIT_CACHE%\cmd\git.exe" goto GIT_STAGED
echo Downloading MinGit (portable git, ~40MB zip / ~130MB unpacked) ...
set "MGIT_URL=https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/MinGit-2.47.1-64-bit.zip"
rem Prefer the latest MinGit asset URL via GitHub API (fallback = fixed version above).
rem Note: no | pipe chars allowed on this cmd line - cmd splits on them.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$u='!MGIT_URL!';" ^
  "try { $r=Invoke-RestMethod -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest' -UseBasicParsing; $m=$r.assets.Where({ $_.name -like 'MinGit-*-64-bit.zip' }); if ($m.Count -gt 0) { $u=$m[0].browser_download_url } } catch { };" ^
  "$tmp=Join-Path '%REL_DIR%' 'dsh-mingit.zip';" ^
  "if (Test-Path $tmp) { Remove-Item $tmp -Force };" ^
  "Invoke-WebRequest -Uri $u -OutFile $tmp -UseBasicParsing -TimeoutSec 600;" ^
  "if (Test-Path '!PGIT_CACHE!') { Remove-Item '!PGIT_CACHE!' -Recurse -Force };" ^
  "Expand-Archive -Path $tmp -DestinationPath '!PGIT_CACHE!' -Force;" ^
  "Remove-Item $tmp -Force"
if errorlevel 1 goto GIT_DLFAIL
goto GIT_STAGED
:GIT_STAGED
echo Copying pre-staged MinGit into the package ...
robocopy "%PGIT_CACHE%" "%PGIT_DIR%" /E /NP /NFL /NDL /NJH /NJS /R:1 /W:1 >nul
if errorlevel 8 goto GIT_COPYFAIL
goto GIT_CHECK
:GIT_DLFAIL
echo [FAILED] MinGit download failed. Release must bundle git for receivers without git.
echo          Download MinGit-*-64-bit.zip from https://github.com/git-for-windows/git/releases/latest
echo          and unpack it to:  %~dp0portable\git\   (so that cmd\git.exe exists), then rerun.
pause
exit /b 1
:GIT_COPYFAIL
echo [FAILED] robocopy failed while copying bundled git.
pause
exit /b 1
:GIT_CHECK
if not exist "%PGIT_DIR%\cmd\git.exe" goto GIT_MISSING
echo Bundled git ready: %PGIT_DIR%\cmd\git.exe
goto GIT_END
:GIT_MISSING
echo [FAILED] bundled git missing at %PGIT_DIR%\cmd\git.exe
pause
exit /b 1
:GIT_READY
echo Bundled git already present: %PGIT_DIR%\cmd\git.exe
goto GIT_END
:GIT_END




rem ============ [5.6] bundled portable node: receiver needs no node/pnpm ============
set "PNODE_DIR=%OUT%\DSH_Desktop\portable\node"
set "PNODE_CACHE=%~dp0portable\node"
if exist "%PNODE_DIR%\node.exe" goto NODE_READY
if exist "%PNODE_CACHE%\node.exe" goto NODE_STAGED
echo Downloading Node.js v22 LTS (portable, ~30MB zip / ~75MB unpacked) ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$u='https://nodejs.org/dist/v22.22.3/node-v22.22.3-win-x64.zip';" ^
  "$tmp=Join-Path '%REL_DIR%' 'dsh-node.zip';" ^
  "$tmp2=Join-Path '%REL_DIR%' 'dsh-node-unpack';" ^
  "if (Test-Path $tmp) { Remove-Item $tmp -Force };" ^
  "if (Test-Path $tmp2) { Remove-Item $tmp2 -Recurse -Force };" ^
  "Invoke-WebRequest -Uri $u -OutFile $tmp -UseBasicParsing -TimeoutSec 600;" ^
  "Expand-Archive -Path $tmp -DestinationPath $tmp2 -Force;" ^
  "$inner=Get-ChildItem -Path $tmp2 -Directory;" ^
  "if (-not $inner) { throw 'node zip has no top-level dir' };" ^
  "if (Test-Path '!PNODE_CACHE!') { Remove-Item '!PNODE_CACHE!' -Recurse -Force };" ^
  "Move-Item $inner[0].FullName '!PNODE_CACHE!';" ^
  "Remove-Item $tmp -Force;" ^
  "Remove-Item $tmp2 -Recurse -Force"
if errorlevel 1 goto NODE_DLFAIL
goto NODE_STAGED
:NODE_STAGED
echo Copying pre-staged Node into the package ...
robocopy "%PNODE_CACHE%" "%PNODE_DIR%" /E /NP /NFL /NDL /NJH /NJS /R:1 /W:1 >nul
if errorlevel 8 goto NODE_COPYFAIL
goto NODE_CHECK
:NODE_DLFAIL
echo [FAILED] Node download failed. Release must bundle node for receivers without node.
echo          Download node-v22-win-x64.zip from https://nodejs.org/dist/
echo          and unpack it to:  %~dp0portable\node\   (so that node.exe exists), then rerun.
pause
exit /b 1
:NODE_COPYFAIL
echo [FAILED] robocopy failed while copying bundled node.
pause
exit /b 1
:NODE_CHECK
if not exist "%PNODE_DIR%\node.exe" goto NODE_MISSING
echo Bundled node ready: %PNODE_DIR%\node.exe
goto NODE_END
:NODE_MISSING
echo [FAILED] bundled node missing at %PNODE_DIR%\node.exe
pause
exit /b 1
:NODE_READY
echo Bundled node already present: %PNODE_DIR%\node.exe
goto NODE_END
:NODE_END




rem ============ [5.7] bundled pnpm (standalone win32-x64 zip) ============
rem The repo node_modules does not contain pnpm itself (corepack/system pnpm
rem does not add it), so the release bundles the pnpm standalone
rem (pnpm.exe + dist\; unpacked as a whole). Version pinned to the repo
rem package.json packageManager value (currently v11.7.0) so pnpm does not
rem auto-switch/download on version mismatch.
set "PPNPM_DIR=%OUT%\DSH_Desktop\portable\pnpm"
set "PPNPM=%PPNPM_DIR%\pnpm.exe"
set "PPNPM_CACHE=%~dp0portable\pnpm"
if exist "%PPNPM%" goto PNPM_READY
if exist "%PPNPM_CACHE%\pnpm.exe" goto PNPM_STAGED
echo Downloading pnpm standalone (pnpm-win32-x64.zip, v11.7.0) ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$u='https://github.com/pnpm/pnpm/releases/download/v11.7.0/pnpm-win32-x64.zip';" ^
  "$tmp=Join-Path '%REL_DIR%' 'dsh-pnpm.zip';" ^
  "if (Test-Path $tmp) { Remove-Item $tmp -Force };" ^
  "$ok=$false; for ($i=0; $i -lt 3 -and -not $ok; $i++) { try { Invoke-WebRequest -Uri $u -OutFile $tmp -UseBasicParsing -TimeoutSec 300; $ok=$true } catch { Start-Sleep -Seconds 3 } };" ^
  "if (-not $ok) { throw 'pnpm download failed' };" ^
  "if (Test-Path '%PPNPM_CACHE%') { Remove-Item '%PPNPM_CACHE%' -Recurse -Force };" ^
  "[void](New-Item -ItemType Directory -Force -Path '%PPNPM_CACHE%');" ^
  "Expand-Archive -Path $tmp -DestinationPath '%PPNPM_CACHE%' -Force;" ^
  "if (-not (Test-Path (Join-Path '%PPNPM_CACHE%' 'pnpm.exe'))) { throw 'pnpm.exe not found in archive' };" ^
  "Remove-Item $tmp -Force"
if errorlevel 1 goto PNPM_DLFAIL
goto PNPM_STAGED
:PNPM_STAGED
echo Copying pre-staged pnpm into the package ...
robocopy "%PPNPM_CACHE%" "%PPNPM_DIR%" /E /NP /NFL /NDL /NJH /NJS /R:1 /W:1 >nul
if errorlevel 8 goto PNPM_COPYFAIL
goto PNPM_CHECK
:PNPM_DLFAIL
echo [FAILED] pnpm download failed. Release must bundle pnpm (runs standalone).
echo          Download pnpm-win32-x64.zip from https://github.com/pnpm/pnpm/releases/latest
echo          and unpack it to:  %~dp0portable\pnpm\   (so that pnpm.exe exists), then rerun.
pause
exit /b 1
:PNPM_COPYFAIL
echo [FAILED] robocopy failed while copying bundled pnpm.
pause
exit /b 1
:PNPM_CHECK
if not exist "%PPNPM%" goto PNPM_MISSING
echo Bundled pnpm ready: %PPNPM%
goto PNPM_END
:PNPM_MISSING
echo [FAILED] bundled pnpm missing at %PPNPM%
pause
exit /b 1
:PNPM_READY
echo Bundled pnpm already present: %PPNPM%
goto PNPM_END
:PNPM_END




rem ============ [6] support files ============
mkdir "%OUT%\DSH_Desktop\window" >nul 2>&1
copy /y "%DEV_BUILD%\window\webview2_launcher.py" "%OUT%\DSH_Desktop\window\" >nul
copy /y "%DEV_BUILD%\window\DSH_Desktop.spec" "%OUT%\DSH_Desktop\window\" >nul
copy /y "%DEV_BUILD%\window\*.ico" "%OUT%\DSH_Desktop\window\" >nul
copy /y "%DEV_BUILD%\window\*.png" "%OUT%\DSH_Desktop\window\" >nul
if exist "%DEV_BUILD%\last-build.txt" copy /y "%DEV_BUILD%\last-build.txt" "%OUT%\DSH_Desktop\" >nul
copy /y "%DEV_BUILD%\paths.env" "%OUT%\DSH_Desktop\" >nul
copy /y "%DEV_BUILD%\project-config.json" "%OUT%\DSH_Desktop\" >nul
copy /y "%DEV_BUILD%\requirements.txt" "%OUT%\DSH_Desktop\" >nul
copy /y "%DEV_BUILD%\resolve_repo.py" "%OUT%\DSH_Desktop\" >nul
copy /y "%DEV_BUILD%\00_env.bat" "%OUT%\DSH_Desktop\" >nul
copy /y "%DEV_BUILD%\01_dsh-window-build.bat" "%OUT%\DSH_Desktop\" >nul




rem ============ [7] verify ============
set "MISSING="
if not exist "%OUT%\DSH_Desktop.exe" set "MISSING=%MISSING% DSH_Desktop.exe"
if not exist "%OUT%\DSH_Desktop\window\webview2_launcher.py" set "MISSING=%MISSING% DSH_Desktop\window\webview2_launcher.py"
if defined MISSING (
  echo [FAILED] missing in package:%MISSING%
  pause
  exit /b 1
)
rem Backend repo not bundled (fetched on first launch); bundled tools must be present.
if not exist "%OUT%\DSH_Desktop\portable\git\cmd\git.exe" set "MISSING=%MISSING% portable\git"
if not exist "%OUT%\DSH_Desktop\portable\node\node.exe" set "MISSING=%MISSING% portable\node"
if not exist "%OUT%\DSH_Desktop\portable\pnpm\pnpm.exe" set "MISSING=%MISSING% portable\pnpm"
if defined MISSING (
  echo [FAILED] missing in package:%MISSING%
  pause
  exit /b 1
)




rem ============ [8] installer (Inno Setup) ============
rem Produce a real installer (DSH_Desktop Setup.exe): receiver double-clicks
rem to install; needs Inno Setup 6 (ISCC.exe), see https://jrsoftware.org/isinfo.php
set "APP_VER=1.0.0"
if exist "%OUT%\DSH_Desktop\last-commit.txt" (
  for /f "usebackq delims=" %%c in ("%OUT%\DSH_Desktop\last-commit.txt") do (
    set "APP_VER=0.1.0-%%c"
    goto APPVER_SET
  )
)
:APPVER_SET
set "ISCC_EXE="
where ISCC >nul 2>&1 && set "ISCC_EXE=ISCC"
if not defined ISCC_EXE if exist "D:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC_EXE=D:\Program Files\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
rem User-level install (no admin; silently installed to %LOCALAPPDATA%\Programs\Inno Setup 6)
if not defined ISCC_EXE if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC_EXE goto INNO_BUILD
echo.
echo [WARN] Inno Setup 6 (ISCC.exe) not found; installer not built.
echo        %OUT% is still a fully self-contained portable package
echo        (bundled git + node) - zip it and it runs anywhere.
echo        To also produce the installer, install Inno Setup 6 from
echo        https://jrsoftware.org/isinfo.php and rerun this script.
goto INNO_END
:INNO_BUILD
echo Building installer (version !APP_VER!) ...
pushd "%~dp0"
"!ISCC_EXE!" /DAppVer=!APP_VER! "%~dp0DSH_DESKTOP.iss"
set "ISCC_OK=!errorlevel!"
popd
if not "!ISCC_OK!"=="0" goto INNO_FAIL
if not exist "%~dp0DSH_Desktop Setup.exe" goto INNO_MISSING
echo Installer ready: %~dp0DSH_Desktop Setup.exe
goto INNO_END
:INNO_FAIL
echo [FAILED] installer build failed.
pause
exit /b 1
:INNO_MISSING
echo [FAILED] installer not produced: %~dp0DSH_Desktop Setup.exe
pause
exit /b 1
:INNO_END




rem drop PyInstaller work dir
rmdir /s /q "%REL_DIR%_pyinstaller-build" >nul 2>&1




echo.
echo [OK] Release package ready: %OUT%
echo      DSH_Desktop.exe       - compiled exe
echo      DSH_Desktop\               - support files + portable\git + portable\node + portable\pnpm
echo      DSH_Desktop Setup.exe - installer (if Inno Setup was found)
echo.
echo Backend repo is NOT bundled: receiver fetches it on first launch
echo (bundled git, needs network), then pnpm install + build (bundled node/pnpm).
echo To distribute:
echo   - installer: send "DSH_Desktop Setup.exe" (receiver double-clicks to install)
echo   - portable:  zip the DSH_Desktop folder and send it (receiver unzips, runs exe)
echo Receiver needs NO git / node / pnpm installed (all bundled in the package).
endlocal