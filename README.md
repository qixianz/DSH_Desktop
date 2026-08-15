# DeepSeek Harness 窗口启动器（DSH_Desktop）

`DSH_Desktop\` 是 WebView2 桌面窗口启动器的构建与配置目录：负责拉起后端、弹出无边框窗口，并打包成独立 exe。

## 目录结构

```
<根目录>/
├── README.md                      # 本文档
├── DSH_Desktop/                   # 构建工具目录
│   ├── 00_env.bat                 # 依赖检查/安装脚本
│   ├── 01_dsh-window-build.bat    # 打包脚本（PyInstaller，英文）
│   ├── project-config.json        # 项目仓库路径配置（相对路径，不写死）
│   ├── resolve_repo.py            # 仓库路径解析（00_env.bat 用）
│   ├── last-build.txt             # 上次成功构建的后端源码指纹（launcher 自动维护）
│   ├── last-commit.txt
│   └── window/                    # 窗体构建内容（launcher + spec + 图标）
│       ├── webview2_launcher.py   # 启动器主脚本（唯一需要改的源码文件）
│       ├── Deepseek Harness.spec  # PyInstaller 打包规格
│       ├── deepseek娘.png / .ico  # 标题栏图标 / 窗口图标
│       ├── make_icon_png.ps1      # 图标 PNG 生成脚本
│       └── verify_ico.py          # 图标校验脚本
├── deepseek-harness/              # 后端 Node 仓库（01 bat 自动克隆，被 .gitignore 忽略）
└── Deepseek Harness.exe           # 打包输出（根目录）
```

## 快速开始

### 1. 源码运行（开发调试）

```bat
DSH_Desktop\.venv\Scripts\python.exe DSH_Desktop\window\webview2_launcher.py
```

前置条件：

- 已运行 `DSH_Desktop\00_env.bat`（自动创建 `DSH_Desktop\.venv`，并在 venv 内安装 pywebview + pyinstaller，不污染全局 Python）
- `deepseek-harness`（或 `project-config.json` 指向的仓库）内已执行过 `pnpm install`
- 系统装有 WebView2 Runtime（Win11 自带）

### 2. 打包成 exe

```bat
DSH_Desktop\01_dsh-window-build.bat
```

- 输出：仓库根目录的 `Deepseek Harness.exe`（即 `<根目录>\Deepseek Harness.exe`）
- 打包前会自动结束正在运行的旧 exe（文件占用会失败）
- 启动器**不修改 site-packages**：窗口边框/主题修复是运行时 monkey-patch，pip 重装 pywebview 不影响，打包也不需要额外处理

## 配置

### 项目仓库路径（`DSH_Desktop\project-config.json`）

**首选机制：`DSH_Desktop\paths.env`（路径文件）**。`01_dsh-window-build.bat` / `00_env.bat` 运行时会把解析出的路径写入该文件，文件内**全部是相对根目录的相对路径**：

```
ROOT=.
BUILD_DIR=DSH_Desktop
WINDOW_DIR=DSH_Desktop/window
VENV_DIR=DSH_Desktop/.venv
REPO_DIR=deepseek-harness
EXE_NAME=Deepseek Harness.exe
```

`resolve_repo.py`、`webview2_launcher.py`（打包后 exe）都**优先读取这个文件**来定位仓库/构建目录/venv——目录改名或换位置后，只需重新运行 `01_dsh-window-build.bat`（或 `00_env.bat`）重新生成该文件，所有程序自动跟随，无需改任何代码。paths.env 是生成文件（已 gitignore）。

`paths.env` 缺失/损坏时回退到以下顺序：

1. `project-config.json` 的 `projectPath`（显式覆盖，可选）
2. 自动探测：`DSH_Desktop` 同层级（父目录）下、排除 `DSH_Desktop` 自身、含 `package.json` + `.git` 的目录（多个时取字母序第一个）——仓库改名/换位置都无需改配置
3. 回退默认布局 `<DSH_Desktop 上级>/deepseek-harness`

```json
{
  "projectPath": null
}
```

- `projectPath` 留空/缺省即自动探测；需要覆盖时才填：绝对路径，或**相对本 DSH_Desktop 目录**的相对路径（例：仓库在 `D:\code\dsh` 时写 `"../../code/dsh"`）

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
| `DSH_NCHIT_LOG` | 无 | 设为任意值可输出命中测试调试日志 |

### WebView2 数据目录

固定为 `%LOCALAPPDATA%\Deepseek Harness\WebView2`，不会在 exe 旁生成 `<exe>.WebView2`。

### 日志

运行日志追加写入 `%TEMP%\dsh-webview2.log`（排查问题先看它）。

## 启动流程

1. 读 `project-config.json` 定位项目仓库（`SOURCE`）
2. 计算后端源码指纹（HEAD 树内容 + 工作区改动内容 + gitignore 之外的 untracked 文件内容，纯内容级、不看 commit）并与 `DSH_Desktop\last-build.txt` 对比：指纹不一致（说明 gitignore 以外的后端源码与上次构建时不同）则弹控制台窗口执行 `pnpm run build`（产物 `apps/cli/lib/bin.js`）
3. 静默启动后端 `node apps/cli/lib/bin.js web`
4. 轮询 `http://127.0.0.1:3080` 直到就绪
5. 弹出 WebView2 窗口（frameless，初始背景/边框即正确主题色，无白色闪现）
6. 关闭窗口 → 自动结束后端进程树

## 窗口特性

- 无边框 + 窗口级自绘标题栏（标题栏直接画在窗口上，**无独立标题栏控件**，截图/无障碍识别整个窗口为一个整体）：左侧应用图标，右侧最小化/最大化/关闭
- 拖动标题栏移动、双击最大化/还原（最大化=所在显示器工作区）
- 四周 8px 边缘缩放（WM_NCHITTEST，含 WebView2 子窗口转发，自动按 DPI 缩放）
- 圆角跟随 Win11（最大化时方角贴边）
- 文本可选择/复制（`text_select=True`）
- 启动瞬间的 1px 边框/白色背景已在窗口显示前（`Load` 事件）设为主题色

## 常见问题

| 现象 | 处理 |
|---|---|
| 首次运行弹构建窗口 | 正常，构建成功后写入 `last-build.txt` |
| 后端超时未就绪 | 检查 `deepseek-harness` 内是否 `pnpm install`、端口是否被占 |
| 主题初始色不对 | 检查 `$DSH_HOME/settings.yaml` 的 `ui-theme.preference`（改完重启 launcher） |
| 打包后窗口异常 | 确认用最新 `DSH_Desktop\window\webview2_launcher.py` 重新打包；看 `%TEMP%\dsh-webview2.log` |
| 换机器/换用户 | 项目路径改 `project-config.json`；设置自动跟随各自 `$DSH_HOME` |
