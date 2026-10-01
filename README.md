# DeepSeek Harness 桌面启动器（DSH_Desktop）

`DSH_Desktop\` 是 WebView2 桌面窗口启动器的构建与配置目录：负责拉起后端、弹出无边框窗口，内置**版本自由切换**，并可打包成独立 exe（`DSH_Desktop.exe`）与安装器。

## 特性

- **版本自由切换（基于 git）**：标题栏常驻"检查更新"按钮，后台自动检测官方仓库新提交，点开即可选择任意历史版本一键切换；切换为纯本地强制操作（丢弃本地改动），完成后自动重新构建并重开画面，无需手动重启
- **零依赖接收方**：发布包自带便携 git / node / pnpm，接收方无需安装任何环境；后端仓库首次启动自动拉取
- **系统托盘常驻**：关窗隐藏到托盘，后端继续运行；托盘"退出"才真正结束（Job Object 兜底，进程强杀后端必死）
- **单实例**：重复启动唤起已有窗口
- **主题跟随应用设置**：窗口初始背景/边框色取自前端主题 token，启动无白闪
- **数据不写 C 盘**：日志、WebView2 缓存、pnpm 依赖仓库都放安装目录 `data\` 下

## 目录结构

```
<根目录>/
├── README.md                      # 本文档
├── DSH_Desktop.exe                # 打包输出（根目录，01 bat 产出）
├── DSH_Desktop/                   # 构建工具目录
│   ├── 00_env.bat                 # 依赖检查/安装脚本
│   ├── 01_dsh-window-build.bat    # 打包脚本（PyInstaller，英文）
│   ├── 02_dsh-window-test.bat     # 测试窗口脚本（独立实例，不影响正式 DSH）
│   ├── project-config.json        # 项目仓库路径配置（相对路径，不写死）
│   ├── resolve_repo.py            # 仓库路径解析（00_env.bat 用）
│   ├── requirements.txt           # Python 依赖（pywebview / pyinstaller）
│   ├── paths.env                  # 路径文件（bat 自动生成，全相对路径，gitignore）
│   ├── last-build.txt             # 上次成功构建的后端源码指纹（launcher 自动维护）
│   ├── last-commit.txt            # 当前版本提交哈希（切换版本时写入）
│   ├── last-update-seen.txt       # 升级通知"已读"标记（红点记忆，重启仍生效）
│   ├── window/                    # 窗体构建内容（launcher + spec + 图标）
│   │   ├── dsh_launcher/          # 启动器源码包（职责见下方"启动器源码结构"）
│   │   ├── DSH_Desktop.spec       # PyInstaller 打包规格
│   │   ├── deepseek娘.png / .ico  # 标题栏图标 / 窗口图标
│   │   └── make_icon_png.ps1 / verify_ico.py  # 图标生成/校验脚本
│   └── portable/                  # 发布包内嵌工具（00_release.bat 打入 release）
│       ├── git/                   # 便携 MinGit（无 git 机器也能拉取/切换版本）
│       ├── node/                  # 便携 Node.js
│       └── pnpm/                  # 便携 pnpm.exe（standalone）
├── data/                          # 程序数据（launcher 创建，不进 C 盘）
│   ├── logs/                      # 运行日志 dsh-webview2.log
│   ├── WebView2/                  # WebView2 用户数据（每实例独立子目录）
│   └── pnpm-store/                # 后端依赖仓库（首启 pnpm install 用）
├── deepseek-harness/              # 后端 Node 仓库（首次启动自动 clone，gitignore）
└── release/                       # 发布打包目录（00_release.bat 产出）
    ├── 00_release.bat             # 一键发布脚本
    ├── DSH_DESKTOP.iss            # Inno Setup 安装器脚本
    ├── DSH_Desktop/               # 便携发布包（exe + 支持目录 + portable）
    └── DSH_Desktop Setup.exe      # 安装器（需装有 Inno Setup 6）
