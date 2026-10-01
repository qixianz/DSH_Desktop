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
  #define AppVer "1.1.0"
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
; Inno Setup 6.7.0 (2026-01) 起，Setup/Uninstall 默认给自己的进程开启 Windows
; RedirectionGuard (EnforceRedirectionTrust=1)。该 mitigation 沿进程树继承，
; 于是本脚本 [Run] 拉起的 DSH_Desktop.exe ——以及它派生的 node/pnpm/git——
; 会拒绝遍历由非提权用户创建的 junction。pnpm (node-linker=isolated) 的
; node_modules 顶层恰好全是这种 junction，所以 `pnpm install` 直接失败并报
; `UNKNOWN: unknown error, open ...` (libuv 不认识 WinError 448 而回退成
; UV_UNKNOWN)。这是"安装后依赖安装反复失败"的真正根因，与依赖是否损坏、
; 杀毒软件、目录权限都无关；且该位只能收紧不能放宽，应用自身无法自愈。
; 关闭它只影响 Setup/Uninstall 自身进程的防护，不改变安装结果的安全性：
; 本安装器的写入目标是 {app} 下的固定路径，不依赖遍历非提权创建的链接。
; VER 保护: 6.7.0 之前的 Inno 不认识该指令，会直接 "Compile aborted"。
#if VER >= EncodeVer(6,7,0)
RedirectionGuard=no
#endif
OutputDir=.
OutputBaseFilename=DSH_Desktop Setup
; CloseApplications must be NO: launcher intercepts close as "hide to tray".
; PrepareToInstall forcefully terminates only the instance under {app}.
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
; The launcher clones the backend repo here and pnpm install/build mutate it.
; Program Files is not user-writable, so grant only runtime-owned directories.
Name: "{app}\deepseek-harness"; Permissions: users-modify
Name: "{app}\DSH_Desktop"; Permissions: users-modify

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
; runasoriginaluser: 显式以"启动安装器前那个非提权用户"的身份拉起应用
; (Inno 对 postinstall 项的默认行为即如此，这里写明是为了意图明确、
;  避免日后有人加上 runascurrentuser 或去掉 postinstall 时静默退化)。
; 装完必须让应用以普通用户上下文运行: 提权实例创建的文件/junction 会被
; 标记为 trusted 且属管理员，与用户此后正常双击启动的实例行为不一致。
Filename: "{app}\DSH_Desktop.exe"; Description: "启动 DSH Desktop"; Flags: nowait postinstall skipifsilent runasoriginaluser

