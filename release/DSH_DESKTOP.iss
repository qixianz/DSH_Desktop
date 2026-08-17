; DeepSeek Harness - Inno Setup installer script
; Build: ISCC.exe "DSH_DESKTOP.iss"  (invoked by 00release.bat step [8])
; Output: release\DSH_Desktop Setup.exe
;
; The package ships the launcher + bundled MinGit (portable\git), bundled Node.js
; (portable\node) and bundled pnpm (portable\pnpm). The backend repo is fetched
; from the official GitHub repo on first launch (needs network) - no git, no node,
; no pnpm needed on the machine.
;
; Upgrade/reinstall behavior (same AppId, no uninstall step):
;   - installed files (DSH_Desktop.exe + DSH_Desktop\ support dir) are
;     overwritten in place (ignoreversion)
;   - runtime-generated content is KEPT: deepseek-harness\ (fetched repo)
;     and data\ (logs / WebView2 / pnpm-store) are NOT part of [Files],
;     so Inno Setup leaves them untouched on reinstall.
;     They are only removed by [UninstallDelete] on uninstall.
;   - CloseApplications must be NO: the launcher intercepts window close
;     as "hide to tray" (never exits), so Inno's auto-close would wait
;     forever and the install hangs. User must quit the running instance
;     (tray -> exit) before installing; a locked exe fails with a clear
;     "file in use" message instead of hanging.

#ifndef AppVer
  #define AppVer "1.0.0"
#endif

[Setup]
AppId={{7F3B8D2E-0A1B-4C5D-9E6F-8A7B6C5D4E3F}
AppName=DSH Desktop
AppVersion={#AppVer}
AppPublisher=DeepSeek
AppComments=DSH Desktop client (self-contained: bundled git + node)
; Install to D:\Program Files by default (user can change); program data
; (logs / WebView2 cache) lives under <install-root>\data\; data is made
; writable for all users (Program Files is not user-writable otherwise).
DefaultDirName=D:\Program Files\DSH_Desktop
DefaultGroupName=DSH Desktop
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\DSH_Desktop.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
OutputDir=.
OutputBaseFilename=DSH_Desktop Setup
; CloseApplications must be NO: launcher intercepts close as "hide to tray",
; Inno's auto-close would wait forever and hang. Quit the app first.
CloseApplications=no
RestartApplications=no
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
; 只对运行时生成的 data 目录预创建 + users-modify (原有配置)。
; 注意: 不要给整个 {app} 设 Permissions —— 覆盖安装时 {app} 已存在且
; 包含 deepseek-harness 仓库等大量文件, Inno 在"正在创建目录"阶段对
; 已存在的大目录设置 ACL 会卡死安装器。
Name: "{app}\data"; Permissions: users-modify

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; top-level exe
Source: "DSH_Desktop\DSH_Desktop.exe"; DestDir: "{app}"; Flags: ignoreversion
; support dir incl. portable\git (MinGit) + portable\node (Node.js) + portable\pnpm
; NOTE: the backend repo is NOT installed here - the launcher fetches it from
; the official GitHub repo on first launch (bundled git), then installs deps
; and builds (bundled node/pnpm). First launch needs network.
Source: "DSH_Desktop\DSH_Desktop\*"; DestDir: "{app}\DSH_Desktop"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\DSH Desktop"; Filename: "{app}\DSH_Desktop.exe"
Name: "{autodesktop}\DSH Desktop"; Filename: "{app}\DSH_Desktop.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DSH_Desktop.exe"; Description: "启动 DSH Desktop"; Flags: nowait postinstall skipifsilent

; Uninstall
; The launcher intercepts window close as "hide to tray" and stays alive.
; The uninstaller MUST kill it first, otherwise the running exe (PyInstaller
; onefile bootloader holds an open handle on DSH_Desktop.exe) locks files and
; the uninstall hangs / leaves residue (deepseek-harness\ + data\).
; taskkill /T kills the whole tree: the backend node process dies with it
; (parent-child tree + launcher's kill-on-close job), releasing all file locks.

[UninstallRun]
; 1) 先杀掉正在运行的实例 (托盘驻留也一并杀; 同名进程全杀, 含 F:\ 开发副本)
Filename: "{sys}\taskkill.exe"; Parameters: "/IM DSH_Desktop.exe /F /T"; Flags: runhidden; StatusMsg: "正在关闭 DSH Desktop…"
; 2) 清空只读/系统/隐藏属性, 避免删除被拒 (filesandordirs 遇只读会失败残留)
Filename: "{sys}\cmd.exe"; Parameters: "/C attrib -r -s -h ""{app}\*.*"" /s /d >nul 2>&1"; Flags: runhidden; StatusMsg: "清除文件属性…"
; 3) 稍等, 让系统释放文件句柄后再删除目录
Filename: "{sys}\cmd.exe"; Parameters: "/C timeout /t 2 /nobreak >nul"; Flags: runhidden; StatusMsg: "等待进程退出…"

[UninstallDelete]
; Delete the whole app dir including runtime-generated content:
; deepseek-harness/ (repo fetched on first launch) and data/ are NOT
; installed files, so the default uninstaller leaves them behind.
; dirifempty would only remove an empty dir -> switch to filesandordirs.
Type: filesandordirs; Name: "{app}\deepseek-harness"
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}"