```

### 启动器源码结构

`DSH_Desktop\window\dsh_launcher\` 按依赖从底层到上层排列，上层模块只 import 下层模块：

| 文件 | 职责 |
| --- | --- |
| `__main__.py` | 程序入口（`python -m dsh_launcher` / PyInstaller 入口脚本），调用 `main.main()` |
| `__init__.py` | 包说明：模块依赖顺序、跨模块状态变量的访问约定 |
| `paths.py` | 路径与常量：根目录/构建目录/仓库定位（paths.env → project-config.json → 自动探测）、端口、`data\` 目录、指纹标记文件 |
| `logs.py` | 日志：写 `data\logs\dsh-webview2.log`，控制面板日志区的内存 buffer 与 UI 回调 |
| `proc.py` | 子进程：静默运行并逐行捕获输出、结束进程树、端口/HTTP 就绪探测、启动取消状态 `_ACTIVE` |
| `tools.py` | 工具定位：git / node / pnpm（便携优先、系统回退），读取 Windows 系统代理，git 调用封装 |
| `app_config.py` | 桌面端设置持久化（关闭按钮行为：退出 / 最小化到托盘） |
| `theme.py` | 主题：标题栏尺寸与配色常量、深色滚动条、读取/监听 `~/.dsh` 主题偏好、读取前端 CSS token |
| `jobobject.py` | Job Object（KILL_ON_JOB_CLOSE）：主进程退出时连带结束后端进程树 |
| `webview2_env.py` | WebView2 用户数据目录（按实例 PID 隔离）、环境预热、旧目录清理 |
| `app_state.py` | 全局应用状态：主窗口/托盘/显示隐藏/退出、单实例（命名 Mutex + Event）、控制面板忙碌状态 |
| `splash.py` | 启动加载窗：进度条、取消启动、致命错误提示 |
| `dialogs.py` | 对话框通用外框：圆角 Region、按钮样式、自绘标题栏外框 |
| `build.py` | 后端构建：源码指纹、首次 clone、依赖安装与校验、`pnpm run build`、构建物清理 |
| `backend.py` | 后端进程：启动/停止/重启、就绪等待、解析带 token 的 URL、运行状态 |
| `updater.py` | 升级：fetch 官方仓库检查更新、版本列表、升级对话框、切换版本后重建 |
| `webview_patches.py` | pywebview / WebView2 补丁：主题同步注入、启动白屏消除、WinForms BrowserForm 定制 |
| `titlebar.py` | 原生自绘标题栏（WinForms）与前端调用的 `js_api` |
| `control_panel.py` | 控制面板（原生主页）：版本卡片、环境检测、构建、DSH 启动、日志区 |
| `main.py` | 启动主流程：单实例 → splash → clone（首次）→ 创建窗口 → 安装标题栏/控制面板/托盘 |

> **跨模块访问约定**：`app_state` / `backend` 中会被 `global` 重新赋值的状态变量（如 `_MAIN_WINDOW`、`_JOB_HANDLE`、`_BACKEND_PORT_IN_USE`），其他模块必须写成 `app_state._JOB_HANDLE` 这种通过模块访问的形式，不能用 `from ... import`，否则拿到的是导入时的旧值。

## 快速开始

### 1. 源码运行（开发调试）

```bat
cd DSH_Desktop\window
..\.venv\Scripts\python.exe -m dsh_launcher
```

前置条件：

- 已运行 `DSH_Desktop\00_env.bat`（自动创建 `DSH_Desktop\.venv`，并在 venv 内安装 pywebview + pyinstaller，不污染全局 Python）
- `deepseek-harness`（或 `project-config.json` 指向的仓库）内已执行过 `pnpm install`
- 系统装有 WebView2 Runtime（Win11 自带）

### 2. 打包成 exe

```bat
DSH_Desktop\01_dsh-window-build.bat
```

- 输出：仓库根目录的 `DSH_Desktop.exe`（即 `<根目录>\DSH_Desktop.exe`）
- 打包前会自动结束正在运行的旧 exe（文件占用会失败）
- 启动器**不修改 site-packages**：窗口边框/主题修复是运行时 monkey-patch，pip 重装 pywebview 不影响，打包也不需要额外处理

### 3. 测试窗口（可选）

```bat
DSH_Desktop\02_dsh-window-test.bat
```

以独立实例（独立 mutex + 独立端口 `3081`）启动一个测试窗口，**不会干扰正在运行的正式 DSH**。用于验证 UI 改动、演示"有更新"红点等。

### 4. 发布（可选，打安装器/便携包）

```bat
release\00_release.bat
```

产出 `release\DSH_Desktop\` 便携包（自带 portable git/node/pnpm）与 `release\DSH_Desktop Setup.exe` 安装器（需要 Inno Setup 6）。详见 [Release 打包](#release-打包)。

## 版本切换（重点）

DSH_Desktop 的升级/降级本质上是 **git 操作**，但**切换本身是纯本地操作、不访问网络**：启动器对本地仓库执行 `git checkout -f` 把代码切换到目标提交（**强制切换，丢弃工作区所有未提交改动**，切换后处于 detached HEAD），随后**自动重新构建并重开画面**，无需手动重启。因此可以**随意切换**：升级到最新、回退到任意历史版本、或固定在某次提交。

> 远程拉取（`git fetch`）只在两个时机发生：**后台定期检查更新时静默拉取**、以及对话框中点 **"获取最新仓库"** 时拉取。版本列表里的 commit 都是本地已有的，切换时直接选择即可，不联网。

### 入口：标题栏"检查更新"按钮

- 按钮常驻在标题栏右侧（最小化按钮左侧）
- **灰色**：当前无更新
- **蓝色 + 红点**：检测到官方仓库有新的提交（红点点击一次后消失，重启后仍记住，记录在 `last-update-seen.txt`）
- 后台线程定期检测：启动后 3 秒开始第一次检测，之后每 30 分钟一次（可用环境变量调整，见下表）

### 版本选择对话框

点击"检查更新"打开 **"升级 DSH Desktop"** 对话框（无论有没有更新都会打开，方便随时回退历史版本）：

- **git log 风格版本列表**：短哈希 / 日期 / 提交说明（最近 100 条，优先显示官方 `origin/master`，本地从未拉取成功时回退当前分支历史）
- **当前版本**行灰色 ● 置灰不可选，其余版本单选 ○
- **获取最新仓库**：立即 `git fetch` 官方 master 并刷新列表（新提交出现）；拉取期间"切换版本"与"获取最新仓库"按钮暂时禁用
- **切换版本**：对选中的提交执行切换；点击后**升级窗口立即关闭**，切换与重建全程在后台进行（主界面显示"版本切换中…"覆盖层）
- 列表默认选中第一个可选项（官方最新），也可选任意历史提交实现**降级**

### 切换流程（点击"切换版本"后，全程本地、不联网）

1. 校验目标提交在本地存在（列表中的 commit 均已就绪）
2. `git checkout -f <目标提交>`：**强制切换**，丢弃工作区所有未提交改动，切换后处于 detached HEAD（不在任何分支上）
3. 记录新哈希到 `DSH_Desktop\last-commit.txt`
4. 删除构建指纹 `last-build.txt`（切换后立即重建；若失败，需手动在控制面板点"前后端构建"重试）
5. `pnpm install`（尽力而为，失败会提示但版本已切换）
6. **自动重新构建**：弹构建窗口执行 `pnpm run build`
7. **自动重启后端并刷新画面**：新版本立即生效，无需手动重启

切换期间主界面显示"版本切换中…"全屏覆盖层（主题色跟随页面，不闪白），日志操作按钮也一并锁定（避免对正在被 `git checkout` 的工作区继续操作）。若构建失败，会弹窗提示，此时界面仍是旧版本的后端（磁盘代码已切换）；需在控制面板点"前后端构建"手动重试。

### 建议配置 SSH（推荐）

版本切换与首次拉取仓库都通过 git 从官方仓库获取，地址为：

```
SSH:   git@github.com:deepseek-ai/deepseek-harness.git
HTTPS: https://github.com/deepseek-ai/deepseek-harness.git
```

拉取默认走 **SSH**，**建议提前配置好 GitHub SSH key**：SSH 拉取最稳定、不受 HTTPS 认证/限流影响，首次安装和后续切换版本都会更快更可靠。未配置 SSH 时启动器会自动回退 HTTPS（也能用，但可能遇到认证或速率限制）。

Windows 上配置步骤（概要）：

1. 生成密钥（若没有）：`ssh-keygen -t ed25519 -C "你的邮箱"`（一路回车，默认存到 `%USERPROFILE%\.ssh\id_ed25519`）
2. 打开 `%USERPROFILE%\.ssh\id_ed25519.pub`，复制全部内容
3. 到 GitHub → Settings → SSH and GPG keys → **New SSH key** → 粘贴保存
4. 验证：`ssh -T git@github.com`，看到 `Hi <用户名>!` 即成功

### 网络与代理

- 直连 GitHub 失败时（尤其代理环境），自动读取 **Windows 系统代理**（注册表 `HKCU ...\Internet Settings`）作为 git `http.proxy` 重试
- 也可用环境变量 `DSH_GIT_PROXY` 显式指定代理（优先于系统代理）
- git 可执行文件优先用**系统 git**（用户已安装时），否则回退包内**便携 MinGit**（release 包自带）

## 配置

### 项目仓库路径（`DSH_Desktop\project-config.json`）

**首选机制：`DSH_Desktop\paths.env`（路径文件）**。`01_dsh-window-build.bat` / `00_env.bat` 运行时会把解析出的路径写入该文件，文件内**全部是相对根目录的相对路径**：

```
ROOT=.
BUILD_DIR=DSH_Desktop
WINDOW_DIR=DSH_Desktop/window
VENV_DIR=DSH_Desktop/.venv
REPO_DIR=deepseek-harness
EXE_NAME=DSH_Desktop.exe
```

`resolve_repo.py`、`dsh_launcher`（打包后 exe）都**优先读取这个文件**来定位仓库/构建目录/venv——目录改名或换位置后，只需重新运行 `01_dsh-window-build.bat`（或 `00_env.bat`）重新生成该文件，所有程序自动跟随，无需改任何代码。paths.env 是生成文件（已 gitignore）。

`paths.env` 缺失/损坏时回退到以下顺序：

1. `project-config.json` 的 `projectPath`（显式覆盖，可选）
2. 自动探测：`DSH_Desktop` 同层级（父目录）下、排除 `DSH_Desktop` 自身、含 `package.json` + `.git` 的目录（多个时取字母序第一个）——仓库改名/换位置都无需改配置
3. 回退默认布局 `<DSH_Desktop 上级>/deepseek-harness`

```json
{
  "projectPath": null
}
```

`projectPath` 留空/缺省即自动探测；需要覆盖时才填：绝对路径，或**相对本 DSH_Desktop 目录**的相对路径（例：仓库在 `D:\code\dsh` 时写 `"../../code/dsh"`）。

### 主题（跟随应用设置，不是系统主题）

窗口初始背景色/边框色按以下顺序决定：

1. **`$DSH_HOME/settings.yaml`** 里的 `ui-theme.preference`：`light` / `dark` / `system`（默认 `system`）
   - `$DSH_HOME` 规则与仓库 `packages/util/home-paths` 一致：`$DSH_HOME` 环境变量 > `~/.dsh`
   - 多用户各自读自己的 home，互不干扰
2. 颜色值取自项目内 `deepseek-harness/packages/client/ui-theme/src/styles/design-platform.css` 的 token（`--dsw-static-neutral-bluish-950` 深色 / `-50` 浅色），不硬编码
3. `system` 或缺省才读 Windows 系统主题（注册表 `AppsUseLightTheme`）

启动后主题仍由网页（Host 主题插件）管理，切换会通过 `set_theme` 实时同步标题栏。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DSH_PORT` | `3080` | 后端监听端口 |
| `DSH_WAIT` | `120` | 后端就绪等待上限（秒） |
| `DSH_TOKEN_WAIT` | `8` | 启动时等待后端打印带 token 的 Web URL 上限（秒）；超时回退裸 URL（旧版后端/异常场景） |
| `DSH_SINGLE_INSTANCE` | `Local\DSH_Desktop_SingleInstance` | 单实例 mutex 名。改此值即绕开单实例保护，用于并行开测试实例（见 `02_dsh-window-test.bat`） |
| `DSH_NCHIT_LOG` | 无 | 设为任意值可输出命中测试调试日志 |
| `DSH_GIT_PROXY` | 无 | git 显式代理（`http://host:port`），优先于系统代理 |
| `DSH_UPDATE_FIRST_DELAY` | `3` | 启动后首次更新检测延迟（秒） |
| `DSH_UPDATE_INTERVAL` | `1800` | 更新检测间隔（秒，默认 30 分钟） |
| `DSH_DEMO_UPDATE` | 无 | 模拟"有更新"数据与模拟切换（离线测试 UI，不访问网络、不碰仓库） |
| `DSH_DEMO_UPDATE_AUTOOPEN` | 无 | 演示模式下自动弹出升级对话框 |