; Uninstall
; The launcher intercepts window close as "hide to tray" and stays alive.
; The uninstaller MUST kill it first, otherwise the running exe (PyInstaller
; onefile bootloader holds an open handle on DSH_Desktop.exe) locks files and
; the uninstall hangs / leaves residue (deepseek-harness\ + data\).
; taskkill /T kills the whole tree: the backend node process dies with it
; (parent-child tree + launcher's kill-on-close job), releasing all file locks.

; ---------------------------------------------------------------------------
; 进程回收 (安装 + 卸载共用)
;
; 结束安装目录下所有运行中的 DSH 进程 (含后端 node 与 WebView2 子树), 并确认它们
; 真的退出了。改之前: 安装侧只杀 DSH_Desktop.exe 一个镜像, 卸载侧用
; taskkill /IM DSH_Desktop.exe 按名字杀 —— 多副本安装时会误杀其它目录的实例。
;
; 为什么必须做: 关窗只是"隐藏到托盘"(FormClosing 拦截 -> form.Hide()), 进程与
; 后端继续运行; 而 PyInstaller onefile bootloader 自持 DSH_Desktop.exe 的映像
; 句柄 (实测 CreateFileW 共享模式 0 -> ERROR_SHARING_VIOLATION 32)。所以不回收到
; 位, [Files] 覆盖就必然失败 / 留下新旧混杂的残留。
;
; 匹配口径 = 进程映像路径位于目标安装目录之下 (精确前缀, 带目录边界), 因此:
;   - 嵌套目录里的进程 (portable\node\node.exe、portable\pnpm\pnpm.exe) 会命中
;   - 其它副本 (D:\ 与 E:\ 各装一份) 不会互相误杀
;   - unins*.exe 必须排除: 卸载器本身就在安装目录下, 否则规则会杀掉自己
; ---------------------------------------------------------------------------
[Code]
// 花括号在本段是 Pascal 注释定界符, 所以上面用 ; 注释, 这里不写 {app} 字面量。
// 返回 True = 已无进程占用; False = 仍有残留 (调用方决定中止还是让用户确认)。
function StopAppProcesses(const AppDir: String): Boolean;
var
  ResultCode: Integer;
  Target: String;
  Parameters: String;
  Q: String;
begin
  Result := False;
  Q := #39;  // PowerShell 单引号字符串定界符
  Target := ExpandConstant(AppDir);
  // 路径里的单引号要在 PowerShell 单引号字符串里双写
  StringChange(Target, Q, Q + Q);
  Parameters :=
    '-NoProfile -ExecutionPolicy Bypass -Command "'
    + '$app = [IO.Path]::GetFullPath(' + Q + Target + Q + ').TrimEnd('
    + Q + '\' + Q + ') + ' + Q + '\' + Q + '; '
    + 'function Get-AppProcs { Get-CimInstance Win32_Process | Where-Object { '
    + '$_.ProcessId -ne $PID -and $_.Name -notlike ' + Q + 'unins*' + Q + ' -and '
    + '$_.ExecutablePath -and '
    + '[IO.Path]::GetFullPath($_.ExecutablePath).StartsWith($app, '
    + '[StringComparison]::OrdinalIgnoreCase) } }; '
    + '$procs = @(Get-AppProcs); '
    + 'foreach ($p in $procs) { & taskkill.exe /PID $p.ProcessId /T /F | Out-Null }; '
    // 有进程被杀时等句柄释放; 无进程时不做无谓等待
    + 'if ($procs.Count -gt 0) { Start-Sleep -Seconds 2 }; '
    + '$left = @(Get-AppProcs); '
    + 'if ($left.Count -gt 0) { exit 1 } else { exit 0 }"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    exit;
  Result := (ResultCode = 0);
end;

// 安装前 (覆盖安装也走这里): 必须先让目标目录下的旧实例完全退出, 否则 [Files]
// 覆盖被占用的文件会失败, 留下新旧混杂的残留。杀不干净就明确中止并告诉用户
// 怎么做 —— 而不是静默走到覆盖失败。
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not StopAppProcesses('{app}') then
    Result := '无法关闭正在运行的 DSH Desktop（仍有进程未退出）。' + #13#10
      + '请在托盘图标上右键选择"退出"，确认任务管理器里 DSH_Desktop.exe '
      + '已全部消失后重试。';
end;

// 卸载开始时 (早于 [UninstallRun] 与文件删除): 同样按目标安装目录精确结束实例。
// 卸载即使杀不干净也允许继续 (由用户确认), 避免出现"卸不掉的安装"。
function InitializeUninstall(): Boolean;
begin
  Result := True;
  if not StopAppProcesses('{app}') then
    if MsgBox('DSH Desktop 似乎仍在运行，未能完全关闭。' + #13#10 + #13#10
        + '继续卸载可能留下未删除的文件（deepseek-harness\ 与 data\）。'
        + #13#10 + '是否仍要继续卸载？', mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
end;

[UninstallRun]
; 1) 结束运行中的实例已上移到 InitializeUninstall (按 {app} 路径精确匹配;
;    不再用 taskkill /IM 按名字杀, 那会误杀其它目录安装的副本)
; 2) 清空只读/系统/隐藏属性, 避免删除被拒 (filesandordirs 遇只读会失败残留)
Filename: "{sys}\cmd.exe"; Parameters: "/C attrib -r -s -h ""{app}\*.*"" /s /d >nul 2>&1"; Flags: runhidden; RunOnceId: "DshClearAttribs"; StatusMsg: "清除文件属性…"
; 3) 稍等, 让系统释放文件句柄后再删除目录
Filename: "{sys}\cmd.exe"; Parameters: "/C timeout /t 2 /nobreak >nul"; Flags: runhidden; RunOnceId: "DshWaitHandles"; StatusMsg: "等待进程退出…"

[UninstallDelete]
; Delete the whole app dir including runtime-generated content:
; deepseek-harness/ (repo fetched on first launch) and data/ are NOT
; installed files, so the default uninstaller leaves them behind.
; dirifempty would only remove an empty dir -> switch to filesandordirs.
Type: filesandordirs; Name: "{app}\deepseek-harness"
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}"