`DSH_HOME` 为后端自身使用的变量（决定 `settings.yaml` 位置），见上文"主题"。

### 数据目录

程序产生的数据统一放**安装根目录 `data\`**，不进 C 盘：

- 日志：`data\logs\dsh-webview2.log`（排查问题先看它）
- WebView2 用户数据：`data\WebView2\win-<pid>`（每个实例独立子目录，避免多窗口冲突；已退出实例的残留目录自动清理）
- pnpm 依赖仓库：`data\pnpm-store`（首次安装 `pnpm install --store-dir` 指定）

注意：WebView2 数据目录不再落在 exe 旁（不会生成 `<exe>.WebView2`）。

## 启动流程

**双击 exe 只负责两件事：拉起后端仓库（如缺失）+ 打开控制面板。依赖安装、构建、后端启动、打开画面一律由控制面板按钮手动触发。**

1. 单实例判定：已有实例运行则通知其显示窗口，本实例退出
2. 弹启动加载窗（splash：图标 + 进度条）
3. **首次运行**（仓库缺失/无效时）：用内嵌 git 拉取官方仓库（`init + fetch + checkout` 三段式，SSH → HTTPS、系统代理逐级回退）；加载窗**实时显示拉取进度**（"正在连接远程仓库 / 正在下载代码 N% / 正在检出文件 N%" + 进度条，SSH 卡住 90 秒自动回退 HTTPS）；**支持断点续传**——中断（关闭/断网）后 `.git` 里已下载的对象保留，下次启动接着拉，不重复下载、不删残留。**不再自动安装依赖 / 构建 / 启动后端**
4. 打开主窗体（控制面板模式）：WebView2 先加载空白页（被控制面板盖住），等待用户手动触发

此后所有动作都经控制面板按钮发起：

| 按钮 | 行为 |
|---|---|
| **运行环境检测** | 检查 node / pnpm 可用性；依赖未装则 `pnpm install`，lockfile 与已装依赖不一致则更新；顺带检查后端编译产物是否缺失（只报告，不自动构建）。同时校验 pnpm 的 `node_modules` 符号链接是否健全（损坏会识别并拒绝误删） |
| **前后端构建** | 执行 `pnpm run build`；成功后把后端源码指纹写入 `DSH_Desktop\last-build.txt`（指纹 = HEAD 树内容 + 工作区改动内容 + gitignore 之外的 untracked 文件内容，纯内容级、不看 commit，切换版本后必然触发） |
| **构建物清除** | 调用仓库自带 `pnpm run clean` 清理构建产物 |
| **DSH 启动** | 若后端编译产物缺失先自动构建一次（产物 `apps/cli/lib/bin.js`），再静默启动后端 `node apps/cli/lib/bin.js web`（新版后端加 `--no-open`：不弹系统默认浏览器，桌面壳自己开 WebView2 窗口），轮询 `http://127.0.0.1:3080` 直到就绪；新版后端强制浏览器会话认证（`browser-auth`：不带启动 token 或有效 cookie 的 `/` 一律 401），启动器从后端日志解析 `dsh web: <带 token 的 URL>` 后加载，每次启动重新解析（WebView2 数据目录按实例独立，cookie 不跨启动保留）。控制面板隐藏 → WebView2 覆盖内容区加载页面 |
| **取消** | 环境检测/构建/清除进行中可用，用于中断当前动作 |
| **打开日志路径 / 清除日志** | 定位 `data\logs\` / 清空日志区显示（磁盘文件不动） |

- 构建与检测的 cmd 输出实时透传到右侧日志区
- 后台启动更新检测线程（定期 `git fetch` 官方 master）

关闭窗口 → **隐藏到系统托盘**（后端继续运行）；托盘"退出" → 结束后端进程树并退出。

## 窗口特性

- 无边框 + 窗口级自绘标题栏（标题栏直接画在窗口上，**无独立标题栏控件**，截图/无障碍识别整个窗口为一个整体）：左侧应用图标，右侧"检查更新" / 最小化 / 最大化 / 关闭
- 拖动标题栏移动、双击最大化/还原（最大化=所在显示器工作区）
- 四周 8px 边缘缩放（WM_NCHITTEST，含 WebView2 子窗口转发，自动按 DPI 缩放）
- 圆角跟随 Win11（最大化时方角贴边）
- 文本可选择/复制（`text_select=True`）
- 启动瞬间的 1px 边框/白色背景已在窗口显示前（`Load` 事件）设为主题色
- 系统托盘：关闭窗口最小化到托盘（通知气泡提示），右键菜单"显示窗口 / 退出"

## Release 打包

`release\00_release.bat` 一键产出发布包：

```
release\DSH_Desktop\              <- release 根目录（= 安装后的程序根目录）
  DSH_Desktop.exe                 <- 编译好的启动器（PyInstaller onefile）
  DSH_Desktop\                    <- 支持目录（window\ + 脚本 + paths.env）
    portable\git\                 <- 便携 MinGit（无 git 机器也能拉取/切换版本）
    portable\node\                <- 便携 Node.js
    portable\pnpm\                <- 便携 pnpm.exe
```

- **后端仓库不打包**（体积考虑）：接收方首次启动时用内嵌 git 自动从官方仓库拉取；依赖安装与构建**不自动做**，需在控制面板依次点"运行环境检测"（`pnpm install`）与"前后端构建"（`pnpm run build`）。首次拉取需要联网
- 接收方**无需安装 git / node / pnpm**（全部内嵌），也无需 Python（exe 已自包含）
- 装有 Inno Setup 6 时，[8] 步还会产出安装器 `release\DSH_Desktop Setup.exe`（脚本 `release\DSH_DESKTOP.iss`）：默认安装到 `D:\Program Files\DSH_Desktop`，创建开始菜单/桌面快捷方式、卸载入口；程序数据写入 `<安装目录>\data\`（对普通用户可写）
- 便携分发：直接压缩 `release\DSH_Desktop\` 文件夹发送即可，解压即用

## 常见问题

| 现象 | 处理 |
|---|---|
| 依赖安装反复失败，日志出现 `UNKNOWN: unknown error, open/stat/scandir ...`、`deps probe transient failure ... [WinError 448] 无法遍历该路径，因为它包含不受信任的装入点` | **不是依赖损坏、不是杀毒软件、不是目录权限**。`UNKNOWN` 是 libuv 对 Windows 未识别错误码的统一回退，真实原因是 `WinError 448`：Windows 11 的 **RedirectionGuard** 拒绝遍历由非提权用户创建的 junction，而 pnpm 的 `node_modules` 顶层全是这种 junction。该限制沿进程树继承且**无法在进程内放宽**，所以删库重建不但无效，还会毁掉本来健康的环境（程序已识别此情况并拒绝删除依赖）。**最常见来源：用 Inno Setup ≥ 6.7 打的安装包**——它默认给 Setup 进程开启 RedirectionGuard，装完从"启动 DSH Desktop"勾选项拉起应用时，该位被继承（`[Setup] RedirectionGuard=no` + `[Run] ... runasoriginaluser` 已在本仓库修掉，见下）。**处理：从托盘菜单彻底退出，确认任务管理器里 `DSH_Desktop.exe` 已全部消失，再从开始菜单快捷方式或资源管理器双击重新启动**；不要从带该限制的终端/宿主进程（某些 IDE、SSH 会话、安装器子进程）启动。诊断：日志里搜 `[curdiag]`、`RT=` |
| 从安装器"启动 DSH Desktop"打开后，依赖安装必失败（报上面那条） | 安装器自身的进程限制被继承了。`release\DSH_DESKTOP.iss` 已修：`[Setup] RedirectionGuard=no`（带 `#if VER >= EncodeVer(6,7,0)` 保护，老版本 Inno 不认识该指令会直接编译失败）+ `[Run]` 补 `runasoriginaluser`（避免以提权身份启动应用，否则其创建的 junction 被标记 trusted、属管理员，与之后正常双击启动的实例行为不一致）。**必须用修好的脚本重新打包装机**；已装好的实例不会自愈（该 mitigation 只能收紧不能放宽）。验证：启动后日志 `[curdiag] RT=` 应为 `0` |
| 开着 DSH 时运行安装程序，文件被占用 / 覆盖后残留 | 关窗 ≠ 退出：窗口关闭被拦截为"隐藏到托盘"（`form.Hide()`），进程与后端继续运行；而 PyInstaller onefile bootloader 自持 `DSH_Desktop.exe` 的映像句柄。`DSH_DESKTOP.iss` 的 `[Code] InitializeUninstall` 与 `PrepareToInstall` 会调 `StopAppProcesses` **优先按安装目录路径**精确结束所有该目录下的进程（含 `portable\node\node.exe`、`portable\pnpm\pnpm.exe` 等嵌套子进程），安装侧杀不干净会明确中止并提示，不再静默走到覆盖失败。注意：**这是 1.1.0 之后的改动，1.1.0 安装包本身在"安装"阶段没有任何关闭 DSH 的逻辑**（只有卸载侧的 `taskkill /IM`），所以用 1.1.0 重装必然出现占用。排查占用：日志搜 `[curdiag]`，或在安装器里看是否弹"无法关闭正在运行的 DSH Desktop" |
| 首次运行不自动装/构建，点"DSH 启动"报构建失败 | 正常：依赖构建都改由按钮手动触发。首次先点"运行环境检测"（`pnpm install`），再点"前后端构建"或直接点"DSH 启动"（产物缺失会自动构建） |
| 首次安装卡在"正在从官方仓库拉取代码" | 检查网络/代理；**建议配置 SSH**（见上），未配 SSH 时直连/HTTPS 可能慢或被限流 |
| 切换版本后画面没变化 | 切换成功后会自动重新构建并刷新画面（构建窗口 + 覆盖层）；若构建失败会弹窗提示，界面暂时仍是旧版本后端，需在控制面板点"前后端构建"手动重试 |
| 切换版本会丢改动吗 | 会：切换是**强制切换**（`git checkout -f`），会丢弃工作区所有未提交的本地改动；切换后处于 detached HEAD |
| 切换失败提示"本地没有目标提交" | 版本列表里的 commit 都是本地已有的；若目标不在列表（如从未拉取成功），先在对话框中点"获取最新仓库"拉取后再切换 |
| "检查更新"一直灰色/无反应 | 后台检测依赖网络（默认 30 分钟一次，启动 3 秒后首次）；可用 `DSH_DEMO_UPDATE=1` 离线验证 UI |
| 后端超时未就绪 | 检查 `deepseek-harness` 内是否 `pnpm install`、端口是否被占 |
| 主题初始色不对 | 检查 `$DSH_HOME/settings.yaml` 的 `ui-theme.preference`（改完重启 launcher） |
| 关窗后程序还在 | 正常：关闭是隐藏到系统托盘，后端继续运行；托盘图标右键"退出"才真正结束 |
| 打包后窗口异常 | 确认用最新 `DSH_Desktop\window\dsh_launcher\` 重新打包；看 `data\logs\dsh-webview2.log` |
| 换机器/换用户 | 项目路径改 `project-config.json`；设置自动跟随各自 `$DSH_HOME` |
