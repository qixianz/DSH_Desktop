#!/usr/bin/env python3
"""DeepSeek Harness WebView2 启动器 (exe 版)

目录约定:
    <根目录>/
        DSH_Desktop.exe          <- 本程序 (打包后)
        deepseek-harness/      <- dsh 仓库 (git)
        DSH_Desktop/           <- 本脚本 + last-build.txt 标记

每次启动流程:
    1. 计算后端源码指纹: HEAD 树 (git ls-tree -r HEAD)
       + 工作区内容改动 (git diff HEAD --raw, 含文件内容哈希)
       + gitignore 之外的 untracked 文件内容哈希
    2. 与 DSH_Desktop/last-build.txt 记录的指纹对比
    3. 不一致 (或标记不存在) -> 弹构建窗口执行 `pnpm run build`,
       成功则记录新指纹
    4. 启动后端 `pnpm dsh web` (静默) -> 等待 3080 端口就绪
    5. 弹出 WebView2 窗口加载 http://127.0.0.1:3080
    6. 关闭窗口即自动结束后端进程

窗口外观:
    frameless 无边框窗口 + WinForms 原生自绘标题栏 (Reasonix 风格):
    左侧应用图标 (DSH_Desktop/window/deepseek娘.png), 右侧最小化/最大化/关闭按钮。
    标题栏颜色通过 js_api.set_theme 跟随主程序主题 (body[data-ds-dark-theme]),
    配色对应前端 ui-theme design-platform.css 的 token。
    WebView2 用户数据目录固定到 <安装根目录>/data/WebView2 (日志在 data/logs),
    不写 C 盘、不在 exe 旁生成 "<exe>.WebView2"。

前提:
    - deepseek-harness 内已执行过 `pnpm install`
    - 已运行 DSH_Desktop\\00_env.bat (创建 DSH_Desktop\\.venv 并在其中安装
      pywebview + pyinstaller, 不污染全局 Python)
    - release 包自带便携 git + node (DSH_Desktop\\portable\\): 接收方无需
      安装 git / node / pnpm; 源码/开发模式回退使用系统 git / node
"""
import ctypes
from ctypes import wintypes
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包后 exe 位于根目录, 构建目录 (原 Build) 在 exe 旁。
    # 构建目录名不写死 (DSH_Desktop / 任意克隆名均可): 自动探测
    # exe 旁含 window/webview2_launcher.py 的目录。
    BASE = Path(sys.executable).resolve().parent
    BUILD_DIR = None
    try:
        for child in sorted(BASE.iterdir()):
            if child.is_dir() and (child / "window" / "webview2_launcher.py").is_file():
                BUILD_DIR = child
                break
    except OSError:
        BUILD_DIR = None
    if BUILD_DIR is None:
        BUILD_DIR = BASE / "DSH_Desktop"  # 回退: 默认名
    WINDOW_DIR = BUILD_DIR / "window"
else:
    # 源码运行时脚本位于 DSH_Desktop/window/ 下; DSH_Desktop 目录 = 脚本目录的父级
    WINDOW_DIR = Path(__file__).resolve().parent
    BUILD_DIR = WINDOW_DIR.parent
    BASE = BUILD_DIR.parent


def _load_project_config() -> Path | None:
    """从 DSH_Desktop/project-config.json 读取项目仓库路径 (可选覆盖)。

    字段 projectPath: 绝对路径, 或相对 DSH_Desktop 目录的相对路径 (如 "../deepseek-harness")。
    文件缺失/损坏/字段缺失/为空返回 None, 由调用方走自动探测。"""
    try:
        import json as _json
        cfg = BUILD_DIR / "project-config.json"
        if not cfg.is_file():
            return None
        data = _json.loads(cfg.read_text(encoding="utf-8"))
        raw = data.get("projectPath")
        if not raw or not isinstance(raw, str):
            return None
        p = Path(raw)
        return p if p.is_absolute() else (BUILD_DIR / p).resolve()
    except Exception:
        return None


def _load_paths_file() -> dict[str, str] | None:
    """读取 BUILD_DIR/paths.env（由 01/00 bat 生成）。

    文件内所有值相对 ROOT（= BUILD_DIR 的父级）。缺失/损坏返回 None，
    由调用方走 project-config.json / 自动探测。"""
    p = BUILD_DIR / "paths.env"
    try:
        if not p.is_file():
            return None
        data: dict[str, str] = {}
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip()
        return data
    except Exception:
        return None


def _find_repo() -> Path | None:
    """自动探测仓库: 在 DSH_Desktop 同层级 (父目录) 下, 排除 DSH_Desktop 自身,
    找含 package.json + .git 的目录 (即 dsh 仓库)。多个时取字母序第一个。"""
    try:
        for child in sorted(BUILD_DIR.parent.iterdir()):
            if child == BUILD_DIR or not child.is_dir():
                continue
            if (child / "package.json").is_file() and (child / ".git").is_dir():
                return child
    except OSError:
        return None
    return None


# 仓库定位顺序: 1) paths.env (01/00 bat 生成, 值相对 ROOT) 2) project-config.json
# 显式 projectPath (覆盖) 3) 自动探测 DSH_Desktop 同层级的仓库目录
# 4) 回退默认布局 <DSH_Desktop 上级>/deepseek-harness
_paths_cfg = _load_paths_file()
_repo_from_paths = None
if _paths_cfg and _paths_cfg.get("REPO_DIR"):
    _cand = BASE / _paths_cfg["REPO_DIR"]
    if (_cand / "package.json").is_file():
        _repo_from_paths = _cand.resolve()
SOURCE = (_repo_from_paths
          or _load_project_config()
          or _find_repo()
          or (BUILD_DIR.parent / "deepseek-harness"))

# 官方仓库地址 (拉取默认走 SSH, 未配 SSH/失败时回退 HTTPS):
# release 安装包不再携带后端仓库 (减小体积), 首次启动时用内嵌 git 从
# 官方仓库 clone 到 SOURCE, 再 pnpm install + build。
REPO_URL_SSH = "git@github.com:deepseek-ai/deepseek-harness.git"
REPO_URL_HTTPS = "https://github.com/deepseek-ai/deepseek-harness.git"

PORT = int(os.environ.get("DSH_PORT", "3080"))
URL = f"http://127.0.0.1:{PORT}"
# 用编译产物启动 (apps/cli/lib/bin.js): 1.3s 就绪, 对比 tsx 源码入口 18.6s。
# 且无需 tsx/esbuild, 不 spawn 子进程, 无控制台窗口闪现。
# 产物由 launcher 的 build 步骤 (pnpm run build) 生成; 缺失时会自动触发 rebuild。

# 包内便携 node: release 包自带, 接收方无需安装 node/pnpm 也能启动后端、
# 拉取仓库/切换版本 (pnpm 本体在 node_modules 里, 用 node 直接跑 pnpm.cjs)。
# Node 官方 zip 解压后顶层是 node-vXX-win-x64\, 打包时把内容放进 portable\node\。
PORTABLE_NODE = BUILD_DIR / "portable" / "node" / "node.exe"
# pnpm standalone 可执行文件 (pnpm-win32-x64.zip 里的 pnpm.exe, SEA 自带运行时):
# 仓库 node_modules 里没有 pnpm 本体 (corepack/系统 pnpm 安装时不进依赖),
# 所以 release 包自带。升级/重建时用它跑 install / run build。
PORTABLE_PNPM = BUILD_DIR / "portable" / "pnpm" / "pnpm.exe"

BACKEND_ENTRY = SOURCE / "apps" / "cli" / "lib" / "bin.js"
WAIT_TIMEOUT = int(os.environ.get("DSH_WAIT", "120"))  # 秒, 后端就绪等待上限
# 程序数据根目录 (安装根目录下的 data\ 文件夹): 日志、WebView2 用户数据等
# 我们程序产生的数据一律放这里, 不进 C 盘; 默认装 D 盘时即 D:\DeepSeek Harness\data。
# harness 后端自己的路径 (如 $DSH_HOME=~/.dsh) 由后端管理, 不在此列。
DATA_DIR = BASE / "data"
LOG_FILE = DATA_DIR / "logs" / "dsh-webview2.log"
# 上次成功构建时的后端源码指纹 (与当前指纹对比决定是否重建)
MARKER = BUILD_DIR / "last-build.txt"
# 升级通知"已读"标记: 记录用户已查看过的最新提交哈希 (红点据此显隐)
SEEN_MARKER = BUILD_DIR / "last-update-seen.txt"
# WebView2 用户数据目录 (缓存/Cookie/GPUCache 等): 放到 <根目录>\data\WebView2,
# 避免 WebView2 默认在 exe 旁生成 "<exe>.WebView2" 目录、也不进 C 盘。
# 每个实例用独立子目录 (带进程 PID): WebView2 数据目录同一时刻只允许一个
# 浏览器进程组使用, 多窗口共享同一目录会让后开者初始化卡住 (白屏/打不开)。
WEBVIEW2_DATA_BASE = DATA_DIR / "WebView2"


def _webview2_data_dir() -> Path:
    return WEBVIEW2_DATA_BASE / f"win-{os.getpid()}"


# WebView2 官方兜底: 当 pywebview 的 CreationProperties.UserDataFolder 未生效时
# (PyInstaller 打包后 pythonnet 属性赋值可能失效, 控件会落默认 <exe>.WebView2),
# 该环境变量强制所有 WebView2 环境使用自定义用户数据目录, 不在 exe 旁生成目录。
os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", str(_webview2_data_dir()))


def _prewarm_webview2_env() -> None:
    """预创建 WebView2 环境到目标数据目录 (webview.start 前调用)。

    死锁机理: WebView2 控件句柄首次创建 (全新数据目录) 时会同步初始化
    浏览器环境; pywebview 在 BrowserForm 构造 (winforms.create) 里创建
    控件, 而 WinForms 消息循环 (app.Run) 在构造之后才启动 —— 初始化
    环境所需的回调无法送达 -> UI 线程阻塞, 症状: 窗体卡死, 但 WebView2
    页面 (独立进程) 动画照常。复用已初始化目录时环境已存在, 不触发。

    这里在窗口创建前用 CoreWebView2Environment.CreateAsync (纯异步 API,
    不依赖消息循环) 预先建好环境, 控件创建时直接复用, 不再卡。

    注意: 必须先 import edgechromium (它把 WebView2Loader.dll 目录加入
    PATH 并 AddReference Core 程序集), 否则 CreateAsync 抛
    WebView2RuntimeNotFoundException。"""
    try:
        from webview.platforms import edgechromium as _ec_prewarm  # noqa: F401
        from Microsoft.Web.WebView2.Core import CoreWebView2Environment
        folder = _webview2_data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        CoreWebView2Environment.CreateAsync(str(folder)).Result
        log(f"webview2 env prewarmed: {folder}")
    except Exception as ex:
        log(f"webview2 env prewarm failed: {ex}")


def _cleanup_old_webview2_dirs() -> None:
    """清理已退出实例残留的独立数据目录 (进程已死且超过 1 天),
    避免多窗口反复启动导致 %LOCALAPPDATA% 堆积。"""
    import shutil
    if not WEBVIEW2_DATA_BASE.is_dir():
        return
    now = time.time()
    for d in WEBVIEW2_DATA_BASE.glob("win-*"):
        try:
            pid = int(d.name[4:])
            if pid == os.getpid():
                continue
            r = hidden_run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True)
            if r.returncode == 0 and f'"{pid}"' in r.stdout:
                continue  # 对应窗口还开着, 不删
            if now - d.stat().st_mtime > 86400:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

# ==================== 自定义无边框标题栏 ====================
# 配色对应前端 packages/client/ui-theme/src/styles/design-platform.css:
#   深色: bg  = --dsw-static-neutral-bluish-950 (21,21,23)
#         icon = --dsw-static-neutral-bluish-500 (151,157,166)
#         hover ≈ rgba(255,255,255,0.08) 叠加于 bg
#   浅色: bg  = --dsw-static-neutral-bluish-50 (249,250,251)
#         icon = --dsw-static-neutral-bluish-700 (97,102,107)
#   关闭: --dsw-static-red-500 (239,68,68) / 按下 更深红
# 运行时由网页主题 (set_theme) 覆盖, 这里只提供两套默认。
TITLEBAR_HEIGHT = 36       # 逻辑像素
BTN_WIDTH = 46             # 单个窗口按钮宽度
RESIZE_BORDER = 8          # 边缘缩放手感宽度 (WM_NCHITTEST)
EDGE_PADDING = 4           # WebView2 左右下留边 (逻辑像素), 让边缘 WM_NCHITTEST 直达父窗口
TITLEBAR_THEMES = {
    "dark": {
        "bg": (21, 21, 23), "hover": (47, 47, 49), "active": (64, 64, 66),
        "icon": (151, 157, 166), "close_hover": (239, 68, 68),
        "close_active": (196, 52, 52),
        # 升级通知: 蓝色文字/下划线 (无背景色), hover 更亮
        "upd": (96, 165, 250), "upd_hover": (147, 197, 253),
    },
    "light": {
        "bg": (249, 250, 251), "hover": (232, 232, 234), "active": (219, 219, 222),
        "icon": (97, 102, 107), "close_hover": (239, 68, 68),
        "close_active": (196, 52, 52),
        "upd": (37, 99, 235), "upd_hover": (29, 78, 216),
    },
}

# 注入网页的主题同步脚本: 监听 body[data-ds-dark-theme] 变化并通知原生标题栏。
# 只注入不修改任何前端源码 (前端仍由 Host 主题插件管理)。
# 注入时 pywebview 桥可能尚未就绪 (window.pywebview 未定义), 定时重试直到可用,
# 避免标题栏永远停在初始主题。
THEME_SYNC_SCRIPT = """(() => {
  const sync = () => {
    try {
      const api = window.pywebview && window.pywebview.api
      if (api && api.set_theme) {
        api.set_theme(document.body.hasAttribute('data-ds-dark-theme'))
        return true
      }
    } catch (e) {}
    return false
  }
  if (!sync()) {
    let tries = 0
    const timer = setInterval(() => {
      tries += 1
      if (sync() || tries > 50) clearInterval(timer)
    }, 200)
  }
  try {
    new MutationObserver(sync).observe(document.body, {
      attributes: true, attributeFilter: ['data-ds-dark-theme']
    })
  } catch (e) {}
})()"""


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _resolve_dsh_home() -> str:
    """复刻 deepseek-harness/packages/util/home-paths/src/index.ts 的 resolveDshHome 规则,
    不写死路径 (多用户各自有 DSH_HOME 或 ~/.dsh):
      优先级: 显式配置 > $DSH_HOME (空/纯空白视为未设置) > ~/.dsh;
      支持 ~ / ~/ / ~\\ 前缀展开; 相对路径按当前工作目录绝对化。"""
    env = os.environ.get("DSH_HOME")
    selected = env.strip() if env is not None and env.strip() else "~/.dsh"
    if selected == "~":
        return str(Path.home())
    if selected.startswith("~/") or selected.startswith("~\\"):
        # lstrip 去掉前缀后残留的斜杠: Windows 上 Path.home() / "\\x" 会把 "\\x" 当盘符根绝对路径
        return str(Path.home() / selected[2:].lstrip("\\/"))
    return str(Path(selected).resolve())


def read_theme_preference() -> str | None:
    """从 Host 用户设置文档读取主题偏好 (ui-theme.preference: light/dark/system)。

    文档路径同 deepseek-harness/packages/settings/settings-file/src/index.ts 的默认:
    <DSH_HOME>/settings.yaml (DSH_HOME 按 resolveDshHome 规则解析, 见
    _resolve_dsh_home)。这是应用自己持久化的偏好 (设置页 Appearance 行写入),
    优先于系统主题猜测: 用户配置 light/dark 与系统不一致时, 窗口首帧即正确。
    找不到文件/字段或解析失败返回 None (调用方回退系统主题)。"""
    import re
    dsh_home = _resolve_dsh_home()
    # 行内 map (ui-theme: { preference: dark }) / JSON / YAML 块 (ui-theme:\n  preference: dark)
    inline = re.compile(
        r"['\"]?ui-theme['\"]?\s*:\s*\{[^}]*preference['\"]?\s*:\s*['\"]?(light|dark|system)['\"]?",
        re.I | re.S,
    )
    block = re.compile(
        r"['\"]?ui-theme['\"]?\s*:\s*\n\s*preference['\"]?\s*:\s*['\"]?(light|dark|system)['\"]?",
        re.I,
    )
    for name in ("settings.yaml", "settings.yml", "settings.json"):
        path = Path(dsh_home) / name
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = inline.search(text) or block.search(text)
        if m:
            pref = m.group(1).lower()
            log(f"theme preference read from {path}: {pref}")
            return pref
    log("theme preference not found in settings document, fallback to system theme")
    return None


def system_dark() -> bool:
    """系统主题 (默认偏好为 'system' 时标题栏初始配色跟随系统)。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return value == 0
    except OSError:
        return True


def resolve_initial_dark() -> bool:
    """初始深色主题: 配置偏好 (settings.yaml 的 ui-theme.preference) 优先,
    缺省 (system/未配置) 才读系统主题。

    窗口背景 (main) 与自绘标题栏 (TitleBar) 共用, 保证首帧整体配色一致:
    用户配置 light/dark 与系统不一致时, 标题栏/边框不出现系统主题色。
    """
    pref = read_theme_preference()
    if pref == "dark":
        return True
    if pref == "light":
        return False
    return system_dark()


def read_theme_tokens() -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """从前端主题 CSS 读取 (深色bg, 浅色bg) RGB, 不硬编码颜色。
    对应 token: --dsw-static-neutral-bluish-950 (dark bg) / -50 (light bg)。
    找不到文件或 token 时返回 None (调用方回退默认值)。"""
    import re
    paths = [
        SOURCE / "packages" / "client" / "ui-theme" / "src" / "styles" / "design-platform.css",
    ]
    pat = re.compile(
        r"--dsw-static-neutral-bluish-(950|50)\s*:\s*rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
        re.I,
    )
    for path in paths:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            found: dict[str, tuple[int, int, int]] = {}
            for m in pat.finditer(text):
                found[m.group(1)] = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
            if "950" in found and "50" in found:
                log(f"theme tokens read from {path}: dark={found['950']} light={found['50']}")
                return found["950"], found["50"]
        except OSError:
            continue
    log("theme tokens not found, using defaults")
    return None


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def http_ready(timeout: float = 2.0) -> bool:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
        conn.request("GET", "/")
        resp = conn.getresponse()
        conn.close()
        return resp.status < 500
    except Exception:
        return False


def hidden_run(args: list[str], **kw):
    """Run a console program without flashing a console window."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    si = None
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    return subprocess.run(args, creationflags=flags, startupinfo=si, **kw)


def kill_tree(pid: int) -> None:
    """Windows 上结束进程树 (pnpm -> node 子进程), 忽略失败。"""
    if os.name != "nt":
        return
    hidden_run(["taskkill", "/PID", str(pid), "/T", "/F"])


def _kill_proc_tree(p) -> None:
    """结束进程树并等待其退出 (最多 ~6s)。

    taskkill 是异步的 (发出终止信号后进程还需时间退出, 尤其网络连接中的
    git/ssh): 不等待就清理目录会撞文件锁, 下次启动 clone 会失败。"""
    try:
        kill_tree(p.pid)
    except Exception:
        pass
    try:
        p.wait(timeout=6)
    except Exception:
        # 进程树未在限时内退出 (卡在 I/O): 再杀一次兜底
        try:
            kill_tree(p.pid)
        except Exception:
            pass


def get_workspace_fingerprint() -> str | None:
    """后端源码状态指纹 (纯内容级, 不含 commit 号)。

    指纹 = sha256(HEAD 树 + 工作区对 HEAD 的内容改动 + untracked 内容):
      - `git ls-tree -r HEAD`: HEAD 树 (每个 tracked 文件的 blob 哈希);
      - `git diff HEAD --raw`: 工作区相对 HEAD 的改动, 每个条目带
        new blob 哈希 = 工作区实际内容, 因此同一文件改两次指纹会变
        (只靠 porcelain 状态行会漏判);
      - `git ls-files --others --exclude-standard` 列出的 gitignore
        之外 untracked 文件, 逐个按内容哈希。
    ignored 产物 (如 lib/) 不参与, 不会误触发构建。"""
    import hashlib

    def _out(args: list[str]) -> str | None:
        try:
            r = hidden_run(args, cwd=str(SOURCE), capture_output=True, text=True)
            return r.stdout if r.returncode == 0 else None
        except OSError:
            # git 完全不可用 (既无便携 git 也无系统 git): 指纹算不出, 返回 None
            return None

    tree = _out([_git_bin(), "ls-tree", "-r", "HEAD"])
    diff = _out([_git_bin(), "diff", "HEAD", "--raw"])
    if tree is None or diff is None:
        return None
    parts = [tree, diff]
    # gitignore 之外的 untracked 文件: 列出并逐个做内容哈希
    try:
        r = hidden_run([_git_bin(), "ls-files", "--others", "--exclude-standard", "-z"],
                       cwd=str(SOURCE), capture_output=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    for name in r.stdout.decode("utf-8", "replace").split("\0"):
        if not name:
            continue
        try:
            data = (SOURCE / name).read_bytes()
        except OSError:
            continue
        parts.append(name + "\x00" + hashlib.sha256(data).hexdigest())
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def get_last_fingerprint() -> str | None:
    """读取 last-build.txt 记录的指纹; 不存在/为空返回 None。"""
    if MARKER.exists():
        return MARKER.read_text(encoding="utf-8").strip() or None
    return None


def record_fingerprint(fp: str) -> None:
    BUILD_DIR.mkdir(exist_ok=True)
    MARKER.write_text(fp + "\n", encoding="utf-8")
    log(f"recorded build fingerprint {fp[:16]}...")


def needs_build() -> tuple[bool, str | None]:
    """返回 (是否需构建, 当前指纹)。

    判定: 编译产物缺失 / 读不到指纹 / 标记不存在 / 指纹与上次构建
    不一致 (gitignore 之外的后端源码有变化) -> 需要重新构建。"""
    cur_fp = get_workspace_fingerprint()
    last_fp = get_last_fingerprint()
    if not BACKEND_ENTRY.exists():
        log("compiled entry missing, rebuild needed")
        return True, cur_fp
    if cur_fp is None:
        # 无法计算指纹 (git 完全不可用: 便携 git 缺失/损坏且无系统 git)。
        # 编译产物已存在时信任打包的预构建产物, 不强制重建 —— 否则每次
        # 启动都跑 pnpm build, 无 node/pnpm 的机器直接失败退出。
        log("cannot fingerprint Source, trusting prebuilt artifacts (no rebuild)")
        return False, None
    if last_fp is None:
        log("no last-build marker, first run -> build")
        return True, cur_fp
    if cur_fp != last_fp:
        log("backend source changed since last build, rebuild needed")
        return True, cur_fp
    log("backend source unchanged, no rebuild needed")
    return False, cur_fp


def run_build() -> bool:
    log("starting build (visible console window)")
    # 弹独立控制台窗口显示构建进度, 失败时暂停以便查看错误; 记录进程供取消时终止
    # 显式 STARTUPINFO(SW_SHOWNORMAL=1) 确保窗口以正常状态显示:
    # 不传时若进程通过 shell=True 创建会带 SW_HIDE; 打包 windowed exe 场景
    # 下显式指定更稳妥, 构建窗口必须可见 (构建耗时较长, 用户要看进度)。
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 1  # SW_SHOWNORMAL
    p = subprocess.Popen(_build_cmd(), cwd=str(SOURCE), env=_node_env(),
                         creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si)
    _ACTIVE["proc"] = p
    try:
        p.wait()
    finally:
        _ACTIVE["proc"] = None
    ok = p.returncode == 0
    log(f"build finished, ok={ok}")
    return ok


def _repo_valid() -> bool:
    """SOURCE 是否为完整有效的 git 仓库 (clone 已完成)。

    仅看 package.json 不够: clone 中途取消时 package.json 可能已落地,
    但 .git 不完整 / 缺文件, 后续 install/build 会出错。必须同时满足
    package.json 存在 + .git 存在 + git rev-parse 能跑通。"""
    if not (SOURCE / "package.json").is_file():
        return False
    if not (SOURCE / ".git").is_dir():
        return False
    try:
        flags, si = _no_window_startup()
        r = subprocess.run(
            [_git_bin(), "-C", str(SOURCE), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30,
            creationflags=flags, startupinfo=si)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except OSError:
        return False


def _git_worktree_ok() -> bool:
    """SOURCE 内 git 是否可用 (rev-parse 能跑通), 不要求 package.json。

    用于区分两类残留: "可断点续传的半成品 clone"(.git 有效, 保留接着拉)
    与"完全无效残留"(无有效 .git, 需删除重来)。"""
    if not (SOURCE / ".git").is_dir():
        return False
    try:
        flags, si = _no_window_startup()
        r = subprocess.run(
            [_git_bin(), "-C", str(SOURCE), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30,
            creationflags=flags, startupinfo=si)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except OSError:
        return False


def _clone_repo() -> bool:
    """首次安装: 确保 SOURCE 是完整可用的官方仓库克隆 (支持断点续传)。

    三段式: ①确保 .git 与 origin remote (缺失则 git init + config);
    ②git fetch --depth 50 官方 master (对象级增量: 已下载对象跳过,
    中断后下次启动接着拉, 不重复下载, 也不删除残留); ③git checkout
    检出工作区 (detached HEAD, 强制覆盖)。带 SSH->HTTPS / 系统git->
    便携git / 直连->系统代理 回退; 用户取消 (splash 关闭) 后立即终止;
    fetch 失败保留 .git 供下次续传。"""
    import queue as _queue
    import re as _re
    import shutil

    def _clean_residue() -> bool:
        """确保 SOURCE 可安全使用: 完整仓库直接放行; 半成品 git 仓库
        (.git 有效) 保留用于断点续传; 只有完全无效 (无有效 .git) 的
        残留才删除 (失败重试, 等待文件锁释放)。"""
        if not SOURCE.is_dir():
            return True
        if _repo_valid():
            return True
        if _git_worktree_ok():
            log("incomplete repo found (.git ok), keep for resume fetch")
            return True
        for attempt in range(5):
            log(f"removing invalid residue (attempt {attempt + 1}/5): {SOURCE}")
            try:
                shutil.rmtree(str(SOURCE))
            except Exception as ex:
                log(f"residue cleanup failed: {ex}")
            if not SOURCE.exists():
                log("invalid residue removed")
                return True
            time.sleep(1.0)  # 等待文件锁释放 (刚杀死的 git/ssh 进程)
        log("residue cleanup FAILED: directory still exists after retries")
        return False

    def _fetch_progress(line: str) -> tuple[float, str] | None:
        """解析 git fetch 进度行 -> (splash 进度 5..30, 提示文字); 无关行 None。

        进度映射 (完成后 main 从 30 进入依赖安装, 不倒退):
        连接 5; Receiving 5..26; Resolving 26..27; 检出 27..30。"""
        line = line.strip()
        if not line:
            return None
        if "Receiving objects" in line:
            m = _re.search(r"(\d+)%", line)
            if m:
                pct = int(m.group(1))
                return 5.0 + 21.0 * pct / 100.0, f"正在下载代码 {pct}%…"
        if "Resolving deltas" in line:
            m = _re.search(r"(\d+)%", line)
            if m:
                pct = int(m.group(1))
                return 26.0 + 1.0 * pct / 100.0, f"正在解析增量 {pct}%…"
        if "Checking out files" in line or "Updating files" in line:
            m = _re.search(r"(\d+)%", line)
            if m:
                pct = int(m.group(1))
                return 27.0 + 3.0 * pct / 100.0, f"正在检出文件 {pct}%…"
        return None

    def _run_fetch(cmd: list[str], timeout: float) -> tuple[int, str]:
        """执行 git fetch: 逐行解析 stderr 进度更新 splash, 带超时/取消。
        返回 (returncode, 错误输出尾部); 取消/超时时已杀进程树。"""
        flags, si = _no_window_startup()
        try:
            # --progress 强制进度输出到 stderr (管道模式 git 默认不输出);
            # stdout 无内容, 直接丢弃
            p = subprocess.Popen(cmd, cwd=str(BASE),
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True,
                                 encoding="utf-8", errors="replace",
                                 creationflags=flags, startupinfo=si)
        except OSError as ex:
            return -1, str(ex)
        _ACTIVE["proc"] = p
        q = _queue.Queue()

        def _reader(p=p, q=q) -> None:
            try:
                for line in p.stderr:
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        rc = None
        err_buf = []
        deadline = time.time() + timeout
        try:
            while True:
                if _ACTIVE["cancel"]:
                    _kill_proc_tree(p)
                    return -1, "cancelled"
                remain = deadline - time.time()
                if remain <= 0:
                    _kill_proc_tree(p)
                    return -1, f"fetch timeout ({timeout}s)"
                try:
                    line = q.get(timeout=min(remain, 0.5))
                except _queue.Empty:
                    continue
                if line is None:
                    break
                prog = _fetch_progress(line)
                if prog is not None:
                    _splash_set_progress(prog[0], prog[1])
                err_buf.append(line)
            rc = p.wait(timeout=10)
        except Exception as ex:
            log(f"fetch wait failed: {ex}")
        finally:
            _ACTIVE["proc"] = None
        if rc == 0:
            return 0, ""
        return rc, "".join(err_buf).strip()[-500:]

    if not _clean_residue():
        return False
    candidates: list[str] = []
    if _system_git_available():
        candidates.append("git")
    if PORTABLE_GIT.is_file():
        candidates.append(str(PORTABLE_GIT))
    if not candidates:
        candidates = ["git"]
    urls = [REPO_URL_SSH, REPO_URL_HTTPS]

    # --- 1. 确保 .git 与 origin remote (断点续传基础) ---
    SOURCE.mkdir(parents=True, exist_ok=True)
    if not (SOURCE / ".git").is_dir():
        log(f"initializing git repo at {SOURCE}")
        flags, si = _no_window_startup()
        r = subprocess.run([_git_bin(), "init", "-q", str(SOURCE)],
                           capture_output=True, text=True, timeout=60,
                           creationflags=flags, startupinfo=si)
        if r.returncode != 0:
            log("git init failed: " + (r.stderr or "")[-300:])
            return False
    _git(["config", "remote.origin.url", REPO_URL_SSH])
    _git(["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"])

    # 清理中断 fetch/clone 留下的不完整 pack (有 .pack 无对应 .idx):
    # git negotiation 读到坏 pack 会直接报错, 导致"拉一小点就失败"
    try:
        pack_dir = SOURCE / ".git" / "objects" / "pack"
        if pack_dir.is_dir():
            for pf in pack_dir.glob("*.pack"):
                if not pf.with_suffix(".idx").exists():
                    pf.unlink(missing_ok=True)
                    log(f"removed incomplete pack: {pf.name}")
    except Exception as ex:
        log(f"incomplete pack cleanup failed: {ex}")

    # 清理 git 残留锁文件 (shallow.lock / index.lock / refs/**/*.lock):
    # 上次 fetch 被取消/崩溃时可能留下, 不清理会导致后续所有 fetch 失败
    # ("Another git process seems to be running in this repository")
    try:
        git_dir = SOURCE / ".git"
        removed = 0
        if git_dir.is_dir():
            for lock in git_dir.glob("*.lock"):
                lock.unlink(missing_ok=True)
                removed += 1
            refs = git_dir / "refs"
            if refs.is_dir():
                for lock in refs.rglob("*.lock"):
                    lock.unlink(missing_ok=True)
                    removed += 1
        if removed:
            log(f"removed {removed} stale git lock file(s)")
    except Exception as ex:
        log(f"git lock cleanup failed: {ex}")

    # --- 2. fetch 官方 master (增量续传: 已下载对象跳过) ---
    _splash_set_progress(5, "正在连接远程仓库…")
    last_err = ""
    fetched = False
    for ui, url in enumerate(urls):
        if _ACTIVE["cancel"]:
            log("fetch aborted by user")
            return False
        for idx, bin_ in enumerate(candidates):
            if _ACTIVE["cancel"]:
                log("fetch aborted by user")
                return False
            for proxy in [None] + list(_git_proxy_candidates()):
                if _ACTIVE["cancel"]:
                    log("fetch aborted by user")
                    return False
                # 注意: -c 是 git 全局选项, 必须放在子命令 (fetch) 之前!
                # clone 子命令自带 -c 选项可以放后面, 但 fetch 不认 -c
                # (会报 "error: unknown switch `c'" 并打印 usage)。
                cmd = [bin_, "-C", str(SOURCE)]
                if proxy:
                    cmd += ["-c", "http.proxy=" + proxy]
                if url == REPO_URL_SSH:
                    cmd += ["-c", "core.sshCommand=ssh -o StrictHostKeyChecking=accept-new"]
                cmd += ["fetch", "--depth", "50", "--progress", "--no-tags",
                        url, "master:refs/remotes/origin/master"]
                # SSH 未配 key/被墙时常卡在连接阶段: 短超时快速回退 HTTPS
                # (45s: 用户网络 SSH 握手常需 30s+, 超过基本没戏)
                timeout = 45 if url == REPO_URL_SSH else 1800
                rc, err = _run_fetch(cmd, timeout)
                if _ACTIVE["cancel"]:
                    log("fetch aborted by user")
                    return False
                if rc == 0:
                    fetched = True
                    log(f"fetched official repo (url={url}, git={bin_}, proxy={proxy or 'direct'})")
                    break
                last_err = err or f"rc={rc}"
                log(f"fetch attempt (url={url}, git={bin_}, proxy={proxy or 'direct'}) failed: {last_err}")
            if fetched:
                break
        if fetched:
            break
    if not fetched:
        # fetch 失败: 保留 .git (下次启动断点续传), 不删除
        log(f"fetch failed: {last_err}")
        # 顺手清理本次尝试可能留下的 lock (如连接中断), 保证下次启动能继续
        try:
            git_dir = SOURCE / ".git"
            if git_dir.is_dir():
                for lock in git_dir.glob("*.lock"):
                    lock.unlink(missing_ok=True)
                refs = git_dir / "refs"
                if refs.is_dir():
                    for lock in refs.rglob("*.lock"):
                        lock.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    # --- 3. checkout 检出工作区 (detached HEAD, 强制覆盖) ---
    _splash_set_progress(27, "正在检出文件…")
    co = _git(["checkout", "-q", "-f", "origin/master"], timeout=300)
    if co[0] != 0:
        log("checkout failed: " + (co[2].strip() or "unknown")[:300])
        return False
    if not _repo_valid():
        log("repo invalid after checkout")
        return False
    log("official repo ready (resume-safe)")
    return True


def _install_deps() -> bool:
    """首次安装: pnpm install (内嵌 pnpm + node), 依赖 store 放 data 目录不占 C 盘。

    用 list 模式直接调 pnpm (不经 cmd /S /c): 后者嵌套引号会把
    --store-dir "path with spaces" 的结尾引号解析给 pnpm, 导致
    mkdir '...pnpm-store"\v11' ENOENT 失败。"""
    flags, si = _no_window_startup()
    p = subprocess.Popen(
        _pnpm_list(["install", "--config.confirmModulesPurge=false",
                    "--store-dir", str(DATA_DIR / "pnpm-store")]),
        cwd=str(SOURCE), env=_node_env(),
        stdin=subprocess.PIPE,  # 自动应答任何交互确认 (默认 y, 不弹提示)
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=flags, startupinfo=si)
    _ACTIVE["proc"] = p
    try:
        try:
            out, err = p.communicate(input="y\n", timeout=1800)
        except subprocess.TimeoutExpired:
            try:
                kill_tree(p.pid)
            except Exception:
                pass
            out, err = "", "pnpm install timeout"
    finally:
        _ACTIVE["proc"] = None
    ok = p.returncode == 0 and (SOURCE / "node_modules" / ".modules.yaml").is_file()
    if not ok:
        # 输出可能落在 stdout 或 stderr (pnpm 错误常在 stdout 打印)
        log(f"pnpm install failed (rc={p.returncode}): {(err or '')[-800:]} {(out or '')[-800:]}")
    else:
        log("pnpm install OK")
    return ok


def start_backend() -> subprocess.Popen | None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    si = None
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    p = subprocess.Popen(
        _start_cmd(), cwd=str(SOURCE), env=_node_env(),
        creationflags=flags, startupinfo=si,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # 登记为当前活动子进程: splash 关闭按钮 (_cancel_startup) 能直接
    # 杀掉等待就绪阶段的后端, 不依赖 main 循环的检查时机。
    _ACTIVE["proc"] = p
    return p


# ==================== 强相关: Job Object (KILL_ON_JOB_CLOSE) ====================
# 需求: 只要本进程结束 (含任务管理器强杀/崩溃), 后端必须跟着死。
# 实现: 创建带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 的 Job, 把后端进程
# (node + 其全部子进程树) 放进去; Job 句柄由本进程持有, 进程退出时系统
# 自动关闭句柄 -> 内核立即终止 Job 内所有进程。这是进程级保证, 不依赖
# 任何清理代码能否执行 (正常退出/强杀/崩溃都一样生效)。

# --- Job Object 结构 (ctypes, 不依赖 pywin32) ---
class _LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def _create_kill_job() -> int | None:
    """创建 KILL_ON_JOB_CLOSE 的 Job, 返回句柄 (None 表示失败)。"""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        log(f"CreateJobObject failed: {ctypes.get_last_error()}")
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        log(f"SetInformationJobObject failed: {ctypes.get_last_error()}")
        _kernel32.CloseHandle(job)
        return None
    log("kill-on-close job created")
    return job


def _assign_pid_to_job(job: int | None, pid: int) -> bool:
    """把指定 PID 进程放入 Job (其后续子进程自动继承 Job 成员身份)。"""
    if not job:
        return False
    h = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not h:
        log(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        return False
    try:
        ok = bool(_kernel32.AssignProcessToJobObject(job, h))
        if not ok:
            log(f"AssignProcessToJobObject({pid}) failed: {ctypes.get_last_error()}")
        return ok
    finally:
        _kernel32.CloseHandle(h)


# ==================== 全局应用状态 (托盘/窗口/退出) ====================
_MAIN_WINDOW = None    # webview 窗口对象
_MAIN_FORM = None      # WinForms form (window.native)
_ALLOW_CLOSE = False   # True 后关闭窗口才真正退出 (托盘"退出"置位)
_TRAY = None           # NotifyIcon 保活引用
_JOB_HANDLE = None     # Job Object 句柄 (KILL_ON_JOB_CLOSE)

# 单实例: 命名 Mutex 判重 + 命名 Event 通知已有实例显示窗口
_SINGLE_INSTANCE_MUTEX = None
_SHOW_EVENT = None
_SINGLE_INSTANCE_NAME = r"Local\DSH_Desktop_SingleInstance"
_SHOW_WINDOW_EVENT_NAME = r"Local\DSH_Desktop_ShowWindow"
_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_EVENT_MODIFY_STATE = 0x0002


def _find_listener_pid(port: int) -> int | None:
    """netstat 找到监听 port 的 PID (无则 None)。"""
    r = hidden_run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    target = f":{port}"
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            if parts[1].endswith(target):
                try:
                    return int(parts[4])
                except ValueError:
                    continue
    return None


def _is_our_backend(pid: int) -> bool:
    """判断 PID 是否是本应用的后端 (命令行含 bin.js)。"""
    try:
        r = hidden_run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True)
        cmd = (r.stdout or "").strip()
    except Exception:
        return False
    return "bin.js" in cmd


def _hide_main_window() -> None:
    """窗口隐藏到系统托盘 (程序与后端继续运行)。"""
    form = _MAIN_FORM
    if form is None:
        return
    try:
        form.Hide()
        log("window hidden to tray")
        try:
            from System.Windows.Forms import ToolTipIcon
            if _TRAY is not None:
                _TRAY.ShowBalloonTip(1500, "DSH Desktop",
                                     "已最小化到托盘, 双击图标恢复窗口",
                                     ToolTipIcon.Info)
        except Exception:
            pass
    except Exception as ex:
        log(f"hide window failed: {ex}")


def _show_main_window() -> None:
    """恢复窗口到前台: 托盘"显示窗口" / 第二实例触发。
    可能在后台线程被调用 (第二实例信号线程), 统一封送到 UI 线程。"""
    form = _MAIN_FORM
    if form is None:
        return

    def _do() -> None:
        try:
            form.Show()
            form.Activate()
            try:
                hwnd = form.Handle.ToInt32()
                user32 = ctypes.windll.user32
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            except Exception:
                pass
            log("window restored (tray / second instance)")
        except Exception as ex:
            log(f"show window failed: {ex}")

    try:
        from System import Action
        if form.InvokeRequired:
            form.Invoke(Action(_do))
        else:
            _do()
    except Exception as ex:
        log(f"show window marshal failed: {ex}")


def _quit_application() -> None:
    """托盘"退出": 真正退出 (后端由退出清理 + Job 兜底保证关闭)。"""
    global _ALLOW_CLOSE
    _ALLOW_CLOSE = True
    log("tray quit requested")
    try:
        if _TRAY is not None:
            _TRAY.Visible = False
            _TRAY.Dispose()
    except Exception as ex:
        log(f"tray dispose failed: {ex}")
    try:
        if _MAIN_FORM is not None:
            _MAIN_FORM.Close()  # FormClosing 见 _ALLOW_CLOSE 不再拦截
        elif _MAIN_WINDOW is not None:
            _MAIN_WINDOW.destroy()
    except Exception as ex:
        log(f"quit close failed: {ex}")


def _setup_tray(form) -> None:
    """创建系统托盘图标 (WinForms NotifyIcon, 复用 pythonnet)。
    右键菜单: 显示窗口 / 退出; 双击 = 显示窗口。"""
    global _TRAY
    try:
        from System.Windows.Forms import (
            NotifyIcon, ContextMenuStrip, ToolStripMenuItem)
        from System.Drawing import Icon as _GIcon
    except Exception as ex:
        log(f"tray import failed: {ex}")
        return
    try:
        ni = NotifyIcon()
        icon_path = WINDOW_DIR / "deepseek娘.ico"
        if icon_path.is_file():
            ni.Icon = _GIcon(str(icon_path))
        ni.Text = "DSH Desktop"
        ni.Visible = True
        menu = ContextMenuStrip()
        show_item = ToolStripMenuItem("显示窗口")
        quit_item = ToolStripMenuItem("退出")
        show_item.Click += lambda s, e: _show_main_window()
        quit_item.Click += lambda s, e: _quit_application()
        menu.Items.Add(show_item)
        menu.Items.Add(quit_item)
        ni.ContextMenuStrip = menu
        ni.DoubleClick += lambda s, e: _show_main_window()
        _TRAY = ni  # 保活, 防 GC 导致图标消失
        log("tray icon created")
    except Exception as ex:
        log(f"tray setup failed: {ex}")


# ==================== 单实例 (命名 Mutex + 命名 Event) ====================
# 第一个实例持有命名 Mutex; 后续实例 CreateMutex 返回 ERROR_ALREADY_EXISTS,
# 通过命名 Event 通知第一个实例"显示窗口"后立即退出 -> 重复启动 = 点显示窗口。

def _acquire_single_instance() -> bool:
    """返回 True = 本实例是唯一实例; False = 已有实例在运行
    (已通知其显示窗口, 本实例应退出)。失败时降级为允许运行。"""
    global _SINGLE_INSTANCE_MUTEX, _SHOW_EVENT
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.SetLastError(0)
    m = _kernel32.CreateMutexW(None, True, _SINGLE_INSTANCE_NAME)
    err = ctypes.get_last_error()
    if not m:
        log(f"CreateMutex failed: {err}, running without single-instance guard")
        return True
    if err == _ERROR_ALREADY_EXISTS:
        # 已有实例: 通知其显示窗口, 本实例退出
        _kernel32.OpenEventW.restype = wintypes.HANDLE
        _kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        _kernel32.SetEvent.restype = wintypes.BOOL
        _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        ev = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _SHOW_WINDOW_EVENT_NAME)
        if ev:
            _kernel32.SetEvent(ev)
            _kernel32.CloseHandle(ev)
            log("another instance running; signaled it to show window, exiting")
        else:
            log(f"another instance running but show-event not found "
                f"(err={ctypes.get_last_error()}), exiting")
        _kernel32.CloseHandle(m)
        return False
    _SINGLE_INSTANCE_MUTEX = m  # 持有到进程退出 (句柄释放 = 互斥释放)
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.CreateEventW.argtypes = [
        wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _SHOW_EVENT = _kernel32.CreateEventW(None, False, False, _SHOW_WINDOW_EVENT_NAME)
    if _SHOW_EVENT:
        log("single-instance mutex + show-window event acquired")
    else:
        log(f"CreateEvent failed: {ctypes.get_last_error()}, "
            f"second-instance activation disabled")
    return True


def _watch_show_window_event() -> None:
    """后台线程: 等待第二实例的"显示窗口"信号, 触发时恢复窗口 (daemon, 随进程退出)。"""
    if not _SHOW_EVENT:
        return
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    while True:
        r = _kernel32.WaitForSingleObject(_SHOW_EVENT, 0xFFFFFFFF)  # INFINITE
        if r == _WAIT_OBJECT_0:
            log("show-window signal received from second instance")
            _show_main_window()


# ==================== 原生自绘标题栏 (WinForms) ====================

class WindowApi:
    """js_api 暴露给网页: set_theme(dark) 让标题栏配色跟随应用主题。"""

    def __init__(self) -> None:
        self._titlebar: TitleBar | None = None

    def bind(self, titlebar: "TitleBar") -> None:
        self._titlebar = titlebar

    def set_theme(self, dark: bool) -> None:
        bar = self._titlebar
        if bar is None:
            return
        try:
            from System import Action
            bar.form.Invoke(Action(lambda: bar.apply_theme(bool(dark))))
        except Exception as e:
            log(f"set_theme failed: {e}")


class TitleBar:
    """窗口自绘标题栏 (Resonix 风格, 无独立标题栏控件): 左侧应用图标,
    右侧最小化/最大化/关闭, 直接绘制在窗口客户区顶部。

    全部在 UI 线程使用 (install 由 shown 事件经 form.Invoke 调度)。
    拖动/双击最大化走 Form 鼠标事件 (ReleaseCapture + WM_NCLBUTTONDOWN/HTCAPTION),
    边缘缩放由子类化 WndProc 的 WM_NCHITTEST 交给系统处理 (含 WebView2
    子窗口转发), 窗口在截图/无障碍视角下是一个整体。
    """

    def __init__(self, window) -> None:
        self._window = window
        self.form = None
        self._scale = 1.0
        self._tb_h = TITLEBAR_HEIGHT
        self._btn_w = BTN_WIDTH
        # 初始主题跟随配置偏好 (settings.yaml ui-theme.preference), 不是系统主题:
        # 用户配置 light/dark 与系统不一致时, 标题栏/边框首帧即用配置色。
        self._dark = resolve_initial_dark()
        # 主题色 token: 直接从前端 CSS 读取, 不硬编码颜色
        _tokens = read_theme_tokens()
        self._dark_bg = _tokens[0] if _tokens else (21, 21, 23)
        self._light_bg = _tokens[1] if _tokens else (249, 250, 251)
        self._hover = -1
        self._pressed = -1
        self._icon_image = None
        self._chrome_ref = None  # 保活 WndProc 回调, 防 GC
        self._maximized = False  # 手动管理 (最大化=工作区, 还原=原 rect)
        self._restore_bounds = None  # 最大化前的窗口边界 (System.Drawing.Rectangle)
        self._form_hwnd = 0      # 拖动/双击最大化用
        self._last_ncr = None    # DWM NCRENDERING 上次状态 (None=未设置)
        # 全屏拖动状态: 按下不还原, 移动超阈值 (真正拖动) 才还原并跟随
        self._drag_pending = False
        self._drag_restored = False
        self._drag_start = None    # 按下点屏幕坐标 (GPoint)
        self._drag_offset_x = 0    # 还原后鼠标在窗口内的抓取偏移
        self._drag_offset_y = 0
        # 升级通知状态: 后台线程检测到官方新提交后 set_update_info 填充
        self._update_info = None       # check_for_update 结果 dict / None
        self._update_rect = None       # 通知文字命中区 (RectangleF, None=不显示)
        self._update_hover = False
        self._update_pressed = False
        self._upd_font = None          # 通知文字字体 (缓存)
        self._upd_thread_started = False
        self._update_dialog_open = False
        self._update_seen_hash = _read_update_seen()  # 已读的最新提交 (红点判断)

    def install(self) -> None:
        from System.Windows.Forms import DockStyle, ControlStyles
        from System.Drawing import Icon

        form = self._window.native
        self.form = form
        hwnd = form.Handle.ToInt32()
        self._form_hwnd = hwnd
        user32 = ctypes.windll.user32
        self._scale = max(1.0, user32.GetDpiForWindow(hwnd) / 96.0)
        self._tb_h = int(TITLEBAR_HEIGHT * self._scale)
        self._btn_w = int(BTN_WIDTH * self._scale)
        # 升级通知文字字体 (9pt, GDI+ 按屏幕 DPI 自动缩放; 微软雅黑缺失时回落系统字体)
        try:
            from System.Drawing import Font, FontStyle, SystemFonts
            self._upd_font = Font("Microsoft YaHei UI", 9.0, FontStyle.Regular)
        except Exception:
            try:
                from System.Drawing import SystemFonts
                self._upd_font = SystemFonts.MessageBoxFont
            except Exception:
                self._upd_font = None
        # DWM 属性: 边框颜色跟随主题 + 最大化时禁用非客户区渲染(阴影/1px 边框)
        from ctypes import wintypes as _wt
        self._dwmapi = ctypes.WinDLL("dwmapi")
        self._dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        self._dwmapi.DwmSetWindowAttribute.argtypes = [
            _wt.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        # shadow=False 后恢复 Win11 圆角 (DWMWA_WINDOW_CORNER_PREFERENCE=33, ROUND=2)
        try:
            corner = ctypes.c_int(2)
            self._dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
            log("window corner preference set to ROUND")
        except Exception as ex:
            log(f"corner preference failed: {ex}")
        # DWM 边框色 = 背景色 (DWMWA_BORDER_COLOR=34), 让 1px 边框隐形
        self._apply_border_color()
        log(f"titlebar install: hwnd={hwnd} scale={self._scale:.2f} "
            f"height={self._tb_h} btn={self._btn_w}")

        # 左侧应用图标: 优先 PNG (与 ico 同源, 平滑缩放), 后备 ico 32x32 帧
        self._icon_image = None
        self._icon_kind = None  # "png" | "ico"
        png_path = WINDOW_DIR / "deepseek娘.png"
        if png_path.is_file():
            try:
                from System.Drawing import Image
                self._icon_image = Image.FromFile(str(png_path))
                self._icon_kind = "png"
                log(f"titlebar icon loaded (png): {png_path}")
            except Exception as e:
                log(f"titlebar icon png load failed: {e}")
        if self._icon_image is None:
            icon_path = WINDOW_DIR / "deepseek娘.ico"
            if icon_path.is_file():
                try:
                    self._icon_image = Icon(str(icon_path), 32, 32)
                    self._icon_kind = "ico"
                    log(f"titlebar icon loaded (ico): {icon_path}")
                except Exception as e:
                    log(f"titlebar icon ico load failed: {e}")

        # 标题栏直接自绘在窗口上 (无 Panel 子控件): 截图/无障碍识别整个窗口为
        # 一个整体 (Resonix 风格), 不再出现"标题栏/边框/内容"多个独立控件。
        # 背景 = 窗口背景 (主题色), 绘制与鼠标事件全部挂 Form 级。
        # Form 双缓冲: 标题栏自绘重绘不闪烁 (原 Panel 自带 OptimizedDoubleBuffer)。
        form.SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint
                      | ControlStyles.OptimizedDoubleBuffer, True)
        form.BackColor = self._color("bg")
        form.Paint += self._on_paint
        form.MouseMove += self._on_mouse_move
        form.MouseLeave += self._on_mouse_leave
        form.MouseDown += self._on_mouse_down
        form.MouseUp += self._on_mouse_up

        # WebView2 下移, 顶部让给自绘标题栏 (手动布局, 避免 Dock 顺序坑)
        webview_ctrl = form.Controls[0]
        webview_ctrl.Dock = getattr(DockStyle, "None")
        self._webview_ctrl = webview_ctrl

        def layout(_s=None, _e=None) -> None:
            w = form.ClientSize.Width
            h = form.ClientSize.Height
            # WebView2 比窗口小一圈: 左右下各留 EDGE_PADDING 窗口客户区 (主题色, 近不可见)。
            # 这样窗口边缘露出父窗口客户区, WM_NCHITTEST 直达父窗口触发系统缩放;
            # 若 WebView2 铺满, 边缘消息被其内部子窗口吞掉, 缩放失效。
            p = int(EDGE_PADDING * self._scale)
            webview_ctrl.SetBounds(p, self._tb_h, w - 2 * p, h - self._tb_h - p)
            self._invalidate_titlebar()  # 最大化/还原状态切换时刷新标题栏按钮
            # WebView2 移动/缩放后, 其四周露出的窗体背景条 (左右下各 p px) 可能
            # 残留旧内容 (白底/桌面): 强制重绘整圈边缘, 保证始终是主题色。
            try:
                from System.Drawing import Rectangle
                form.Invalidate(Rectangle(0, self._tb_h, p, h - self._tb_h - p))      # 左
                form.Invalidate(Rectangle(w - p, self._tb_h, p, h - self._tb_h - p))  # 右
                form.Invalidate(Rectangle(0, h - p, w, p))                            # 下
            except Exception:
                pass
            # WebView2 初始化会重置 DWM 属性: 每次 Resize 都重设 NCR,
            # 确保全屏时边框渲染被禁用 (overscan 方案下边框本就在屏幕外)
            self._apply_ncr_state()

        form.Resize += layout
        layout()
        log(f"titlebar layout: client={form.ClientSize.Width}x{form.ClientSize.Height}")

        # WebView2 初始化 (异步, 数秒) 会重置 DWM 属性:
        # 用 daemon 线程多次延迟重设 NCR, 确保全屏无边框稳定生效。
        def _retry_dwm() -> None:
            try:
                from System import Action

                def _apply() -> None:
                    self._apply_ncr_state()

                for delay in (2.0, 4.0, 8.0):
                    time.sleep(delay)
                    form.Invoke(Action(_apply))
            except Exception as ex:
                log(f"dwm retry failed: {ex}")

        threading.Thread(target=_retry_dwm, daemon=True).start()

        # 文档级主题背景注入: 消灭启动/导航白屏 (不改前端代码)
        self._inject_doc_background()

        self._install_frame_chrome(hwnd)

    def _invalidate_titlebar(self) -> None:
        """只重绘自绘标题栏区域 (避免全窗口 Invalidate 引起按钮符号闪烁)。"""
        try:
            from System.Drawing import Rectangle
            w = self.form.ClientSize.Width
            self.form.Invalidate(Rectangle(0, 0, w, self._tb_h))
        except Exception:
            pass

    def _apply_border_color(self) -> None:
        """DWM 边框色 (DWMWA_BORDER_COLOR=34) = 当前主题背景色, 让 1px 边框隐形。"""
        try:
            bg = self._dark_bg if self._dark else self._light_bg
            col = ctypes.c_int((bg[2] << 16) | (bg[1] << 8) | bg[0])  # COLORREF 0x00BBGGRR
            self._dwmapi.DwmSetWindowAttribute(
                self._form_hwnd, 34, ctypes.byref(col), ctypes.sizeof(col))
        except Exception as ex:
            log(f"border color set failed: {ex}")

    def apply_theme(self, dark: bool) -> None:
        """主题切换 (由网页 js_api 或初始系统主题调用, 需在 UI 线程)。
        同步: 窗口背景 (标题栏自绘同色)、DWM 边框色、页面 html/body 背景。"""
        self._dark = bool(dark)
        if self.form is not None:
            # 窗口背景 = 标题栏背景 (自绘, 无独立控件)
            self.form.BackColor = self._color("bg")
            self._invalidate_titlebar()
        self._apply_border_color()
        self._update_doc_bg()

    # ---------- 文档级主题背景 (启动/导航白屏消除, 不改前端) ----------

    def _doc_bg_script(self) -> str:
        """注入到每个文档创建早期的脚本: 强制 html/body 背景 = 主题色。

        页面 CSS 由 JS 模块加载, 应用前 body 无背景 (浏览器默认白底), 造成
        启动白屏; SPA 从 Loading 切到主界面时根节点挂载前也有白闪间隙。
        这里在文档创建的最早期插入 <style> 用 !important 锁定 html/body
        背景, 任何白底间隙都被主题色盖住; 主题切换时 _update_doc_bg 更新。"""
        rgb = self._dark_bg if self._dark else self._light_bg
        return _dsh_doc_bg_script(rgb)

    def _inject_doc_background(self) -> None:
        """注册文档创建脚本 (AddScriptToExecuteOnDocumentCreatedAsync),
        并订阅 NavigationStarting 兜底首次导航, 再在 loaded 后补一次当前文档:
        三条路径保证任何导航阶段 html/body 背景都是主题色。

        全部在 UI 线程执行 (install 在 shown 事件): WinForms 控件属性
        CoreWebView2 不能在后台线程访问, 故不用轮询线程。"""
        try:
            wv = self._webview_ctrl
        except Exception as ex:
            log(f"doc background: no webview control: {ex}")
            return
        script = self._doc_bg_script()
        registered = [False]

        def _register() -> None:
            if registered[0]:
                return
            try:
                core = wv.CoreWebView2
                if core is None:
                    return
                core.AddScriptToExecuteOnDocumentCreatedAsync(script)
                registered[0] = True
                log("doc background script registered (AddScriptToExecuteOnDocumentCreatedAsync)")
            except Exception as ex:
                log(f"doc background register failed: {ex}")

        def _on_navigation_starting(_s=None, _e=None) -> None:
            # 导航开始后、文档创建前注册, 对该次导航的文档同样生效 (含首次导航)
            _register()

        def _on_init_completed(_s=None, _e=None) -> None:
            # UI 线程: CoreWebView2 初始化完成, 立即注册 + 订阅导航兜底
            try:
                core = wv.CoreWebView2
                if core is not None:
                    core.NavigationStarting += _on_navigation_starting
            except Exception as ex:
                log(f"doc background navstarting hook failed: {ex}")
            _register()

        # CoreWebView2 可能已完成初始化 (install 在 shown 事件), 也可能仍异步初始化中
        try:
            if wv.CoreWebView2 is not None:
                _on_init_completed()
            else:
                wv.CoreWebView2InitializationCompleted += _on_init_completed
        except Exception as ex:
            log(f"doc background init hook failed: {ex}")

        # loaded 后补一次当前文档 (以上机制赶不上首帧时的最终兜底)。
        # 注意: pywebview 的 loaded 事件在后台线程触发, CoreWebView2 只能在
        # UI 线程访问 (STA), 直接访问会跨线程阻塞等待 UI 线程 -> 与 UI 线程
        # 的 WebView2 初始化互锁 (窗体卡死但页面在动)。统一封送到 UI 线程。
        def _on_loaded() -> None:
            try:
                core = wv.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(self._doc_bg_script())
            except Exception as ex:
                log(f"doc background loaded-fallback failed: {ex}")

        def _on_loaded_marshaled() -> None:
            try:
                from System import Action
                if not wv.InvokeRequired:
                    _on_loaded()
                else:
                    wv.Invoke(Action(_on_loaded))
            except Exception as ex:
                log(f"doc background loaded marshal failed: {ex}")

        try:
            win = self._window
            win.events.loaded += _on_loaded_marshaled
        except Exception as ex:
            log(f"doc background loaded hook failed: {ex}")

    def _update_doc_bg(self) -> None:
        """主题切换时更新已加载文档的 html/body 背景 (fire-and-forget,
        不阻塞 UI 线程; 对后续新文档由 AddScriptToExecuteOnDocumentCreatedAsync
        以当时主题重新注入)。"""
        try:
            core = self._webview_ctrl.CoreWebView2
            if core is None:
                return
            rgb = self._dark_bg if self._dark else self._light_bg
            color = "rgb(%d,%d,%d)" % rgb
            js = (
                "(() => {"
                "const st = document.getElementById('__dsh_launcher_bg__');"
                "if (st) st.textContent = '" + _DSH_BG_SELECTORS
                + " { background-color: " + color + " !important; }';"
                "})()"
            )
            core.ExecuteScriptAsync(js)
        except Exception as ex:
            log(f"doc bg update failed: {ex}")

    def _apply_ncr_state(self) -> None:
        """始终禁用 DWM 非客户区渲染 (无边框无阴影, 去白线);
        圆角随最大化状态切换: 最大化=方形贴边(无灰框), 普通=圆角。
        最大化状态用 self._maximized (手动管理), 不用 IsZoomed。"""
        try:
            maximized = bool(self._maximized)
            # DWMWA_NCRENDERING_POLICY(2) = DISABLED(1): 去掉 DWM 画的 1px 边框线
            if self._last_ncr != 1:
                val = ctypes.c_int(1)
                hr = self._dwmapi.DwmSetWindowAttribute(
                    self._form_hwnd, 2, ctypes.byref(val), ctypes.sizeof(val))
                if hr == 0:
                    self._last_ncr = 1
                else:
                    log(f"dwm ncr set failed hr={hr:#x}")
            # DWMWA_WINDOW_CORNER_PREFERENCE(33): 1=DONOTROUND 2=ROUND
            corner = ctypes.c_int(1 if maximized else 2)
            self._dwmapi.DwmSetWindowAttribute(
                self._form_hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        except Exception as ex:
            log(f"dwm ncr exception: {ex}")

    # ---------- 绘制 ----------

    def _rgb(self, rgb: tuple[int, int, int]):
        from System.Drawing import Color as GColor
        return GColor.FromArgb(*rgb)

    def _color(self, key: str):
        return self._rgb(TITLEBAR_THEMES["dark" if self._dark else "light"][key])

    def _hit_button(self, x: int, y: int) -> int:
        """x/y (窗口客户坐标) -> 0=min 1=max 2=close, -1=无。

        按钮只存在于自绘标题栏高度内: y 超出标题栏 (落在 WebView2 内容区)
        一律返回 -1。鼠标事件挂在整个 Form 上 (子控件事件会冒泡), 若不检查
        y, 最大化时窗口铺满工作区, 鼠标在最右侧任意高度都会误命中关闭按钮。"""
        if y < 0 or y >= self._tb_h:
            return -1
        w = self.form.ClientSize.Width
        if x >= w - 3 * self._btn_w and x < w - 2 * self._btn_w:
            return 0
        if x >= w - 2 * self._btn_w and x < w - self._btn_w:
            return 1
        if x >= w - self._btn_w:
            return 2
        return -1

    def _on_paint(self, sender, e) -> None:
        from System.Drawing import Pen, Rectangle, SolidBrush
        g = e.Graphics
        h = self._tb_h  # 标题栏高度 (自绘区)
        w = self.form.ClientSize.Width
        c = TITLEBAR_THEMES["dark" if self._dark else "light"]
        s = self._scale

        # 先铺满整个客户区背景 (主题色): 窗体任何一次重绘都带上完整背景,
        # WebView2 边缘条 / resize / 移动露出的区域永远不会是白底或旧内容。
        g.Clear(self._rgb(c["bg"]))

        # 左侧应用图标 (无文字标题)
        if self._icon_image is not None:
            size = int(22 * s)
            x, y = 12 * s, (h - size) / 2
            if self._icon_kind == "png":
                from System.Drawing.Drawing2D import InterpolationMode
                old = g.InterpolationMode
                g.InterpolationMode = InterpolationMode.HighQualityBicubic
                g.DrawImage(self._icon_image, x, y, size, size)
                g.InterpolationMode = old
            else:
                g.DrawIconUnstretched(self._icon_image, Rectangle(
                    int(x), int(y), size, size))

        # 右侧三个窗口按钮
        maximized = self._maximized
        kinds = [0, 1 if not maximized else 2, 3]  # min / max|restore / close
        for idx, kind in enumerate(kinds):
            x0 = int(w - (3 - idx) * self._btn_w)
            cx = x0 + self._btn_w / 2
            cy = h / 2
            if self._pressed == idx:
                bg = c["close_active"] if idx == 2 else c["active"]
            elif self._hover == idx:
                bg = c["close_hover"] if idx == 2 else c["hover"]
            else:
                bg = None
            if bg is not None:
                g.FillRectangle(SolidBrush(self._rgb(bg)), x0, 0, self._btn_w, h)
            icon_rgb = (255, 255, 255) if idx == 2 and self._hover == idx else c["icon"]
            self._draw_icon(g, kind, cx, cy, icon_rgb)

        # 升级按钮: 最小化按钮左侧常驻 (灰色=无更新, 蓝色=有更新 + 红点)。
        # 点击: 有更新 -> 升级对话框 (更新日志+版本选择); 无更新 -> 已是最新提示。
        self._draw_update_notice(g, self._update_info)

    def _draw_update_notice(self, g, info) -> None:
        """绘制常驻"检查更新"按钮 (最小化按钮左侧, 无背景色):
        图标 + "检查更新"文字, 平时灰色; 有更新时变蓝色 + 红点 (点击一次后消失)。"""
        from System.Drawing import Pen, SolidBrush, RectangleF
        from System.Drawing.Drawing2D import SmoothingMode, LineCap
        from System.Drawing.Text import TextRenderingHint
        c = TITLEBAR_THEMES["dark" if self._dark else "light"]
        font = self._upd_font
        if font is None:
            return
        available = bool(info and info.get("available"))
        hover = bool(self._update_hover)
        if available:
            color = c["upd_hover"] if hover else c["upd"]
        else:
            color = c["upd_hover"] if hover else c["icon"]
        text = "检查更新"
        brush = SolidBrush(self._rgb(color))
        try:
            size = g.MeasureString(text, font)
            w = self.form.ClientSize.Width
            margin = 10.0 * self._scale
            right = w - 3 * self._btn_w - margin      # 最小化按钮左侧
            s = self._scale
            icon_w = 14.0 * s
            gap = 5.0 * s
            total_w = icon_w + gap + size.Width
            x_icon = right - total_w
            min_x = 48.0 * s                          # 应用图标右侧留白
            if x_icon < min_x:
                x_icon = min_x
                if x_icon + total_w > right:
                    self._update_rect = None           # 窗口太窄, 画不下
                    return
            cy = self._tb_h / 2.0
            # 图标: 向上箭头 (更新)
            g.SmoothingMode = SmoothingMode.AntiAlias
            pen = Pen(self._rgb(color), max(1.0, 1.4 * s))
            pen.StartCap = LineCap.Round
            pen.EndCap = LineCap.Round
            ix = x_icon + icon_w / 2.0
            try:
                g.DrawLine(pen, ix, cy + 4.0 * s, ix, cy - 2.5 * s)
                g.DrawLine(pen, ix, cy - 5.5 * s, ix - 3.2 * s, cy - 0.8 * s)
                g.DrawLine(pen, ix, cy - 5.5 * s, ix + 3.2 * s, cy - 0.8 * s)
            finally:
                pen.Dispose()
            # 文字 "检查更新"
            x_text = x_icon + icon_w + gap
            y_text = (self._tb_h - size.Height) / 2.0
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
            g.DrawString(text, font, brush, x_text, y_text)
            # 红点: 有更新且未读 (点击一次后消失, 持久化)
            if available and self._show_update_badge(info):
                r = 3.5 * s
                bx = x_icon + icon_w - 1.0 * s
                by = 5.0 * s
                red = SolidBrush(self._rgb((239, 68, 68)))
                try:
                    g.FillEllipse(red, bx - r, by - r, 2 * r, 2 * r)
                finally:
                    red.Dispose()
            self._update_rect = RectangleF(
                x_icon - 4.0 * s, 0,
                total_w + 8.0 * s, self._tb_h)
        finally:
            brush.Dispose()

    def _show_update_badge(self, info) -> bool:
        """红点是否显示: 有更新 且 最新提交 != 已读记录 (点击一次后消失)。"""
        latest = (info or {}).get("latest") or ""
        return bool(latest) and latest != self._update_seen_hash

    def _mark_update_seen(self, latest_hash: str) -> None:
        """记录已读 (打开升级对话框时调用), 红点消失并持久化到文件。"""
        self._update_seen_hash = latest_hash
        try:
            SEEN_MARKER.write_text(latest_hash + "\n", encoding="utf-8")
        except OSError as ex:
            log(f"update seen marker write failed: {ex}")
        self._invalidate_titlebar()

    def _show_updating_overlay(self) -> None:
        """切换版本期间: 向 webview 页面注入全屏覆盖层 (主题色跟随页面变量)。

        后台线程调用, 内部封送 UI 线程; CoreWebView2 只能 UI 线程访问。"""
        def _inject() -> None:
            try:
                core = self._webview_ctrl.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(_UPDATING_OVERLAY_JS)
                    log("updating overlay injected")
            except Exception as ex:
                log(f"updating overlay inject failed: {ex}")
        try:
            from System import Action
            self.form.Invoke(Action(_inject))
        except Exception as ex:
            log(f"updating overlay marshal failed: {ex}")

    def _hide_updating_overlay(self) -> None:
        """移除切换版本覆盖层 (UI 线程封送)。"""
        def _inject() -> None:
            try:
                core = self._webview_ctrl.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(_UPDATING_OVERLAY_HIDE_JS)
                    log("updating overlay removed")
            except Exception as ex:
                log(f"updating overlay hide failed: {ex}")
        try:
            from System import Action
            self.form.Invoke(Action(_inject))
        except Exception as ex:
            log(f"updating overlay hide marshal failed: {ex}")

    def _draw_icon(self, g, kind: int, cx: float, cy: float, rgb) -> None:
        """kind: 0=min 1=max 2=restore 3=close
        手绘窗口图标, 以按钮中心 (cx, cy) 垂直居中, 整体缩小一档。"""
        from System.Drawing import Pen
        from System.Drawing.Drawing2D import SmoothingMode, LineCap
        s = self._scale
        g.SmoothingMode = SmoothingMode.AntiAlias
        pen = Pen(self._rgb(rgb), max(1.0, 1.3 * s))
        pen.StartCap = LineCap.Round
        pen.EndCap = LineCap.Round
        if kind == 0:  # 最小化: 居中横线 8px (中心=cy, 与其他按钮图标统一)
            g.DrawLine(pen, cx - 4 * s, cy, cx + 4 * s, cy)
        elif kind == 1:  # 最大化: 对称空心方框 8x8
            g.DrawRectangle(pen, cx - 4 * s, cy - 4 * s, 8 * s, 8 * s)
        elif kind == 2:  # 还原: 后框 (左上) + 前框 (右下), 整体中心 = cy
            g.DrawRectangle(pen, cx - 4 * s, cy - 4 * s, 6 * s, 5 * s)
            g.DrawRectangle(pen, cx - 1.5 * s, cy - 1 * s, 6 * s, 5 * s)
            cover = Pen(self._color("bg"), max(1.0, 1.3 * s))
            # 前框左边穿过后框内部的部分用背景色覆盖
            g.DrawLine(cover, cx - 1.5 * s, cy - 1 * s, cx - 1.5 * s, cy + 1 * s)
            cover.Dispose()
        else:  # 关闭: × 对称 7px
            g.DrawLine(pen, cx - 3.5 * s, cy - 3.5 * s, cx + 3.5 * s, cy + 3.5 * s)
            g.DrawLine(pen, cx + 3.5 * s, cy - 3.5 * s, cx - 3.5 * s, cy + 3.5 * s)
        pen.Dispose()

    # ---------- 鼠标交互 ----------

    DRAG_THRESHOLD = 5  # 全屏按下后移动超过该像素才算"拖动" (单击不退出全屏)

    def _on_mouse_move(self, sender, e) -> None:
        # 全屏拖动: 移动超阈值才还原并跟随, 未超阈值 (单击) 保持全屏
        if self._drag_pending:
            try:
                from System.Drawing import Point as GPoint
                user32 = ctypes.windll.user32
                cur = self.form.PointToScreen(GPoint(e.X, e.Y))
                if not self._drag_restored:
                    if (abs(cur.X - self._drag_start.X) < self.DRAG_THRESHOLD
                            and abs(cur.Y - self._drag_start.Y) < self.DRAG_THRESHOLD):
                        return  # 还没开始拖动
                    # 还原并拖动: 按鼠标在全屏窗口中的百分比映射到还原窗口位置, 保持跟手
                    b = self._restore_bounds
                    full_w = self.form.Bounds.Width
                    full_h = self.form.Bounds.Height
                    px = b.Width * e.X / full_w if full_w else 0
                    py = b.Height * e.Y / full_h if full_h else 0
                    nx = int(cur.X - px)
                    ny = int(cur.Y - py)
                    self._maximized = False
                    user32.SetWindowPos(self._form_hwnd, 0, nx, ny, b.Width, b.Height,
                                        0x0004 | 0x0010)  # NOZORDER | NOACTIVATE
                    self._apply_ncr_state()
                    self._drag_restored = True
                    self._drag_offset_x = px
                    self._drag_offset_y = py
                    self._invalidate_titlebar()
                else:
                    # 已还原: 窗口跟随鼠标 (鼠标下的内容不跳)
                    nx = int(cur.X - self._drag_offset_x)
                    ny = int(cur.Y - self._drag_offset_y)
                    user32.SetWindowPos(self._form_hwnd, 0, nx, ny, 0, 0,
                                        0x0004 | 0x0010 | 0x0001)  # NOZORDER|NOACTIVATE|NOSIZE
                return
            except Exception as ex:
                log(f"fullscreen drag move failed: {ex}")
                return
        # 升级通知悬停: 高亮文字 + 手型光标 (仅在通知区域内)
        hit_u = self._hit_update(e.X, e.Y)
        if hit_u != self._update_hover:
            self._update_hover = hit_u
            try:
                from System.Windows.Forms import Cursors
                self.form.Cursor = Cursors.Hand if hit_u else Cursors.Default
            except Exception:
                pass
            self._invalidate_titlebar()
        idx = self._hit_button(e.X, e.Y)
        if idx != self._hover:
            self._hover = idx
            self._invalidate_titlebar()

    def _on_mouse_leave(self, sender, e) -> None:
        if self._hover != -1 or self._pressed != -1 or self._update_hover or self._update_pressed:
            self._hover = -1
            self._pressed = -1
            self._update_hover = False
            self._update_pressed = False
            self._invalidate_titlebar()

    def _on_mouse_down(self, sender, e) -> None:
        log(f"titlebar mousedown x={e.X} y={e.Y} clicks={e.Clicks}")
        # 升级通知区域: 按下进入按下态 (不触发窗口拖动/最大化)
        if self._hit_update(e.X, e.Y):
            self._update_pressed = True
            self._invalidate_titlebar()
            return
        idx = self._hit_button(e.X, e.Y)
        if idx != -1:
            self._pressed = idx
            self._invalidate_titlebar()
            return
        # 双击标题栏 → 最大化/还原 (不启动拖动)
        if e.Clicks == 2:
            self._toggle_maximize()
            return
        # 全屏时按下标题栏: 不立即还原, 捕获鼠标等真正拖动 (移动超阈值) 才还原;
        # 单击 (无移动) 松开后保持全屏, 符合"拖动/双击才退出全屏"。
        if self._maximized and self._restore_bounds is not None:
            try:
                from System.Drawing import Point as GPoint
                from ctypes import wintypes as _wt
                user32 = ctypes.windll.user32
                user32.SetCapture.argtypes = [_wt.HWND]  # 64 位 HWND, 防截断
                user32.SetCapture.restype = _wt.HWND
                self._drag_pending = True
                self._drag_restored = False
                self._drag_start = self.form.PointToScreen(GPoint(e.X, e.Y))
                user32.SetCapture(self._form_hwnd)  # 捕获后即使鼠标移出标题栏仍收 MouseMove/Up
            except Exception as ex:
                log(f"fullscreen drag start failed: {ex}")
            return
        # 普通窗口拖动: ReleaseCapture + WM_NCLBUTTONDOWN/HTCAPTION 让系统接管 move loop
        try:
            user32 = ctypes.windll.user32
            user32.ReleaseCapture()
            user32.SendMessageW(self._form_hwnd, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
        except Exception as ex:
            log(f"titlebar drag failed: {ex}")

    def _toggle_maximize(self) -> None:
        """手动最大化/还原 (不依赖系统 WM_GETMINMAXINFO):
        最大化 = 所在显示器工作区 (任务栏保留), 还原 = 之前的位置大小。
        必须先更新 self._maximized 再 SetWindowPos (SetWindowPos 同步触发
        Resize→layout, 边缘热区依据 _maximized 决定显隐)。"""
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32 = ctypes.windll.user32
        try:
            if self._maximized:
                b = self._restore_bounds
                self._maximized = False
                user32.SetWindowPos(self._form_hwnd, 0, b.X, b.Y, b.Width, b.Height,
                                    SWP_NOZORDER | SWP_NOACTIVATE)
            else:
                self._restore_bounds = self.form.Bounds
                wa = self._work_area()
                self._maximized = True
                user32.SetWindowPos(self._form_hwnd, 0, wa[0], wa[1], wa[2], wa[3],
                                    SWP_NOZORDER | SWP_NOACTIVATE)
            self._apply_ncr_state()
            self._invalidate_titlebar()
        except Exception as ex:
            log(f"toggle maximize failed: {ex}")

    def _work_area(self):
        """所在显示器工作区 (left, top, width, height)。"""
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        user32 = ctypes.windll.user32
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = user32.MonitorFromWindow(self._form_hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return (0, 0, 1920, 1040)
        return (mi.rcWork.left, mi.rcWork.top,
                mi.rcWork.right - mi.rcWork.left, mi.rcWork.bottom - mi.rcWork.top)

    def _on_mouse_up(self, sender, e) -> None:
        # 升级通知: 松开时仍在区域内 -> 弹出升级对话框
        was_u = self._update_pressed
        if was_u:
            self._update_pressed = False
            self._invalidate_titlebar()
            if self._hit_update(e.X, e.Y):
                self._open_update_dialog()
            return
        idx = self._hit_button(e.X, e.Y)
        was = self._pressed
        self._pressed = -1
        self._invalidate_titlebar()
        if was == -1 or idx != was:
            # 非按钮按下: 结束全屏拖动状态 (单击未拖动 → 保持全屏, 不还原)
            if self._drag_pending:
                self._drag_pending = False
                try:
                    ctypes.windll.user32.ReleaseCapture()
                except Exception:
                    pass
            return
        try:
            if idx == 0:
                self._window.minimize()
            elif idx == 1:
                self._toggle_maximize()
            else:
                # 关闭按钮 = 隐藏到系统托盘 (真正退出走托盘"退出"菜单)
                _hide_main_window()
        except Exception as ex:
            # 不吞异常: 记录后重抛, 让 WinForms 事件分发可见 (否则点击"没反应")
            log(f"titlebar button action failed idx={idx}: {ex}")
            raise

    # ---------- 升级通知 (检测/绘制/点击) ----------

    def _hit_update(self, x: int, y: int) -> bool:
        """x/y (窗口客户坐标) 是否命中升级通知区域。"""
        r = self._update_rect
        return r is not None and r.Contains(x, y)

    def set_update_info(self, info) -> None:
        """更新检测结果 (后台线程调用, 内部封送 UI 线程)。

        info=None 表示检测失败 (保持现状); available=False 表示无更新 (隐藏提示)。"""
        def _apply() -> None:
            self._update_info = info
            self._update_hover = False
            self._update_pressed = False
            if not info or not info.get("available"):
                self._update_rect = None
            self._invalidate_titlebar()
            # 演示模式自动弹出对话框 (仅测试钩子, 正常模式不受影响)
            if (info and info.get("demo")
                    and os.environ.get("DSH_DEMO_UPDATE_AUTOOPEN")):
                self._schedule_demo_open()
        try:
            from System import Action
            self.form.Invoke(Action(_apply))
        except Exception as ex:
            log(f"set_update_info failed: {ex}")

    def _schedule_demo_open(self) -> None:
        """演示模式下延迟自动弹出升级对话框 (验证 UI 用, 非生产路径)。"""
        def _later() -> None:
            time.sleep(2.0)
            try:
                from System import Action
                self.form.Invoke(Action(self._open_update_dialog))
            except Exception:
                pass
        threading.Thread(target=_later, daemon=True).start()

    def _open_update_dialog(self) -> None:
        if self._update_dialog_open:
            return
        self._update_dialog_open = True
        try:
            info = self._update_info
            # 总是打开版本列表界面 (git 样式): 不管有没有更新都可选历史版本切换。
            # 有更新时先标记已读 (红点消失, 持久化)。
            if info and info.get("available"):
                latest = info.get("latest") or ""
                if latest:
                    self._mark_update_seen(latest)
            show_update_dialog(self)
        except Exception as ex:
            log(f"update dialog open failed: {ex}")
            try:
                from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon
                MessageBox.Show(self.form, f"无法打开升级窗口: {ex}", "升级",
                                MessageBoxButtons.OK, MessageBoxIcon.Warning)
            except Exception:
                pass
        finally:
            self._update_dialog_open = False

    def _show_up_to_date_dialog(self) -> None:
        """无更新时点击灰色按钮: "已是最新"提示对话框 (含立即重新检测)。"""
        from System.Windows.Forms import (
            Form, Label, Button, FormBorderStyle)
        from System.Drawing import Color, Point, Size, Font
        dark = self._dark
        s = self._scale
        info = self._update_info

        form = Form()
        form.Text = "检查更新"
        form.FormBorderStyle = FormBorderStyle(0)  # None (python 关键字冲突, 用枚举构造)
        try:
            from System import Enum as _Enum
            form.StartPosition = _Enum.ToObject(form.StartPosition.GetType(), 4)
        except Exception:
            pass
        form.ShowInTaskbar = False
        form.MaximizeBox = False
        form.MinimizeBox = False
        form.ClientSize = Size(int(480 * s), int(210 * s))
        try:
            form.Font = Font("Microsoft YaHei UI", 9.5)
        except Exception:
            pass
        # 自绘标题栏 (主题色背景 + 图标 + 标题 + 关闭按钮, 可拖动)
        tb = _install_dialog_chrome(form, "检查更新", dark, s,
                                    lambda: form.Close())
        form.ClientSize = Size(int(480 * s), int(210 * s) + tb)

        # 无边框窗口: DWM 圆角 + 边框色 = 主题背景色 (与主窗口一致)
        _theme_bg = TITLEBAR_THEMES["dark" if dark else "light"]["bg"]

        def _apply_dwm(_s=None, _e=None) -> None:
            try:
                hwnd = form.Handle.ToInt32()
                _dwm = ctypes.WinDLL("dwmapi")
                _dwm.DwmSetWindowAttribute.restype = ctypes.c_long
                _dwm.DwmSetWindowAttribute.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
                col = ctypes.c_int((_theme_bg[2] << 16) | (_theme_bg[1] << 8) | _theme_bg[0])
                _dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
                corner = ctypes.c_int(2)
                _dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
            except Exception as ex:
                log(f"up-to-date dialog dwm failed: {ex}")

        form.Shown += _apply_dwm
        if dark:
            form.BackColor = Color.FromArgb(21, 21, 23)
            form.ForeColor = Color.FromArgb(229, 231, 235)

        def theme(ctrl) -> None:
            if dark:
                ctrl.BackColor = Color.FromArgb(30, 30, 33)
                ctrl.ForeColor = Color.FromArgb(229, 231, 235)

        head = Label()
        head.SetBounds(int(22 * s), int(24 * s) + tb, int(436 * s), int(44 * s))
        head.AutoSize = False
        if info is None:
            head.Text = "尚未完成更新检测（网络不可用？）。点击\"立即重新检测\"重试。"
        else:
            head.Text = (f"当前已是最新版本（{info.get('head_short', '?')}）。"
                         "官方仓库暂无新提交。")
        theme(head)
        form.Controls.Add(head)

        status = Label()
        status.SetBounds(int(22 * s), int(76 * s) + tb, int(436 * s), int(44 * s))
        status.AutoSize = False
        status.Text = "有更新时会变为蓝色并显示红点，点击可查看更新日志并选择版本。"
        status.ForeColor = Color.FromArgb(148, 163, 184) if dark else Color.Gray
        form.Controls.Add(status)

        btn_close = Button()
        btn_close.SetBounds(int(480 * s - 22 * s - 92 * s), int(140 * s) + tb, int(92 * s), int(32 * s))
        btn_close.Text = "关闭"
        form.Controls.Add(btn_close)

        btn_check = Button()
        btn_check.SetBounds(int(480 * s - 22 * s - 192 * s), int(140 * s) + tb, int(92 * s), int(32 * s))
        btn_check.Text = "立即重新检测"
        form.Controls.Add(btn_check)

        def _done(info2) -> None:
            try:
                if form.IsDisposed:
                    return
                if info2 is None:
                    status.Text = "检测失败（网络不可用？），请检查代理设置后重试。"
                    btn_check.Enabled = True
                    btn_close.Enabled = True
                    return
                if info2.get("available"):
                    form.Close()
                    self.set_update_info(info2)
                    # 直接打开升级对话框 (绕过 _update_dialog_open 标志, 已在打开中)
                    show_update_dialog(self)
                    return
                status.Text = f"仍然是最新（{info2.get('head_short', '?')}）。"
                btn_check.Enabled = True
                btn_close.Enabled = True
            except Exception as ex:
                log(f"recheck done failed: {ex}")

        def _recheck(_s, _e) -> None:
            btn_check.Enabled = False
            btn_close.Enabled = False
            status.Text = "正在检测官方仓库…"
            log("manual re-check requested")

            def _work() -> None:
                info2 = check_for_update()
                try:
                    from System import Action
                    form.Invoke(Action(lambda: _done(info2)))
                except Exception:
                    pass

            threading.Thread(target=_work, daemon=True).start()

        btn_close.Click += lambda s, e: form.Close()
        btn_check.Click += _recheck
        form.ShowDialog(self.form)
        try:
            form.Dispose()
        except Exception:
            pass

    def start_update_checker(self) -> None:
        """后台线程: 定期检测官方仓库 (origin/master) 更新。

        首个检测延迟 DSH_UPDATE_FIRST_DELAY 秒 (默认 12, 让启动/构建先完成),
        之后每 DSH_UPDATE_INTERVAL 秒一次 (默认 1800 = 30 分钟)。
        检测失败 (断网等) 不改变现有提示状态。"""
        if self._upd_thread_started:
            return
        self._upd_thread_started = True
        first_delay = float(os.environ.get("DSH_UPDATE_FIRST_DELAY", "12"))
        interval = float(os.environ.get("DSH_UPDATE_INTERVAL", "1800"))

        def _loop() -> None:
            try:
                time.sleep(first_delay)
                while True:
                    info = check_for_update()
                    if info is None:
                        log("update check failed (network/repo), keep current state")
                    else:
                        log("update check: " + (
                            f"update available ({info['count']} commits)"
                            if info.get("available") else "up to date"))
                        self.set_update_info(info)
                    time.sleep(interval)
            except Exception as ex:
                log(f"update checker stopped: {ex}")

        threading.Thread(target=_loop, daemon=True).start()
        log(f"update checker started (first={first_delay}s, interval={interval}s)")

    # ---------- 窗口行为: WndProc 子类化 (边缘缩放) ----------

    def _install_frame_chrome(self, hwnd: int) -> None:
        """拦截 WM_NCHITTEST / WM_GETMINMAXINFO:
        非全屏: 边缘 8px -> HT* (系统缩放, 窗口级 + WebView2 子窗口转发);
        全屏 (最大化): 任何位置 -> HTCLIENT, 拖动边框不产生缩放 (还原后恢复);
        其余 (含自绘标题栏区域) -> HTCLIENT (拖动/双击/按钮走 Form 鼠标事件);
        最大化时窗口=所在显示器工作区 (WM_GETMINMAXINFO, 自适应 DPI/分辨率/多屏)。
        """
        WM_NCHITTEST = 0x0084
        WM_GETMINMAXINFO = 0x0024
        WM_SIZE = 0x0005
        WM_NCLBUTTONDOWN = 0x00A1
        HTCLIENT = 1
        HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
        HTTOPLEFT, HTTOPRIGHT = 13, 14
        HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
        RESIZE_HITS = (HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT,
                       HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT)
        GWL_WNDPROC = -4
        MONITOR_DEFAULTTONEAREST = 2
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        WNDPROC_T = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_longlong)
        user32.GetWindowLongPtrW.restype = ctypes.c_void_p
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = ctypes.c_longlong
        user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_longlong]
        user32.ReleaseCapture.restype = wintypes.BOOL
        user32.SendMessageW.restype = ctypes.c_longlong
        user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, ctypes.c_ulong]
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        class MINMAXINFO(ctypes.Structure):
            _fields_ = [("ptReserved", POINT), ("ptMaxSize", POINT),
                        ("ptMaxPosition", POINT), ("ptMinTrackSize", POINT),
                        ("ptMaxTrackSize", POINT)]

        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]

        def _apply_minmaxinfo(hwnd_, lparam) -> None:
            """最大化 = 真正全屏 (覆盖任务栏) + 四周溢出 1px:
            窗口 rect 比屏幕各大 2px, DWM 的 1px 边框落在屏幕外不可见,
            图标/内容仅随窗口上移 1px。"""
            mmi = MINMAXINFO.from_address(ctypes.c_void_p(lparam).value)
            monitor = user32.MonitorFromWindow(hwnd_, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
                return
            # 最大化: 整屏 (含任务栏) + 四周各溢出 1px (仅藏掉 1px 边框)
            o = 1
            mw = mi.rcMonitor.right - mi.rcMonitor.left
            mh = mi.rcMonitor.bottom - mi.rcMonitor.top
            mmi.ptMaxPosition.x = mi.rcMonitor.left - o
            mmi.ptMaxPosition.y = mi.rcMonitor.top - o
            mmi.ptMaxSize.x = mw + 2 * o
            mmi.ptMaxSize.y = mh + 2 * o
            # 跟踪上限同步为溢出尺寸: 系统会把最大化尺寸 clamp 到 ptMaxTrackSize,
            # 不放大则最大化仍被限制在较小尺寸 (窗口只平移不变大)
            mmi.ptMaxTrackSize.x = mw + 2 * o
            mmi.ptMaxTrackSize.y = mh + 2 * o

        def hit_test(x: int, y: int) -> int:
            # x/y 为 WM_NCHITTEST lparam 屏幕坐标 (带符号, 支持负坐标副屏)
            # 全屏(最大化)时禁用边缘缩放: 任何位置都返回 HTCLIENT,
            # 拖动边框不再触发系统缩放循环 (还原后热区自动恢复)。
            if self._maximized:
                return HTCLIENT
            # 客户区屏幕边界 (GetWindowRect 含 Win11 阴影, 不能用)
            cr = RECT()
            user32.GetClientRect(hwnd, ctypes.byref(cr))
            origin = POINT(0, 0)
            user32.ClientToScreen(hwnd, ctypes.byref(origin))
            cl, ct = origin.x, origin.y
            cw, ch = cr.right, cr.bottom
            border = RESIZE_BORDER * self._scale  # 边缘热区随 DPI 缩放
            left = x <= cl + border
            right = x >= cl + cw - border
            top = y <= ct + border
            bottom = y >= ct + ch - border
            if top and left:
                return HTTOPLEFT
            if top and right:
                return HTTOPRIGHT
            if bottom and left:
                return HTBOTTOMLEFT
            if bottom and right:
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM
            # 其余 (含自绘标题栏区域) -> HTCLIENT: 标题栏拖动/双击/按钮
            # 全部由 Form 级鼠标事件 (MouseDown/Move/Up) 处理, 与系统行为一致
            return HTCLIENT

        def wndproc(hwnd_, msg, wparam, lparam):
            if os.environ.get("DSH_NCHIT_LOG"):
                log(f"wndproc msg=0x{msg:04x} wp={wparam}")
            if msg == WM_NCHITTEST:
                try:
                    x = ctypes.c_short(lparam & 0xFFFF).value
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    r = hit_test(x, y)
                    if os.environ.get("DSH_NCHIT_LOG"):
                        log(f"nchit x={x} y={y} -> {r}")
                    return r
                except Exception:
                    return HTCLIENT
            if msg == WM_GETMINMAXINFO:
                try:
                    _apply_minmaxinfo(hwnd_, lparam)
                    return 0
                except Exception:
                    pass
            # 全屏时拖边缘缩放已禁用: hit_test 在 _maximized 时返回 HTCLIENT,
            # 系统不会发起缩放循环, 也不需要"先还原再缩放"。
            # WM_SIZE: NCR/圆角状态由 _apply_ncr_state (Resize 事件) 统一管理,
            # 这里不覆盖 self._maximized (手动最大化/还原, 避免被 SIZE_RESTORED 冲掉)。
            return user32.CallWindowProcW(orig, hwnd_, msg, wparam, lparam)

        proc = WNDPROC_T(wndproc)
        orig = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
        newproc = ctypes.cast(proc, ctypes.c_void_p).value
        old = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, newproc)
        self._chrome_ref = (proc,)
        log(f"frame chrome installed (wndproc subclassed) orig={orig:#x} "
            f"new={newproc:#x} setlwpret={old:#x}")

        # WebView2 铺满客户区, 边缘的 WM_NCHITTEST 会发给这个子窗口而到不了
        # 父窗口 (父窗口子类化收不到), 导致边缘缩放失效。子类化 WebView2 的
        # WndProc: 拦截 WM_NCHITTEST 复用同一个 hit_test (返回 HT* 由系统缩放),
        # 其余消息原样转发, 不干扰 WebView2 自身功能。
        try:
            wv_ctrl = self._webview_ctrl
            wv_hwnd = wv_ctrl.Handle.ToInt32()
            wv_orig = user32.GetWindowLongPtrW(wv_hwnd, GWL_WNDPROC)

            def wv_wndproc(wv_hwnd_, msg, wparam, lparam):
                if msg == WM_NCHITTEST:
                    try:
                        x = ctypes.c_short(lparam & 0xFFFF).value
                        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                        r = hit_test(x, y)
                        if os.environ.get("DSH_NCHIT_LOG"):
                            log(f"wv nchit x={x} y={y} -> {r}")
                        return r
                    except Exception:
                        return HTCLIENT
                return user32.CallWindowProcW(wv_orig, wv_hwnd_, msg, wparam, lparam)

            wv_proc = WNDPROC_T(wv_wndproc)
            wv_new = ctypes.cast(wv_proc, ctypes.c_void_p).value
            wv_old = user32.SetWindowLongPtrW(wv_hwnd, GWL_WNDPROC, wv_new)
            self._webview_chrome_ref = (wv_proc,)
            log(f"webview wndproc subclassed hwnd={wv_hwnd} orig={wv_orig:#x} "
                f"new={wv_new:#x} setlwpret={wv_old:#x}")
        except Exception as ex:
            log(f"webview wndproc subclass failed: {ex}")


# ==================== 升级检测与版本更新 ====================
# 需求: 窗体标题栏最小化按钮左侧显示"有新版本"蓝色提示 (蓝色文字 + 蓝色
# 下划线, 无背景色); 点击弹出更新日志 (官方 master 新提交列表, 即更新日志)
# 与版本选择 (最新版 / 指定提交), 确认后 git fetch + checkout 切换版本,
# 并让下次启动自动重新构建 (删除构建指纹标记)。
#
# 网络: 直连 GitHub 可能失败 (尤其代理环境), 自动读取 Windows 系统代理
# (HKCU Internet Settings) 作为 git http.proxy 重试; 也可用环境变量
# DSH_GIT_PROXY 显式指定代理 (http://host:port)。
#
# 测试钩子 (不影响正常运行):
#   DSH_DEMO_UPDATE=1          模拟"有更新"数据与模拟升级 (不访问网络, 不碰仓库)
#   DSH_DEMO_UPDATE_AUTOOPEN=1 演示模式下自动弹出升级对话框 (验证 UI 用)
#   DSH_UPDATE_FIRST_DELAY / DSH_UPDATE_INTERVAL  首次检测延迟/检测间隔 (秒)

def _no_window_startup() -> tuple[int, object | None]:
    """subprocess 无窗口启动参数 (Windows)。"""
    if os.name != "nt":
        return 0, None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return subprocess.CREATE_NO_WINDOW, si


def _read_system_proxy() -> str | None:
    """读取 Windows 系统代理 (HKCU .../Internet Settings), 返回 http://host:port
    或 None。仅提取 http 协议条目 (https 条目同 host, http 够用)。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            winreg.CloseKey(key)
    except OSError:
        return None
    if not enable or not server:
        return None
    server = server.strip()
    if not server:
        return None
    if "=" in server:  # 分协议列表: http=host:port;https=host:port
        for part in server.split(";"):
            name, _, host = part.partition("=")
            if name.strip().lower() == "http" and host.strip():
                server = host.strip()
                break
        else:
            return None
    if "://" not in server:
        server = "http://" + server
    return server


def _git_proxy_candidates() -> list[str]:
    """候选代理列表: 环境变量 DSH_GIT_PROXY 优先, 其次系统代理。"""
    out: list[str] = []
    seen: set[str] = set()
    for p in (os.environ.get("DSH_GIT_PROXY", "").strip(),
              _read_system_proxy() or ""):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


_GIT_PROXY = {"value": None}  # None=未探测  ""=直连可用  "url"=该代理可用

# 包内便携 git (MinGit): release 包自带, 接收方无需安装 git 也能
# 拉取仓库 / 切换版本。MinGit zip 解压后根目录直接是 cmd\git.exe 等,
# 打包时放在 DSH_Desktop\portable\git\ 下。源码/开发模式没有该目录,
# 回退使用系统 git。
PORTABLE_GIT = BUILD_DIR / "portable" / "git" / "cmd" / "git.exe"

_GIT_BIN: str | None = None


def _system_git_available() -> bool:
    """检测系统 PATH 里是否有可用的 git (用户自己安装了 git)。"""
    try:
        import shutil
        return shutil.which("git") is not None
    except Exception:
        return False


def _git_bin() -> str:
    """解析 git 可执行文件: 优先系统 git (用户已安装时直接用其 git),
    否则用包内便携 MinGit。

    首次调用探测并缓存 (启动后不会中途更换)。"""
    global _GIT_BIN
    if _GIT_BIN is None:
        if _system_git_available():
            _GIT_BIN = "git"
            log("using system git (user installed)")
        elif PORTABLE_GIT.is_file():
            _GIT_BIN = str(PORTABLE_GIT)
            log(f"using bundled git: {_GIT_BIN}")
        else:
            _GIT_BIN = "git"
            log("no git found (system or bundled), git commands will fail")
    return _GIT_BIN


_NODE_BIN: str | None = None


def _node_bin() -> str:
    """解析 node 可执行文件: 优先包内便携 node, 否则系统 node。

    首次调用探测并缓存。"""
    global _NODE_BIN
    if _NODE_BIN is None:
        if PORTABLE_NODE.is_file():
            _NODE_BIN = str(PORTABLE_NODE)
            log(f"using bundled node: {_NODE_BIN}")
        else:
            _NODE_BIN = "node"
            log("bundled node not found, falling back to system node")
    return _NODE_BIN


def _node_env() -> dict:
    """子进程环境: 便携 node 时把其目录 prepend 到 PATH。

    后端/构建脚本内部会 spawn node/pnpm 子命令, 需要 node 在 PATH 里
    (否则只装了内嵌 node、没装系统 node 的机器上子命令找不到 node)。"""
    env = os.environ.copy()
    node_bin = _node_bin()
    node_dir = str(Path(node_bin).resolve().parent) if os.path.isabs(node_bin) else ""
    if node_dir:
        path_key = next((k for k in env if k.lower() == "path"), "PATH")
        env[path_key] = node_dir + os.pathsep + env.get(path_key, "")
    return env


_PNPM_BIN: str | None = None


def _pnpm_bin() -> str:
    """解析 pnpm: 便携 pnpm.exe > 仓库 node_modules 里的 pnpm.cjs > 系统 pnpm。

    首次调用探测并缓存。"""
    global _PNPM_BIN
    if _PNPM_BIN is None:
        if PORTABLE_PNPM.is_file():
            _PNPM_BIN = str(PORTABLE_PNPM)
            log(f"using bundled pnpm: {_PNPM_BIN}")
        elif (SOURCE / "node_modules" / "pnpm" / "bin" / "pnpm.cjs").is_file():
            _PNPM_BIN = str(SOURCE / "node_modules" / "pnpm" / "bin" / "pnpm.cjs")
            log(f"using repo pnpm: {_PNPM_BIN}")
        else:
            _PNPM_BIN = "pnpm"
            log("bundled pnpm not found, falling back to system pnpm")
    return _PNPM_BIN


def _pnpm_cmd(action: str) -> str:
    """返回可直接执行的 pnpm 命令串 (带引号): pnpm.exe 直接跑;
    .cjs 用 node 跑 (pnpm.cjs 需要 node 运行时); 系统 pnpm 直接调。
    仅用于弹控制台窗口的构建命令; 静默子进程请用 _pnpm_list
    (cmd /S /c 的嵌套引号会把参数里带引号的值解析坏, 如
    --store-dir "path with spaces" 的结尾引号会传给 pnpm)。"""
    bin_ = _pnpm_bin()
    if bin_ == "pnpm":
        return f"pnpm {action}"
    if bin_.lower().endswith(".cjs"):
        return f'"{_node_bin()}" "{bin_}" {action}'
    return f'"{bin_}" {action}'


def _pnpm_list(action: list[str]) -> list[str]:
    """返回 pnpm 命令 (list 形式, 无 cmd 引号问题): pnpm.exe 直接跑;
    .cjs 用 node 跑; 系统 pnpm 直接调。"""
    bin_ = _pnpm_bin()
    if bin_ == "pnpm":
        return ["pnpm"] + action
    if bin_.lower().endswith(".cjs"):
        return [_node_bin(), bin_] + action
    return [bin_] + action


def _start_cmd() -> list[str]:
    """启动后端的命令: node apps/cli/lib/bin.js web (node 用解析后的路径)。"""
    return [_node_bin(), "apps/cli/lib/bin.js", "web"]


def _build_cmd() -> str:
    """重建后端的命令 (完整命令行字符串): cmd /S /c "node <pnpm> run build ..."。

    弹独立控制台, 失败暂停。必须返回字符串并由 subprocess 以字符串模式
    执行 (CreateProcess 原样传命令行): 列表模式会被 list2cmdline 把内部
    引号转义成 \\", cmd 不认, 含空格的路径会解析失败。"""
    return ('cmd /S /c "'
            + _pnpm_cmd("run build")
            + ' && exit 0 || (echo. & echo [BUILD FAILED] 构建失败, 请查看上方错误信息. & pause)"')


def _git(args: list[str], timeout: float = 180.0) -> tuple[int, str, str]:
    """在仓库内运行 git (cwd=SOURCE), 带系统代理回退 + 内嵌 git 回退。

    候选顺序: 系统 git (用户已安装) 优先; 失败 (网络/启动/自身配置) 自动
    回退包内便携 MinGit 重试; 仍失败返回最后一次结果。
    返回 (returncode, stdout, stderr); 超时/无法启动返回 returncode=-1。
    首次调用探测直连与各代理 (系统代理每次实时从注册表读取), 成功后缓存;
    缓存命中但失败 (如代理端口变了/代理关闭) 时清除缓存并重新探测。"""
    flags, si = _no_window_startup()

    def _run(bin_: str, proxy: str | None):
        cmd = [bin_, "-C", str(SOURCE)]
        if proxy:
            cmd += ["-c", "http.proxy=" + proxy]
        try:
            # git 输出可能是 UTF-8 (中文提交信息等), 显式按 UTF-8 解码,
            # 避免系统默认 GBK 解码崩溃导致 stdout 变空 (列表为空/只有一个 commit)
            r = subprocess.run(cmd + list(args), capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=timeout, creationflags=flags, startupinfo=si)
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"git 超时 ({timeout:.0f}s): {' '.join(args)}"
        except OSError as ex:
            return -1, "", f"git 启动失败: {ex}"

    # 候选 git: 系统 git 优先, 内嵌 MinGit 兜底 (去重)
    candidates: list[str] = []
    if _system_git_available():
        candidates.append("git")
    if PORTABLE_GIT.is_file():
        candidates.append(str(PORTABLE_GIT))
    if not candidates:
        candidates = ["git"]

    last: tuple[int, str, str] | None = None
    for idx, bin_ in enumerate(candidates):
        cached = _GIT_PROXY["value"]
        if cached is not None:
            r = _run(bin_, cached or None)
            if r[0] == 0:
                return r
            # 缓存失效 (代理端口变了/代理关了): 清缓存, 走完整重新探测
            log(f"git cached proxy '{cached}' failed, re-probing (system proxy may have changed)")
            _GIT_PROXY["value"] = None
        last = None
        for p in [None] + list(_git_proxy_candidates()):
            r = _run(bin_, p)
            if r[0] == 0:
                _GIT_PROXY["value"] = p or ""
                return r
            last = r
        if last is not None and last[0] == 0:
            return last
        if idx < len(candidates) - 1:
            log("system git failed (network/config), falling back to bundled MinGit")
    return last if last is not None else (-1, "", "git unavailable")


def _read_update_seen() -> str:
    """读取升级通知"已读"标记 (已查看过的最新提交哈希), 无则空串。

    用途: 红点显隐判断 (点击升级按钮后记录, 重启后仍记住)。"""
    try:
        if SEEN_MARKER.exists():
            return SEEN_MARKER.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _demo_update_info(seed: str) -> dict:
    """测试/演示用假更新数据 (不访问网络, 不修改仓库)。

    用途: 不联网即可验证标题栏升级按钮、红点、更新日志列表与版本选择 UI。
    seed 为数字时作为模拟新提交数量 (1..20), 否则默认 3 条。"""
    n = 3
    try:
        n = max(1, min(20, int(seed)))
    except ValueError:
        pass
    commits = []
    for i in range(n):
        commits.append({
            "hash": "d5em0" + f"{i:035d}",
            "short": "demo%02d" % i,
            "date": "2026-01-%02d %02d:%02d:%02d" % (
                i + 1, (i + 8) % 24, (i * 7) % 60, (i * 11) % 60),
            "subject": f"模拟提交 {i + 1}: 演示用更新日志条目 (DSH_DEMO_UPDATE)",
        })
    return {
        "available": True, "count": n, "demo": True,
        "head": "47f943859bef60e4160492346772ded9b24f765a",
        "head_short": "47f9438",
        "latest": "d" * 40, "latest_short": "d" * 7,
        "commits": commits,
    }


def _fetch_origin() -> tuple[int, str, str]:
    """拉取官方 master 到 origin/master: 默认走 origin remote (clone 时是 SSH),
    SSH 失败 (未配 key / 认证失败 / 网络) 时回退 HTTPS URL 拉取。"""
    r = _git(["fetch", "origin", "master", "--no-tags"], timeout=180)
    if r[0] == 0:
        return r
    log("fetch via origin failed, falling back to https url: " + r[2].strip()[:200])
    return _git(["fetch", REPO_URL_HTTPS,
                 "master:refs/remotes/origin/master", "--no-tags"], timeout=180)


def check_for_update() -> dict | None:
    """检测官方仓库 (origin/master) 相对当前 HEAD 的新提交。

    成功返回信息字典 (available=False 也表示检测成功, 只是无更新);
    网络/仓库异常返回 None (调用方保持现状, 不因检测失败隐藏已有提示)。"""
    demo = os.environ.get("DSH_DEMO_UPDATE", "").strip()
    if demo:
        log("update check: DEMO mode (DSH_DEMO_UPDATE)")
        return _demo_update_info(demo)
    r = _fetch_origin()
    if r[0] != 0:
        log(f"update check: fetch failed: {r[2].strip()[:300]}")
        return None
    head_r = _git(["rev-parse", "HEAD"])
    if head_r[0] != 0:
        log("update check: cannot resolve HEAD")
        return None
    count_r = _git(["rev-list", "--count", "HEAD..origin/master"])
    if count_r[0] != 0:
        log("update check: cannot count commits")
        return None
    try:
        count = int(count_r[1].strip() or "0")
    except ValueError:
        return None
    latest_r = _git(["rev-parse", "origin/master"])
    head = head_r[1].strip()
    latest = latest_r[1].strip() if latest_r[0] == 0 else ""
    info = {"available": count > 0, "count": count, "demo": False,
            "head": head, "head_short": head[:7],
            "latest": latest, "latest_short": latest[:7],
            "commits": []}
    if count > 0:
        log_r = _git(["log", "HEAD..origin/master",
                      "--format=%H%x09%ad%x09%s", _GIT_DATE_FMT])
        if log_r[0] == 0 and log_r[1]:
            for line in log_r[1].splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    h, d, subj = parts
                    info["commits"].append(
                        {"hash": h, "short": h[:7], "date": d, "subject": subj})
        log(f"update check: {count} new commits on origin/master")
    else:
        log("update check: up to date")
    return info


def perform_update(target_ref: str, progress=None, demo: bool = False) -> tuple[bool, str]:
    """把仓库强制切换到 target_ref (本地已存在的 commit/ref)。

    纯本地操作, 不访问网络: 切换 = git checkout -f (强制, 丢弃工作区所有
    未提交改动), 切换后处于 detached HEAD。远程拉取 (fetch) 由后台更新
    检测线程 / 对话框"获取最新仓库"按钮负责, 切换只针对本地已有提交。
    流程: 校验目标本地存在 -> checkout -f -> 记录 last-commit.txt ->
    删除构建指纹标记 (调用方随后立即重新构建, 删指纹作失败兜底) ->
    pnpm install (尽力而为)。
    返回 (ok, message)。progress(msg) 可选回调 (后台线程调用, 调用方负责封送)。"""
    def _say(m: str) -> None:
        if progress:
            try:
                progress(m)
            except Exception:
                pass
        log("update: " + m)

    if demo:
        _say("演示模式: 模拟升级过程…")
        time.sleep(1.5)
        _say("演示完成 (未实际修改代码)")
        return True, "演示模式: 已完成, 未修改任何代码。"

    # 校验目标在本地存在 (列表里的 commit 都来自本地仓库; 防止
    # origin/master 从未拉取成功时 checkout 直接失败)
    verify = _git(["rev-parse", "--verify", "--quiet", target_ref + "^{commit}"],
                  timeout=30)
    if verify[0] != 0:
        return False, ("本地没有目标提交 " + target_ref[:12]
                       + "，请先点击\"获取最新仓库\"拉取后再切换。")
    _say(f"切换版本到 {target_ref[:12]} …")
    # 强制切换: 丢弃工作区所有未提交改动, 切换后为 detached HEAD
    r = _git(["checkout", "-q", "-f", target_ref], timeout=300)
    if r[0] != 0:
        return False, ("切换版本失败: " + (r[2].strip() or "未知错误")[:400]
                       + "\n\n强制切换会丢弃工作区未提交的改动。")
    new_r = _git(["rev-parse", "HEAD"])
    new_head = new_r[1].strip() if new_r[0] == 0 else "?"
    # 记录当前提交 (与 DSH_Desktop/last-commit.txt 的既有约定一致)
    try:
        (BUILD_DIR / "last-commit.txt").write_text(new_head + "\n", encoding="utf-8")
    except OSError as ex:
        log(f"update: last-commit write failed: {ex}")
    # 使构建指纹失效: 调用方随后立即重新构建; 若构建失败, 下次启动
    # 检测到指纹缺失会自动重试构建 (兜底)
    try:
        if MARKER.exists():
            MARKER.unlink()
            log("update: build fingerprint invalidated (rebuild follows)")
    except OSError as ex:
        log(f"update: fingerprint invalidate failed: {ex}")
    _say("安装依赖 (pnpm install) …")
    # list 模式: cmd /S /c 嵌套引号会把含空格路径解析坏
    pi = hidden_run(_pnpm_list(["install", "--config.confirmModulesPurge=false"]),
                    input="y\n",
                    cwd=str(SOURCE), env=_node_env(),
                    capture_output=True, text=True, timeout=900)
    if pi.returncode != 0:
        log("update: pnpm install failed: " + ((pi.stderr or "")[-400:]))
        msg = (f"已切换到 {new_head[:12]}（强制切换，工作区改动已丢弃）。"
               "依赖安装未完成 (pnpm install 失败), 重新构建可能失败。")
    else:
        msg = (f"已切换到 {new_head[:12]}（强制切换，工作区改动已丢弃）。"
               "正在重新构建后端…")
    _say("完成")
    return True, msg


def _wait_backend_ready(timeout: float | None = None) -> bool:
    """轮询 http://127.0.0.1:PORT 直到就绪 (默认 WAIT_TIMEOUT 秒)。"""
    deadline = time.time() + (timeout if timeout is not None else WAIT_TIMEOUT)
    while time.time() < deadline:
        if http_ready():
            return True
        time.sleep(0.5)
    return False


def _restart_backend_blocking(timeout: float | None = None) -> bool:
    """杀旧后端进程树 -> 启动新后端 (纳入 Job) -> 等待就绪。

    用于切换版本重新构建后加载新产物。返回是否就绪; 端口被非 DSH
    进程占用时只等待就绪 (不杀)。"""
    global _JOB_HANDLE
    proc = _ACTIVE.get("proc")
    if proc is not None and proc.poll() is None:
        log(f"restart backend: killing old backend pid={proc.pid}")
        kill_tree(proc.pid)
        _ACTIVE["proc"] = None
    # 等端口释放 (kill_tree 异步, 轮询)
    for _ in range(40):
        if not port_open("127.0.0.1", PORT):
            break
        time.sleep(0.25)
    if port_open("127.0.0.1", PORT):
        log("restart backend: port still occupied (not ours?), reusing")
        return _wait_backend_ready(timeout)
    log("restart backend: starting new backend")
    p = start_backend()
    if p is not None and _JOB_HANDLE is not None:
        _assign_pid_to_job(_JOB_HANDLE, p.pid)
    return _wait_backend_ready(timeout)


def _reload_webview() -> None:
    """重新加载主窗口页面 (重建后端后重开画面)。"""
    w = _MAIN_WINDOW
    if w is None:
        return
    try:
        w.load_url(URL)
        log("webview reloaded (load_url)")
    except Exception as ex:
        log(f"webview load_url failed: {ex}")
        try:
            w.evaluate_js("location.reload()")
            log("webview reloaded (location.reload)")
        except Exception as ex2:
            log(f"webview reload failed: {ex2}")


def _start_rebuild_after_switch(titlebar) -> None:
    """切换版本成功后立即执行: 重新构建后端 -> 重启后端 -> 重开画面。

    后台线程执行; 期间主界面保持"版本切换中…"覆盖层, 页面刷新后自动
    消失 (新页面没有覆盖层 div)。构建失败: 提示用户 (代码已切换,
    下次启动会自动重试构建, 当前仍用旧后端)。"""
    def _ui(fn) -> None:
        # 封送到主窗口 UI 线程 (titlebar.form 是主窗口, 升级对话框已关闭)
        try:
            from System import Action
            titlebar.form.Invoke(Action(fn))
        except Exception:
            try:
                fn()
            except Exception:
                pass

    def _finish_ui(alert: str | None) -> None:
        def _do() -> None:
            try:
                hide = getattr(titlebar, "_hide_updating_overlay", None)
                if hide is not None:
                    hide()
            except Exception:
                pass
            if alert:
                try:
                    from System.Windows.Forms import (
                        MessageBox, MessageBoxButtons, MessageBoxIcon)
                    MessageBox.Show(titlebar.form, alert, "重新构建",
                                    MessageBoxButtons.OK, MessageBoxIcon.Warning)
                except Exception:
                    pass
        _ui(_do)

    def _work() -> None:
        try:
            log("rebuild after switch: pnpm build start")
            if not run_build():
                log("rebuild after switch: build failed")
                _finish_ui("重新构建后端失败。代码已切换，下次启动应用时会自动重试构建。")
                return
            fp = get_workspace_fingerprint()
            if fp:
                record_fingerprint(fp)
                log("rebuild after switch: fingerprint recorded")
            log("rebuild after switch: restarting backend")
            if not _restart_backend_blocking():
                log("rebuild after switch: backend not ready in time")
                _finish_ui(f"后端重启超时（超过 {WAIT_TIMEOUT} 秒未就绪）。请重启应用。")
                return
            log("rebuild after switch: reloading webview")
            _ui(_reload_webview)
            log("rebuild after switch: done")
        except Exception as ex:
            log(f"rebuild after switch failed: {ex}")
            _finish_ui(f"切换后重建流程出错: {ex}")

    threading.Thread(target=_work, daemon=True).start()


def _restart_application() -> None:
    """延迟 3 秒重启 exe (等本进程退出、释放单实例 Mutex 后再启动新实例)。

    用独立的 powershell 进程做延迟启动 (本进程退出后它仍存活)。
    注: 版本切换已改为切换后自动重建重开画面 (_start_rebuild_after_switch),
    本函数当前无调用者, 保留备用 (如构建失败后提供"立即重启"选项)。"""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
    else:
        exe = BASE / "DSH_Desktop.exe"
    if not exe.is_file():
        log(f"restart: exe not found at {exe}")
        return
    ps = ("Start-Sleep -Seconds 3; "
          "Start-Process -FilePath '" + str(exe).replace("'", "''") + "'")
    try:
        flags, si = _no_window_startup()
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive",
                          "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=flags, startupinfo=si)
        log(f"restart scheduled: {exe}")
    except Exception as ex:
        log(f"restart spawn failed: {ex}")


# ==================== 启动加载窗 (splash) ====================
# 覆盖"构建后端 / 启动后端 / 等待就绪"阶段: 中间 deepseek娘.png,
# 下方蓝色 marquee 进度条。主题色与主窗口同一套方案 (CSS token + 偏好)。
_SPLASH = {"form": None, "bar": None, "bar_state": None, "lbl": None}

# 启动流程取消状态: 用户点击 splash 关闭按钮时置位, 终止进行中的
# clone/install/build 子进程并让 main() 尽快退出。
_ACTIVE = {"proc": None, "cancel": False}


def _splash_theme():
    """与主窗口一致的 (dark, bg_rgb, fg_rgb, track_rgb) 配色。"""
    tokens = read_theme_tokens()
    dark = resolve_initial_dark()
    if tokens:
        bg_rgb = tokens[0] if dark else tokens[1]
    else:
        bg_rgb = (21, 21, 23) if dark else (249, 250, 251)
    fg_rgb = (229, 231, 235) if dark else (31, 41, 55)
    track_rgb = (47, 47, 50) if dark else (226, 230, 236)
    return dark, bg_rgb, fg_rgb, track_rgb


def _splash_paint_bar(sender, e, state, blue) -> None:
    """自绘蓝色圆角进度条 (按 state["pct"] 0..100 填充), 颜色固定 #2563EB。"""
    try:
        from System.Drawing import SolidBrush
        from System.Drawing.Drawing2D import (GraphicsPath, SmoothingMode)
        w = sender.ClientSize.Width
        h = sender.ClientSize.Height
        pct = max(0.0, min(100.0, float(state["pct"])))
        bw = w * pct / 100.0
        if bw <= 0:
            return
        g = e.Graphics
        g.SmoothingMode = SmoothingMode.AntiAlias
        d = float(h)
        if bw < d:
            bw = d  # 极小进度时仍显示一个最小圆角块
        path = GraphicsPath()
        path.AddArc(0, 0, d, d, 180, 90)
        path.AddArc(bw - d, 0, d, d, 270, 90)
        path.AddArc(bw - d, h - d, d, d, 0, 90)
        path.AddArc(0, h - d, d, d, 90, 90)
        path.CloseFigure()
        g.FillPath(SolidBrush(blue), path)
    except Exception as ex:
        log(f"splash bar paint failed: {ex}")


def _splash_run() -> None:
    """加载窗线程入口 (独立消息循环); 主窗口显示后由 _close_splash 关闭。"""
    try:
        import clr  # noqa: F401  初始化 pythonnet (主流程在 import webview 时才加载, 这里提前)
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System import Enum as _Enum
        from System.Windows.Forms import (Form, PictureBox, Label, Panel,
                                          FormBorderStyle, Application)
        from System.Drawing import (Color, Size, Image, Font, ContentAlignment, Icon)
        dark, bg_rgb, fg_rgb, track_rgb = _splash_theme()
        png = WINDOW_DIR / "deepseek娘.png"
        BLUE = Color.FromArgb(37, 99, 235)  # WebUI 主按钮蓝 #2563EB

        form = Form()
        form.Text = "DSH Desktop"
        form.FormBorderStyle = FormBorderStyle(0)  # None
        # pythonnet 3.x: StartPosition 枚举无法直接 import, 用 Enum.ToObject 构造
        form.StartPosition = _Enum.ToObject(form.StartPosition.GetType(), 1)  # CenterScreen
        form.ShowInTaskbar = True
        # 任务栏图标: 用应用图标, 避免任务栏空白/默认图标
        try:
            ico = WINDOW_DIR / "deepseek娘.ico"
            if ico.is_file():
                form.Icon = Icon(str(ico))
        except Exception:
            pass
        # 不用 TopMost (置顶会一直挡其他窗口): 改为显示时激活到前台
        # (与主窗口 _activate_foreground 同一手法), 一瞬间跑到最前面
        # 但不带置顶属性, 之后其他窗口可正常盖住它。
        def _splash_activate(sender, e) -> None:
            try:
                hwnd = form.Handle.ToInt32()
                u = ctypes.windll.user32
                # 模拟 ALT 键解除前台锁限制 (标准绕过手法, 同主窗口)
                u.keybd_event(0x12, 0, 0, 0)
                u.keybd_event(0x12, 0, 2, 0)
                u.ShowWindow(hwnd, 9)  # SW_RESTORE
                u.SetForegroundWindow(hwnd)
                u.BringWindowToTop(hwnd)
                u.SetActiveWindow(hwnd)
            except Exception:
                pass

        form.Shown += _splash_activate
        try:
            form.Font = Font("Microsoft YaHei UI", 9.5)
        except Exception:
            pass
        form.ClientSize = Size(460, 560)
        form.BackColor = Color.FromArgb(bg_rgb[0], bg_rgb[1], bg_rgb[2])

        # DWM 圆角 + 边框色 = 背景色 (与主窗口/升级对话框一致)
        def _dwm(sender, e) -> None:
            try:
                hwnd = form.Handle.ToInt32()
                dwm = ctypes.WinDLL("dwmapi")
                dwm.DwmSetWindowAttribute.restype = ctypes.c_long
                dwm.DwmSetWindowAttribute.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
                col = ctypes.c_int((bg_rgb[2] << 16) | (bg_rgb[1] << 8) | bg_rgb[0])
                dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
                corner = ctypes.c_int(2)  # DWMWA_WINDOW_CORNER_PREFERENCE = ROUND
                dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
            except Exception:
                pass
        form.Shown += _dwm

        # 右上角关闭按钮: 与主窗口标题栏一致的样式 (hover 红底 + 白色 ×)
        from System.Drawing import Pen as _Pen, SolidBrush as _SolidBrush
        from System.Windows.Forms import Cursors as _Cursors
        from System.Drawing.Drawing2D import SmoothingMode as _Smoothing
        close_state = {"hover": False, "pressed": False}
        close_btn = Panel()
        close_btn.SetBounds(460 - 44, 0, 44, 40)
        close_btn.BackColor = form.BackColor

        def _paint_close(s, e) -> None:
            try:
                g = e.Graphics
                rw, rh = 44, 40
                if close_state["pressed"]:
                    col = (196, 52, 52)
                elif close_state["hover"]:
                    col = (239, 68, 68)
                else:
                    col = None
                if col is not None:
                    g.FillRectangle(_SolidBrush(Color.FromArgb(col[0], col[1], col[2])),
                                    0, 0, rw, rh)
                x_rgb = ((229, 231, 235) if (close_state["hover"] or close_state["pressed"])
                         else (148, 163, 184))
                pen = _Pen(Color.FromArgb(x_rgb[0], x_rgb[1], x_rgb[2]), 2.0)
                g.SmoothingMode = _Smoothing.AntiAlias
                cx, cy = rw / 2.0, rh / 2.0
                g.DrawLine(pen, cx - 7, cy - 7, cx + 7, cy + 7)
                g.DrawLine(pen, cx + 7, cy - 7, cx - 7, cy + 7)
                pen.Dispose()
            except Exception:
                pass

        close_btn.Paint += _paint_close
        close_btn.MouseEnter += lambda s, e: (close_state.update(hover=True), close_btn.Invalidate())
        close_btn.MouseLeave += lambda s, e: (close_state.update(hover=False, pressed=False), close_btn.Invalidate())
        close_btn.MouseDown += lambda s, e: (close_state.update(pressed=True), close_btn.Invalidate())
        close_btn.MouseUp += lambda s, e: (close_state.update(pressed=False), close_btn.Invalidate())
        close_btn.Click += lambda s, e: _cancel_startup()
        try:
            close_btn.Cursor = _Cursors.Hand
        except Exception:
            pass
        form.Controls.Add(close_btn)

        # 中间图片: 1:1 原图居中显示 (256x256), 不放大避免发糊
        pic = PictureBox()
        pic.SetBounds(102, 70, 256, 256)
        pic.SizeMode = _Enum.ToObject(pic.SizeMode.GetType(), 3)  # CenterImage (原尺寸)
        pic.BackColor = form.BackColor
        if png.is_file():
            try:
                pic.Image = Image.FromFile(str(png))
            except Exception as ex:
                log(f"splash png load failed: {ex}")
        form.Controls.Add(pic)

        # 蓝色进度条 (自绘, 按阶段推进的确定进度)
        bar = Panel()
        bar.SetBounds(100, 400, 260, 8)
        bar.BackColor = Color.FromArgb(track_rgb[0], track_rgb[1], track_rgb[2])
        # 双缓冲: 每次 Invalidate 触发全量自绘, 无双缓冲会明显闪烁
        try:
            from System.Windows.Forms import ControlStyles as _CS
            bar.SetStyle(_CS.OptimizedDoubleBuffer | _CS.AllPaintingInWmPaint
                         | _CS.UserPaint, True)
        except Exception:
            pass
        bar_state = {"pct": 0.0}
        bar.Paint += lambda s, e: _splash_paint_bar(s, e, bar_state, BLUE)
        form.Controls.Add(bar)

        # 底部文字
        lbl = Label()
        lbl.SetBounds(0, 430, 460, 40)
        lbl.Text = "正在启动 DSH Desktop…"
        lbl.TextAlign = ContentAlignment.MiddleCenter
        lbl.BackColor = form.BackColor
        lbl.ForeColor = Color.FromArgb(fg_rgb[0], fg_rgb[1], fg_rgb[2])
        form.Controls.Add(lbl)

        # 无边框窗体拖动: 任意位置按下左键 -> ReleaseCapture +
        # WM_NCLBUTTONDOWN/HTCAPTION 让系统接管拖动循环 (与主窗口标题栏
        # 同一手法)。关闭按钮除外 (保留其点击 = 取消启动)。
        from System.Windows.Forms import MouseButtons as _MouseButtons

        def _drag_start(s, e) -> None:
            try:
                if e.Button == _MouseButtons.Left:
                    hwnd = form.Handle.ToInt32()
                    u = ctypes.windll.user32
                    u.ReleaseCapture()
                    u.SendMessageW(wintypes.HWND(hwnd), 0x00A1, 2, 0)
            except Exception:
                pass

        form.MouseDown += _drag_start
        pic.MouseDown += _drag_start
        bar.MouseDown += _drag_start
        lbl.MouseDown += _drag_start

        _SPLASH["form"] = form
        _SPLASH["bar"] = bar
        _SPLASH["bar_state"] = bar_state
        _SPLASH["lbl"] = lbl
        Application.Run(form)
    except Exception as ex:
        log(f"splash thread failed: {ex}")
    finally:
        _SPLASH["form"] = None


def _splash_apply_progress(form, pct: float, text: str | None) -> None:
    """splash 线程内 (Invoke 封送): 更新进度条百分比与状态文字。"""
    try:
        bar = _SPLASH.get("bar")
        bar_state = _SPLASH.get("bar_state")
        if bar is not None and bar_state is not None:
            bar_state["pct"] = max(0.0, min(100.0, float(pct)))
            bar.Invalidate()
        if text is not None:
            lbl = _SPLASH.get("lbl")
            if lbl is not None:
                lbl.Text = text
    except Exception:
        pass


def _splash_set_progress(pct: float, text: str | None = None) -> None:
    """主线程调用: 按启动阶段推进 splash 进度 (跨线程封送到 splash 线程)。"""
    form = _SPLASH.get("form")
    if form is None:
        return
    try:
        from System import Action
        form.Invoke(Action(lambda: _splash_apply_progress(form, pct, text)))
    except Exception:
        pass


def _show_fatal(title: str, msg: str) -> None:
    """启动关键步骤失败时的错误弹窗 (main 阶段, clr 可能尚未加载)。"""
    try:
        import clr  # noqa: F401
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import (MessageBox, MessageBoxButtons,
                                          MessageBoxIcon)
        MessageBox.Show(msg, title, MessageBoxButtons.OK, MessageBoxIcon.Warning)
    except Exception as ex:
        log(f"fatal dialog failed: {ex}")


def _start_splash() -> None:
    """启动加载窗 (独立线程, daemon): 构建/后端启动/等待就绪期间显示。"""
    try:
        threading.Thread(target=_splash_run, daemon=True, name="splash").start()
        # 等待 splash form 就绪: 否则紧随其后的 _splash_set_progress
        # (首次安装时 clone/fetch 紧接 splash 启动) 会因 form 未创建被
        # 直接丢弃, 表现为加载窗一直停在初始文字、进度条不动
        for _ in range(200):
            if _SPLASH.get("form") is not None:
                time.sleep(0.2)  # 再等 Application.Run 进入消息循环
                break
            time.sleep(0.05)
        log("splash started")
    except Exception as ex:
        log(f"splash start failed: {ex}")


def _cancel_startup() -> None:
    """用户点击 splash 关闭按钮: 取消启动流程, 终止进行中的子进程, 退出应用。"""
    if _ACTIVE["cancel"]:
        return
    _ACTIVE["cancel"] = True
    log("startup cancelled by user (splash close button)")
    p = _ACTIVE.get("proc")
    if p is not None and p.poll() is None:
        try:
            # 杀进程树并等待退出, 避免孤儿 git/ssh 进程残留文件锁
            _kill_proc_tree(p)
            log(f"cancelled active subprocess pid={p.pid}")
        except Exception as ex:
            log(f"cancel subprocess failed: {ex}")
    _close_splash()


def _close_splash() -> None:
    """关闭加载窗 (主窗口显示后 / 任何退出路径调用; 幂等)。"""
    form = _SPLASH.get("form")
    if form is None:
        return
    try:
        from System import Action
        form.Invoke(Action(form.Close))
    except Exception:
        try:
            form.Close()
        except Exception:
            pass
    log("splash closed")


def _install_dialog_chrome(form, title: str, dark: bool, scale: float,
                           on_close) -> int:
    """给无边框对话框安装自绘标题栏 (与主窗口风格一致)。

    背景=主题色, 左侧应用图标+标题文字, 右侧关闭按钮 (hover 变红),
    标题栏区域可拖动窗体 (ReleaseCapture + WM_NCLBUTTONDOWN/HTCAPTION)。
    返回标题栏高度 (逻辑像素), 调用方需把内容控件整体下移该高度。"""
    from System.Windows.Forms import Panel, Cursors
    from System.Drawing import (Color, Font, Pen, SolidBrush, Image,
                                FontStyle)
    from System.Drawing.Drawing2D import SmoothingMode
    from System.Drawing.Text import TextRenderingHint

    s = max(1.0, float(scale))
    tb_h = int(TITLEBAR_HEIGHT * s)  # 复用主窗口标题栏高度常量
    bg = TITLEBAR_THEMES["dark" if dark else "light"]["bg"]
    if dark:
        fg = (229, 231, 235)
        icon_rgb = (151, 157, 166)
    else:
        fg = (31, 41, 55)
        icon_rgb = (97, 102, 107)
    close_hover = (239, 68, 68)
    close_active = (196, 52, 52)

    panel = Panel()
    panel.BackColor = Color.FromArgb(bg[0], bg[1], bg[2])
    state = {"hover": False, "pressed": False, "icon": None}
    # 左侧应用图标 (与主窗口同源 png, 缩小显示)
    try:
        png = WINDOW_DIR / "deepseek娘.png"
        if png.is_file():
            state["icon"] = Image.FromFile(str(png))
    except Exception:
        pass

    def _close_rect():
        w = panel.ClientSize.Width
        bw = int(44 * s)
        return (w - bw, 0, bw, tb_h)

    def _font():
        try:
            return Font("Microsoft YaHei UI", 9.0, FontStyle.Regular)
        except Exception:
            return Font("Arial", 9.0)

    def _paint(sender, e) -> None:
        g = e.Graphics
        g.Clear(panel.BackColor)
        if state["icon"] is not None:
            size = int(16 * s)
            g.DrawImage(state["icon"], int(10 * s), (tb_h - size) // 2, size, size)
            text_x = int(32 * s)
        else:
            text_x = int(12 * s)
        font = _font()
        try:
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
            brush = SolidBrush(Color.FromArgb(fg[0], fg[1], fg[2]))
            try:
                size = g.MeasureString(title, font)
                g.DrawString(title, font, brush, text_x, (tb_h - size.Height) / 2.0)
            finally:
                brush.Dispose()
        finally:
            font.Dispose()
        # 右侧关闭按钮
        rx, ry, rw, rh = _close_rect()
        if state["pressed"]:
            col = close_active
        elif state["hover"]:
            col = close_hover
        else:
            col = None
        if col is not None:
            g.FillRectangle(SolidBrush(Color.FromArgb(col[0], col[1], col[2])),
                            rx, ry, rw, rh)
        x_rgb = (229, 231, 235) if (state["hover"] or state["pressed"]) else icon_rgb
        pen = Pen(Color.FromArgb(x_rgb[0], x_rgb[1], x_rgb[2]), max(1.0, 1.3 * s))
        g.SmoothingMode = SmoothingMode.AntiAlias
        cx = rx + rw / 2.0
        cy = ry + rh / 2.0
        try:
            g.DrawLine(pen, cx - 3.5 * s, cy - 3.5 * s, cx + 3.5 * s, cy + 3.5 * s)
            g.DrawLine(pen, cx + 3.5 * s, cy - 3.5 * s, cx - 3.5 * s, cy + 3.5 * s)
        finally:
            pen.Dispose()

    def _in_close(x: int, y: int) -> bool:
        rx, ry, rw, rh = _close_rect()
        return rx <= x < rx + rw and 0 <= y < rh

    def _mm(sender, e) -> None:
        hover = _in_close(e.X, e.Y)
        if hover != state["hover"]:
            state["hover"] = hover
            panel.Invalidate()
            try:
                panel.Cursor = Cursors.Hand if hover else Cursors.Default
            except Exception:
                pass

    def _ml(sender, e) -> None:
        if state["hover"]:
            state["hover"] = False
            panel.Invalidate()

    def _md(sender, e) -> None:
        if _in_close(e.X, e.Y):
            state["pressed"] = True
            panel.Invalidate()
            return
        # 拖动对话框 (与主窗口一致)
        try:
            user32 = ctypes.windll.user32
            hwnd = form.Handle.ToInt32()
            user32.ReleaseCapture()
            user32.SendMessageW(hwnd, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
        except Exception as ex:
            log(f"dialog drag failed: {ex}")

    def _mu(sender, e) -> None:
        was = state["pressed"]
        state["pressed"] = False
        panel.Invalidate()
        if was and _in_close(e.X, e.Y):
            try:
                on_close()
            except Exception as ex:
                log(f"dialog close failed: {ex}")

    panel.Paint += _paint
    panel.MouseMove += _mm
    panel.MouseLeave += _ml
    panel.MouseDown += _md
    panel.MouseUp += _mu

    def _layout(_s=None, _e=None) -> None:
        panel.SetBounds(0, 0, form.ClientSize.Width, tb_h)

    form.Resize += _layout
    _layout()
    form.Controls.Add(panel)
    return tb_h


# 切换版本期间的覆盖层脚本: 全屏盖住 webview 内容区。
# 背景/文字/图标颜色全部引用页面自身的 CSS 变量 (跟随当前主题, 不突变):
#   dark  : bg=--dsw-static-neutral-bluish-950  fg=--dsw-static-neutral-bluish-00
#   light : bg=--dsw-static-neutral-bluish-50   fg=--dsw-static-neutral-bluish-950
#   主色蓝: --dsw-static-blue-500 (旋转图标)
_UPDATING_OVERLAY_JS = """(() => {
  const id = '__dsh_updating__';
  if (document.getElementById(id)) return;
  const dark = document.body.hasAttribute('data-ds-dark-theme');
  const bg = dark ? 'var(--dsw-static-neutral-bluish-950)' : 'var(--dsw-static-neutral-bluish-50)';
  const fg = dark ? 'var(--dsw-static-neutral-bluish-00)' : 'var(--dsw-static-neutral-bluish-950)';
  const accent = 'var(--dsw-static-blue-500)';
  const cs = getComputedStyle(document.documentElement);
  const fgVal = (cs.getPropertyValue(dark ? '--dsw-static-neutral-bluish-00' : '--dsw-static-neutral-bluish-950') || '').trim();
  const ring = fgVal.startsWith('rgb') ? fgVal.replace('rgb(', 'rgba(').replace(')', ',0.25)') : 'rgba(128,128,128,0.25)';
  const el = document.createElement('div');
  el.id = id;
  el.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:20px;background:' + bg + ';color:' + fg + ';';
  el.innerHTML =
    '<div style="width:48px;height:48px;border:4px solid ' + ring + ';border-top-color:' + accent + ';border-radius:50%;animation:__dsh_spin 1.1s linear infinite;"></div>' +
    '<div style="font-size:15px;letter-spacing:2px;opacity:0.9;">版本切换中…</div>' +
    '<style>@keyframes __dsh_spin{to{transform:rotate(360deg)}}</style>';
  (document.body || document.documentElement).appendChild(el);
})()"""

_UPDATING_OVERLAY_HIDE_JS = (
    "(() => { const el = document.getElementById('__dsh_updating__');"
    " if (el) el.remove(); })()")


# git log 日期格式: 精确到秒 (YYYY-MM-DD HH:MM:SS, 本地时间)
_GIT_DATE_FMT = "--date=format:%Y-%m-%d %H:%M:%S"


def _list_local_commits(limit: int = 100) -> tuple[list[dict], str]:
    """读本地仓库 commit 列表 (新->旧) 与当前 HEAD。

    --no-merges 展开 merge commit。优先读拉取分支 origin/master;
    若 origin/master 缺失 (从未拉取成功) 则回退读当前分支 (HEAD) 历史,
    保证列表始终有内容。返回 (commits, head_hash)。"""
    def _parse(r) -> list[dict]:
        out: list[dict] = []
        if r[0] == 0 and r[1]:
            for line in r[1].splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    out.append({"hash": parts[0], "short": parts[0][:7],
                                "date": parts[1], "subject": parts[2]})
        return out

    fmt = ["--format=%H%x09%ad%x09%s", _GIT_DATE_FMT, "-n", str(limit)]
    commits = _parse(_git(["log", "origin/master", "--no-merges"] + fmt))
    if not commits:
        # origin/master 不存在 (从未拉取成功): 回退当前分支历史
        commits = _parse(_git(["log", "HEAD", "--no-merges"] + fmt))
    head_r = _git(["rev-parse", "HEAD"])
    head = head_r[1].strip() if head_r[0] == 0 else ""
    return commits, head


def _build_update_dialog(titlebar) -> "object | None":
    """构建升级对话框 (UI 线程调用): git log 风格版本单选列表 + 切换版本。

    返回 Form 或 None (无更新信息时)。独立可测: 传入含 _update_info /
    _dark / _scale / form 属性的对象即可 (见 DSH_DEMO_UPDATE 测试钩子)。"""
    from System.Windows.Forms import (
        Form, Label, RadioButton, Button, FormBorderStyle, DialogResult)
    from System.Drawing import Color, Point, Size, Font, FontStyle
    from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon

    info = getattr(titlebar, "_update_info", None)
    # 检测失败/未检测 (info=None) 也打开: 列表显示本地历史, 可选择切换
    if info is None:
        info = {}
    dark = bool(getattr(titlebar, "_dark", True))
    s = max(1.0, float(getattr(titlebar, "_scale", 1.0)))

    form = Form()
    form.Text = "升级 DSH Desktop"
    form.FormBorderStyle = FormBorderStyle(0)  # None (python 关键字冲突, 用枚举构造)
    # pythonnet 3.x: StartPosition 枚举无法直接 import, 用 Enum.ToObject 构造 CenterParent
    try:
        from System import Enum as _Enum
        form.StartPosition = _Enum.ToObject(form.StartPosition.GetType(), 4)
    except Exception:
        pass  # 缺省位置 (由系统摆放), 不影响功能
    form.ShowInTaskbar = False
    form.MaximizeBox = False
    form.MinimizeBox = False
    # 布局: 780x520 内容 + 自绘标题栏, 时间列显示到秒, 列表拉宽不拥挤
    form.ClientSize = Size(int(780 * s), int(520 * s))
    try:
        form.Font = Font("Microsoft YaHei UI", 9.5)
    except Exception:
        pass
    # 自绘标题栏 (主题色背景 + 图标 + 标题 + 关闭按钮, 可拖动)
    tb = _install_dialog_chrome(form, "升级 DSH Desktop", dark, s,
                                lambda: form.Close())
    form.ClientSize = Size(int(780 * s), int(520 * s) + tb)

    # 无边框窗口: DWM 圆角 + 边框色 = 主题背景色 (与主窗口一致)
    _theme_bg = TITLEBAR_THEMES["dark" if dark else "light"]["bg"]

    def _apply_dwm_border(_s=None, _e=None) -> None:
        try:
            hwnd = form.Handle.ToInt32()
            _dwm = ctypes.WinDLL("dwmapi")
            _dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            _dwm.DwmSetWindowAttribute.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            col = ctypes.c_int((_theme_bg[2] << 16) | (_theme_bg[1] << 8) | _theme_bg[0])
            _dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
            corner = ctypes.c_int(2)  # DWMWA_WINDOW_CORNER_PREFERENCE = ROUND
            _dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
            log(f"update dialog dwm border set: bg={_theme_bg}")
        except Exception as ex:
            log(f"update dialog dwm border failed: {ex}")

    form.Shown += _apply_dwm_border
    form.Shown += lambda s, e: _fit_columns()

    def theme(control) -> None:
        if dark:
            control.BackColor = Color.FromArgb(30, 30, 33)
            control.ForeColor = Color.FromArgb(229, 231, 235)

    if dark:
        form.BackColor = Color.FromArgb(21, 21, 23)
        form.ForeColor = Color.FromArgb(229, 231, 235)

    # ---------- 数据: 本地仓库拉取分支 (demo 模式用模拟数据) ----------
    demo = bool(os.environ.get("DSH_DEMO_UPDATE", "").strip())
    W = int(736 * s)
    X = int(22 * s)
    # 头部: 无背景框 (与窗体同色)
    lbl_head = Label()
    lbl_head.SetBounds(X, int(14 * s) + tb, W, int(36 * s))
    lbl_head.AutoSize = False
    lbl_head.Text = "已拉取最新仓库" if info else "连接github失败"
    lbl_head.BackColor = form.BackColor
    lbl_head.ForeColor = (Color.FromArgb(229, 231, 235) if dark
                          else Color.FromArgb(31, 41, 55))
    form.Controls.Add(lbl_head)

    # ---------- git 样式列表 (ListView: 单选框 / 短哈希 / 日期 / 提交说明) ----------
    from System.Windows.Forms import (ListView as _ListView, View as _View,
                                      ColumnHeaderStyle as _CHS, ListViewItem)
    lv = _ListView()
    lv.SetBounds(X, int(58 * s) + tb, W, int(330 * s))
    lv.View = _View.Details
    lv.FullRowSelect = True
    lv.MultiSelect = False
    lv.HideSelection = False
    lv.HeaderStyle = _CHS(0)            # None (隐藏表头, 更像 git log 文本)
    # 列宽总和预留竖向滚动条, 避免出现横向滚动条
    lv.Columns.Add("sel", int(34 * s))
    lv.Columns.Add("hash", int(88 * s))
    lv.Columns.Add("date", int(180 * s))
    lv.Columns.Add("subject", int(W - 324 * s))
    lv.BackColor = Color.FromArgb(30, 30, 33) if dark else Color.White
    lv.ForeColor = Color.FromArgb(229, 231, 235) if dark else Color.FromArgb(31, 41, 55)
    cur_fg = Color.FromArgb(148, 163, 184) if dark else Color.Gray
    cur_bg = Color.FromArgb(56, 56, 60) if dark else Color.FromArgb(230, 230, 232)
    # 单选列字体: ●/○ 是同一字体的配套几何符号 (外径一致), Segoe UI Symbol
    # 渲染更清晰且圈更大, 避免默认字体下未选中圈偏小、与选中圈对不上
    try:
        from System.Drawing import Font as _Font
        _radio_font = _Font("Segoe UI Symbol", 12.0)
    except Exception:
        _radio_font = None
    form.Controls.Add(lv)

    rows = []  # {"item": ListViewItem, "commit": dict, "current": bool}
    _sel_guard = [False]
    _last_sel = [0]

    def _populate_list() -> None:
        """读取本地仓库拉取分支的 commit 并填充列表 (打开/拉取最新后刷新)。"""
        nonlocal rows
        lv.BeginUpdate()
        try:
            lv.Items.Clear()
            if demo:
                commits = list(info.get("commits") or [])
                head = info.get("head") or ""
            else:
                commits, head = _list_local_commits(limit=100)
            # 当前版本不在列表时, 顶部插入"当前版本"行 (置灰不可选)
            if head and not any(c.get("hash") == head for c in commits):
                head_date = ""
                if demo:
                    head_date = "2026-01-01 08:30:00"
                else:
                    d = _git(["log", "-1", "--format=%ad", _GIT_DATE_FMT, head])
                    if d[0] == 0 and d[1]:
                        head_date = d[1].strip()
                commits.insert(0, {"hash": head, "short": head[:7],
                                   "date": head_date, "subject": ""})
            rows = []
            for c in commits:
                is_cur = bool(head) and c.get("hash") == head
                subject = c.get("subject", "")
                label = (subject + "  （当前版本）") if is_cur else subject
                # 第一列 = 单选框: 当前版本 ● 灰色选中; 其他 ○ 未选中
                # ●/○ 配套字形外径一致; 该列用 _radio_font (Segoe UI Symbol) 渲染
                item = ListViewItem("●" if is_cur else "○")
                if _radio_font is not None:
                    item.SubItems[0].Font = _radio_font
                item.SubItems.Add(c.get("short", "?"))
                item.SubItems.Add(c.get("date", ""))
                item.SubItems.Add(label)
                if is_cur:
                    item.ForeColor = cur_fg
                    item.BackColor = cur_bg     # 当前版本整行灰背景
                lv.Items.Add(item)
                rows.append({"item": item, "commit": c, "current": is_cur})
            # 默认选中第一个可选项 (官方最新): 选中后当前版本行状态不变 (灰色 ● 保留)
            for i, row in enumerate(rows):
                if not row["current"]:
                    lv.Items[i].Selected = True
                    _last_sel[0] = i
                    break
        finally:
            lv.EndUpdate()
            _fit_columns()

    def _fit_columns() -> None:
        """列宽总和铺满列表宽度: 无竖向滚动条时整行铺满 (行背景/高亮颜色完整
        显示到最右); 有竖向滚动条时给滚动条让位, 避免出现横向滚动条。"""
        try:
            scroll = 0
            if lv.Items.Count > 0:
                row_h = lv.Items[0].Bounds.Height
                if row_h > 0 and lv.ClientSize.Height / row_h < lv.Items.Count:
                    from System.Windows.Forms import SystemInformation
                    scroll = SystemInformation.VerticalScrollBarWidth + 2
            # 302 = sel(34) + hash(88) + date(180)
            lv.Columns[3].Width = max(80, lv.ClientSize.Width - 302 - scroll)
        except Exception as ex:
            log(f"fit columns failed: {ex}")

    # 单选列刷新: 选中行 ●, 其他可选项 ○, 当前版本行始终灰色 ● (状态不变)
    def _refresh_radio_col() -> None:
        sel = lv.SelectedIndices[0] if lv.SelectedIndices.Count > 0 else -1
        for i, row in enumerate(rows):
            if row["current"]:
                row["item"].SubItems[0].Text = "●"   # 当前版本: 灰选中 (ForeColor 已是灰)
            elif i == sel:
                row["item"].SubItems[0].Text = "●"   # 用户选中行
            else:
                row["item"].SubItems[0].Text = "○"
        lv.Invalidate()

    # 当前版本行不可选: 拦截选择并回退到上一个有效选择
    def _on_sel(sender, e) -> None:
        if _sel_guard[0] or lv.SelectedIndices.Count == 0:
            return
        idx = lv.SelectedIndices[0]
        if rows[idx]["current"]:
            _sel_guard[0] = True
            try:
                if _last_sel[0] < len(rows) and not rows[_last_sel[0]]["current"]:
                    lv.Items[_last_sel[0]].Selected = True
                else:
                    for i, row in enumerate(rows):
                        if not row["current"]:
                            lv.Items[i].Selected = True
                            _last_sel[0] = i
                            break
            finally:
                _sel_guard[0] = False
            _refresh_radio_col()
            return
        _last_sel[0] = idx
        _refresh_radio_col()

    lv.SelectedIndexChanged += _on_sel
    _populate_list()
    _refresh_radio_col()

    # ---------- 底部提示 + 按钮 (WebUI 风格: 圆角 + 主色蓝, 三个等宽) ----------
    lbl_status = Label()
    lbl_status.SetBounds(X, int(398 * s) + tb, W, int(36 * s))
    lbl_status.AutoSize = False
    lbl_status.Text = "选择对应commit后，点击切换版本后，立即生效"
    lbl_status.BackColor = form.BackColor
    if dark:
        lbl_status.ForeColor = Color.FromArgb(148, 163, 184)
    else:
        lbl_status.ForeColor = Color.Gray
    form.Controls.Add(lbl_status)

    from System.Windows.Forms import FlatStyle
    from System.Drawing import Region
    from System.Drawing.Drawing2D import GraphicsPath

    def _round_region(ctrl, radius: int):
        w, h = ctrl.Width, ctrl.Height
        path = GraphicsPath()
        d = 2 * radius
        path.AddArc(0, 0, d, d, 180, 90)
        path.AddArc(w - d, 0, d, d, 270, 90)
        path.AddArc(w - d, h - d, d, d, 0, 90)
        path.AddArc(0, h - d, d, d, 90, 90)
        path.CloseFigure()
        return Region(path)

    _radius = int(8 * s)
    _btn_h = int(36 * s)
    _gap = int(12 * s)
    _right = int(780 * s) - int(22 * s)
    _btn_y = int(444 * s) + tb
    _w_ok = int(100 * s)     # 切换版本 (主按钮)
    _w_fetch = int(120 * s)  # 获取最新仓库 (文字多, 稍宽)
    _w_cancel = int(80 * s)  # 取消 (文字少, 稍窄)

    def _secondary_btn(text: str) -> Button:
        b = Button()
        b.Text = text
        b.FlatStyle = FlatStyle.Flat
        b.FlatAppearance.BorderSize = 0
        if dark:
            b.BackColor = Color.FromArgb(30, 30, 33)
            b.ForeColor = Color.FromArgb(229, 231, 235)
            b.FlatAppearance.MouseOverBackColor = Color.FromArgb(47, 47, 49)
        # Region 必须在最终尺寸下重建 (SetBounds 后 Resize 触发), 否则按默认尺寸
        # (75x23) 裁剪, 视觉上高度与主按钮不一致
        b.Region = _round_region(b, _radius)
        b.Resize += lambda s, e: setattr(b, "Region", _round_region(b, _radius))
        return b

    btn_ok = Button()
    btn_ok.SetBounds(_right - _w_ok, _btn_y, _w_ok, _btn_h)
    btn_ok.Text = "切换版本"
    btn_ok.FlatStyle = FlatStyle.Flat
    btn_ok.FlatAppearance.BorderSize = 0
    btn_ok.BackColor = Color.FromArgb(37, 99, 235)              # WebUI 主按钮蓝 #2563EB
    btn_ok.FlatAppearance.MouseOverBackColor = Color.FromArgb(29, 78, 216)
    btn_ok.ForeColor = Color.White
    btn_ok.Region = _round_region(btn_ok, _radius)
    btn_ok.Resize += lambda s, e: setattr(btn_ok, "Region", _round_region(btn_ok, _radius))
    form.Controls.Add(btn_ok)

    btn_fetch = _secondary_btn("获取最新仓库")
    btn_fetch.SetBounds(_right - _w_ok - _gap - _w_fetch, _btn_y, _w_fetch, _btn_h)
    form.Controls.Add(btn_fetch)

    btn_cancel = _secondary_btn("取消")
    btn_cancel.SetBounds(_right - _w_ok - _gap - _w_fetch - _gap - _w_cancel,
                         _btn_y, _w_cancel, _btn_h)
    form.Controls.Add(btn_cancel)

    def _target_ref() -> str:
        if lv.SelectedIndices.Count > 0:
            return rows[lv.SelectedIndices[0]]["commit"]["hash"]
        return "origin/master"

    def _set_busy(busy: bool) -> None:
        # 忙碌期间 (拉取仓库/切换版本): 切换版本 + 获取最新仓库按钮都禁用,
        # 避免重复操作; 取消按钮始终可用 (用户仍可关闭对话框)。
        btn_ok.Enabled = not busy
        btn_fetch.Enabled = not busy
        btn_cancel.Enabled = True
        # 不禁用 lv: WinForms 禁用态会把深色背景画成系统白/灰

    def _set_status(m: str) -> None:
        try:
            lbl_status.Text = m
        except Exception:
            pass

    # ---------- "获取最新仓库": fetch 官方 master 并刷新列表 ----------
    def _fetch_latest() -> None:
        _set_busy(True)
        _set_status("正在拉取最新仓库…")
        log("manual fetch latest requested")

        def _work() -> None:
            try:
                info2 = check_for_update()   # 内部 git fetch + 分析
            except Exception as ex:
                log(f"manual fetch failed: {ex}")
                info2 = None
            try:
                from System import Action
                form.Invoke(Action(lambda: _fetch_done(info2)))
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    def _fetch_done(info2) -> None:
        try:
            if form.IsDisposed:
                return
            if info2 is not None:
                # 更新标题栏状态 (红点/蓝色) 与本地信息
                setter = getattr(titlebar, "set_update_info", None)
                if setter is not None:
                    setter(info2)
                lbl_head.Text = "已拉取最新仓库"
                _populate_list()          # 刷新列表 (可能出现新 commit)
                _set_status("已拉取最新仓库，选择目标后点击切换版本。")
            else:
                lbl_head.Text = "连接github失败"
                _set_status("拉取失败，请检查网络/代理后重试。")
            _set_busy(False)
        except Exception as ex:
            log(f"fetch done failed: {ex}")
            _set_busy(False)

    def _finish_update(ok: bool, msg: str, demo_mode: bool) -> None:
        """切换流程收尾 (对话框已关闭, 由主窗口 UI 线程调用)。

        成功: 保持覆盖层 -> 自动重建并重开画面 (demo 模式仅移除覆盖层);
        失败: 移除覆盖层 + 弹窗提示。"""
        try:
            if ok:
                if demo_mode:
                    hide = getattr(titlebar, "_hide_updating_overlay", None)
                    if hide is not None:
                        hide()
                else:
                    _start_rebuild_after_switch(titlebar)
                return
            hide = getattr(titlebar, "_hide_updating_overlay", None)
            if hide is not None:
                hide()
            try:
                MessageBox.Show(titlebar.form, msg, "切换失败",
                                MessageBoxButtons.OK, MessageBoxIcon.Warning)
            except Exception:
                pass
        except Exception as ex:
            log(f"update finish failed: {ex}")

    def _start_update() -> None:
        target = _target_ref()
        # 点击"切换版本": 立即关闭升级对话框, 切换与重建全程在后台进行,
        # 主界面显示"版本切换中…"覆盖层 (页面刷新后自动消失)
        form.DialogResult = DialogResult.OK
        form.Close()
        show_overlay = getattr(titlebar, "_show_updating_overlay", None)
        if show_overlay is not None:
            show_overlay()

        def _work() -> None:
            try:
                ok, msg = perform_update(target, progress=None, demo=demo)
            except Exception as ex:
                log(f"update work failed: {ex}")
                ok, msg = False, f"切换出错: {ex}"
            try:
                from System import Action
                titlebar.form.Invoke(Action(lambda: _finish_update(ok, msg, demo)))
            except Exception:
                _finish_update(ok, msg, demo)

        threading.Thread(target=_work, daemon=True).start()

    btn_ok.Click += lambda s, e: _start_update()
    btn_fetch.Click += lambda s, e: _fetch_latest()
    btn_cancel.Click += lambda s, e: form.Close()
    return form


def show_update_dialog(titlebar) -> None:
    """弹出升级对话框 (模态, UI 线程调用)。"""
    form = _build_update_dialog(titlebar)
    if form is None:
        return
    try:
        form.ShowDialog(getattr(titlebar, "form", None))
    except Exception as ex:
        log(f"update dialog show failed: {ex}")
    finally:
        try:
            form.Dispose()
        except Exception:
            pass


def inject_theme_sync(window) -> None:
    """页面就绪后注入主题监听脚本 (仅注入, 不改前端源码)。

    pywebview 的 loaded 事件在后台线程触发, 而 CoreWebView2 只能在 UI 线程
    访问 (STA): 后台线程直接访问会抛异常/阻塞。这里统一把注入动作封送到
    UI 线程执行, 只用 ExecuteScriptAsync (fire-and-forget), 不再 fallback
    到 window.evaluate_js —— 它同步等待 semaphore, 与 patch_eval 组合时
    在 loaded 初始化窗口期与 GUI 线程互锁, 造成窗体卡死。"""
    try:
        native = window.native
        if native is None:
            return

        def _inject() -> None:
            try:
                core = native.browser.webview.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(THEME_SYNC_SCRIPT)
                    log("theme sync script injected (async)")
                else:
                    log("theme sync: CoreWebView2 not ready")
            except Exception as e:
                log(f"theme sync async inject failed: {e}")

        if not native.InvokeRequired:
            # 已在 UI 线程 (本函数理论不在, 兜底)
            _inject()
        else:
            from System import Action
            native.Invoke(Action(_inject))
            log("theme sync script injected (ui-thread marshaled)")
    except Exception as e:
        log(f"theme sync inject failed: {e}")


def _patch_on_webview_ready() -> None:
    """Monkey-patch EdgeChrome.on_webview_ready: 在 pywebview 首次 load_url
    之前注册文档背景脚本 (html/body 主题色, 消灭启动白屏)。

    时序: pywebview 在 CoreWebView2InitializationCompleted 事件里立即
    load_url (首次导航)。AddScriptToExecuteOnDocumentCreatedAsync 只对
    注册后创建的文档生效 —— 若在 load_url 之后注册, 首次导航的文档
    (Loading 页) 不执行脚本, 其 body 背景 (var(--dsw-alias-bg-base) 无
    dark 属性时=白色) 露出白底。这里包一层原 handler, 在它执行
    (load_url) 之前注册脚本。"""
    try:
        from webview.platforms import edgechromium as _ec
    except Exception as e:
        log(f"on_webview_ready patch: import failed: {e}")
        return
    if getattr(_ec.EdgeChrome, "_dsh_wvready_patched", False):
        return
    _orig = _ec.EdgeChrome.on_webview_ready

    def _safe(self, sender, args) -> None:
        # 响应注入: 拦截根文档 HTML 响应, 在 <head> 注入主题背景色 style
        # (!important 锁定 html/body/#root/.boot)。文档创建时 HTML 已含 style,
        # 首帧即主题色, 消灭启动白屏 —— Loading 页 .boot 背景
        # (var(--dsw-alias-bg-base, #f9fafb)) 在 CSS 变量就绪前 fallback 近白。
        # 不用 AddScriptToExecuteOnDocumentCreatedAsync: 它是异步注册, 完成回调
        # 晚于首次文档创建, 首次导航的文档会漏执行注入脚本 (实测洋红验证)。
        try:
            pyw = getattr(self, "pywebview_window", None)
            rgb = getattr(pyw, "_dsh_init_bg_rgb", None)
            want_dark = bool(getattr(pyw, "_dsh_init_dark", True))
            wv = getattr(self, "webview", None)
            core = getattr(wv, "CoreWebView2", None) if wv is not None else None
            if rgb and core is not None:
                from System.IO import MemoryStream
                from System.Text import Encoding
                from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
                style = (
                    "<style id='__dsh_launcher_bg__'>" + _DSH_BG_SELECTORS
                    + " { background-color: rgb(%d,%d,%d) !important; }</style>"
                    % rgb
                )
                # 属性稳定器: 前端主题服务在本地浏览器先以 system 提供 (系统浅色时
                # dark 属性被改浅), 主界面渲染瞬间所有 --dsw-* 变量用 :root 浅色值
                # (输入框 card 背景等) -> 浅色闪。这里在启动初期 (10s 内) 只干预
                # 一次: 检测到 dark 属性被改成非偏好值 (即 system 初始覆盖) 时改回
                # 偏好值并立即释放观察器 —— 之后用户切换主题不被拦截 (不锁窗口),
                # 前端 adopt(偏好) 也正常。不锁背景, 不盖页面渐变/图案。
                guard = (
                    "<script>"
                    "(() => {"
                    "const wantDark = " + ("true" if want_dark else "false") + ";"
                    "document.body.toggleAttribute('data-ds-dark-theme', wantDark);"
                    "const t0 = Date.now();"
                    "let intervened = false;"
                    "const mo = new MutationObserver(() => {"
                    "if (intervened || Date.now() - t0 > 10000) { mo.disconnect(); return; }"
                    "if (document.body.hasAttribute('data-ds-dark-theme') !== wantDark) {"
                    "document.body.toggleAttribute('data-ds-dark-theme', wantDark);"
                    "intervened = true;"
                    "mo.disconnect();"
                    "}"
                    "});"
                    "mo.observe(document.body, { attributes: true,"
                    " attributeFilter: ['data-ds-dark-theme'] });"
                    "})()"
                    "</script>"
                )

                def _on_wrr(s, e) -> None:
                    try:
                        uri = str(e.Request.Uri)
                        if uri.rstrip("/") != URL:
                            return  # 只注入根文档, 其他 Document 请求放行
                        # 用 http.client 直接读 (不走系统代理, 快)
                        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
                        conn.request("GET", "/", headers={"Accept": "text/html"})
                        resp = conn.getresponse()
                        data = resp.read().decode("utf-8", "replace")
                        conn.close()
                        if "<head>" in data:
                            data = data.replace("<head>", "<head>" + style, 1)
                        if "<body>" in data:
                            data = data.replace("<body>", "<body>" + guard, 1)
                        ms = MemoryStream(Encoding.UTF8.GetBytes(data))
                        r2 = core.Environment.CreateWebResourceResponse(
                            ms, 200, "OK",
                            "Content-Type: text/html; charset=utf-8")
                        e.Response = r2
                        log("doc bg style + theme guard injected via response interception")
                    except Exception as ex:
                        log(f"doc bg response inject failed: {ex}")

                core.AddWebResourceRequestedFilter(
                    URL + "*", CoreWebView2WebResourceContext.Document)
                core.WebResourceRequested += _on_wrr
                log("doc bg response interception installed")
        except Exception as ex:
            log(f"pre-nav doc bg register failed: {ex}")
        _orig(self, sender, args)

    _ec.EdgeChrome.on_webview_ready = _safe
    _ec.EdgeChrome._dsh_wvready_patched = True
    log("edgechromium on_webview_ready patched (pre-nav doc bg)")


def _patch_evaluate_js() -> None:
    """Monkey-patch pywebview EdgeChrome.evaluate_js: 防止 UI 线程死锁。

    死锁机理: pywebview 的 evaluate_js 用 semaphore.acquire() 同步等待
    ExecuteScriptAsync 回调, 回调经 ContinueWith 排到 UI 线程同步上下文。
    若在 UI 线程调用 (如 NavigationCompleted/loaded 事件), UI 线程被
    acquire 阻塞, 回调排不上队 -> 窗体卡死但 WebView2 页面 (独立进程)
    动画照常。WebView2 空闲时 ExecuteScriptAsync 常同步完成 (回调内联),
    不触发; 渲染进程忙时 (首次初始化/大页面) 异步完成 -> 偶发卡死。

    patch: 检测到调用方在 UI 线程时, 把同步等待移到后台线程, UI 线程保持
    空闲, 回调能正常在 UI 上下文执行并 release。只改本文件, 不修改
    site-packages (PyInstaller 打包与 pip 重装 pywebview 均不受影响)。"""
    try:
        from webview.platforms import edgechromium as _ec
    except Exception as e:
        log(f"evaluate_js patch: import failed: {e}")
        return
    if getattr(_ec.EdgeChrome, "_dsh_eval_patched", False):
        return
    _orig = _ec.EdgeChrome.evaluate_js

    def _safe(self, script, parse_json):
        # InvokeRequired == False 表示当前线程即控件创建线程 (UI 线程)。
        # UI 线程绝不能同步等待跨线程操作 (Invoke 排队 + semaphore 等待 =
        # 互相死锁), 这里直接 fire-and-forget: pywebview 的注入调用
        # (inject_pywebview) 不关心返回值, 异步执行即可。
        try:
            if not self.webview.InvokeRequired:
                core = self.webview.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(script)
                    return None
        except Exception:
            pass
        return _orig(self, script, parse_json)

    _ec.EdgeChrome.evaluate_js = _safe
    _ec.EdgeChrome._dsh_eval_patched = True
    log("edgechromium evaluate_js patched (UI-thread deadlock guard)")


# 启动白屏/浅色闪消除: 背景锁定选择器。只锁 Loading 页容器 ([class*=boot]):
# 它 100% 高盖住 body, 消灭 .boot fallback 近白。不再锁 html/body/根容器
# (AppFrame frame / sidebar / ConversationRoot) —— 页面整背景有渐变/图案,
# 锁纯色会盖住它们出现黑框; 主界面渲染期的浅色闪改由响应注入的 theme guard
# (启动期强制 data-ds-dark-theme) 解决, 不依赖背景锁定。
_DSH_BG_SELECTORS = "[class*=boot], [class*=Boot]"


def _dsh_doc_bg_script(rgb: tuple[int, int, int]) -> str:
    """文档创建早期注入脚本: 启动/导航白屏消除 (不改前端代码)。

    白色来源: body 背景用 var(--dsw-alias-bg-base) (无 data-ds-dark-theme
    时 = 白色), Loading 页 .boot 背景 fallback #f9fafb (近白)。SPA 切页
    (Loading -> 主界面) 根节点挂载间隙, 这些容器短暂露出白底。
    这里:
      - html/body/#root/所有元素背景锁定为主题色 (!important)
      - 主题色写在 style 元素里 (React 不会清掉 head 里注入的 style)
      - MutationObserver 持续监控: 新挂载的元素/样式变化后重新应用,
        任何时刻页面背景都不会露出白色
    """
    color = "rgb(%d,%d,%d)" % (rgb[0], rgb[1], rgb[2])
    return (
        "(() => {"
        "const color = '" + color + "';"
        "const apply = () => {"
        "  try {"
        "    let st = document.getElementById('__dsh_launcher_bg__');"
        "    if (!st) {"
        "      st = document.createElement('style');"
        "      st.id = '__dsh_launcher_bg__';"
        "      (document.head || document.documentElement).appendChild(st);"
        "    }"
        "    // 白底来源: body 背景 var(--dsw-alias-bg-base) (无 dark 属性=白);"
        "    // Loading 页 .boot (CSS Module hash 化, 用属性包含匹配) fallback"
        "    // #f9fafb 近白; AppFrame/sidebar 列在主题 system 初始期用浅色。"
        "    // 全部 !important 锁死, 选择器与响应注入/_update_doc_bg 一致。"
        "    st.textContent = '" + _DSH_BG_SELECTORS + " { background-color: ' + color + ' !important; }';"
        "  } catch (e) {}"
        "};"
        "if (document.readyState === 'loading') {"
        "  document.addEventListener('DOMContentLoaded', apply);"
        "} else { apply(); }"
        "})()"
    )


def _patch_winforms_browser_form() -> None:
    """Monkey-patch pywebview WinForms: 消灭启动瞬间的 1px 白框闪烁。

    原理: pywebview 的 create_window() 里 `before_show.set()` 和
    `browser.Show()` 紧挨着执行 (winforms.py), before_show 等待线程醒来时
    窗口已开始显示, 所以必须在 Show() 内部、窗口可见之前同步设置 DWM。
    WinForms 保证 Form.Show() 在窗口真正可见 (WS_VISIBLE) 之前同步触发
    Load 事件, 且 BrowserForm.__init__ 已创建 HWND (self.Handle), 因此
    在 Load 里设置 DWMWA_BORDER_COLOR(34)=背景色 + 圆角(33)=ROUND 即可
    让窗口首帧就是主题色边框, 无白框闪现。

    实现: 把 BrowserView.BrowserForm 换成子类 (create_window 里只有一处
    构造它, 替换类引用即完全接管)。只改本文件, 不修改 site-packages:
    PyInstaller 打包与 pip 重装 pywebview 均不受影响。
    数据通过 window._dsh_init_bg_rgb 传入 (create_window 返回后、
    webview.start() 之前设置)。无该属性时子类什么都不做, 不影响 pywebview
    其他用法。
    """
    try:
        from webview.platforms import winforms as wf
    except Exception as e:
        log(f"winforms patch: import failed: {e}")
        return
    bv = getattr(wf, "BrowserView", None)
    base = getattr(bv, "BrowserForm", None)
    if base is None or getattr(base, "_dsh_patched", False):
        return

    class _DshPreShowForm(base):
        """BrowserForm 子类: Load (窗口显示前) 同步设置初始主题色。

        Load 事件在窗口显示前、WebView2 首次导航 (on_webview_ready -> load_url)
        之前触发, 这里把能提前的都设好:
          - DWM 边框色 = 主题背景色 (1px 边框隐形)
          - 圆角
          - WebView2 控件背景色 = 主题背景色 (控件自身不闪白)
          - 注册文档创建背景脚本 (AddScriptToExecuteOnDocumentCreatedAsync),
            早于 shown 事件的 install, 赶在首次导航前, 首帧 html/body 即主题色
        """

        def __init__(self, window, cache_dir):
            super().__init__(window, cache_dir)
            # [DEBUG] 确认控件实际使用的用户数据目录 (诊断 <exe>.WebView2 来源)
            try:
                log(f"[DEBUG] EdgeChrome user_data_folder = {getattr(self.browser, 'user_data_folder', 'N/A')!r}, cache_dir arg = {cache_dir!r}")
            except Exception as ex:
                log(f"[DEBUG] user_data_folder read failed: {ex}")
            self.Load += self._on_dsh_load
            # 初始化完成的第一时间注册文档背景脚本: pywebview 在初始化完成后
            # 立即 load_url (首次导航), 脚本必须赶在导航前注册才能盖住首帧
            # body 白底 (body background 用 var(--dsw-alias-bg-base), 无
            # data-ds-dark-theme 时为白色; 加载页 boot 也是白底)。
            try:
                wv = getattr(self, "webview", None)
                if wv is not None:
                    wv.CoreWebView2InitializationCompleted += self._on_dsh_wv_ready
            except Exception as ex:
                log(f"dsh wv init hook failed: {ex}")

        def _dsh_bg_rgb(self):
            return getattr(self.pywebview_window, "_dsh_init_bg_rgb", None)

        def _dsh_register_doc_bg(self) -> None:
            try:
                rgb = self._dsh_bg_rgb()
                if not rgb:
                    return
                wv = getattr(self, "webview", None)
                if wv is None or wv.CoreWebView2 is None:
                    return
                wv.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(
                    _dsh_doc_bg_script(rgb))
                log(f"doc bg script registered at wv-ready: {rgb}")
            except Exception as ex:
                log(f"doc bg register at wv-ready failed: {ex}")

        def _on_dsh_wv_ready(self, sender, args) -> None:
            # UI 线程, CoreWebView2 初始化完成 (pywebview 的 load_url 在其后)
            self._dsh_register_doc_bg()

        def _on_dsh_load(self, sender, e):
            try:
                rgb = getattr(self.pywebview_window, "_dsh_init_bg_rgb", None)
                if not rgb:
                    return
                hwnd = self.Handle.ToInt32()
                dwm = ctypes.WinDLL("dwmapi")
                dwm.DwmSetWindowAttribute.restype = ctypes.c_long
                dwm.DwmSetWindowAttribute.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
                # DWMWA_BORDER_COLOR(34) = 背景色 → 1px 边框隐形
                col = ctypes.c_int((rgb[2] << 16) | (rgb[1] << 8) | rgb[0])
                dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
                # DWMWA_WINDOW_CORNER_PREFERENCE(33) = ROUND(2)
                corner = ctypes.c_int(2)
                dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
                log(f"pre-show dwm: border color={rgb} corner=ROUND")
            except Exception as ex:
                log(f"pre-show dwm failed: {ex}")
            # WebView2 控件背景 = 主题色 (首次导航完成前控件区域不闪白)
            try:
                wv = getattr(self, "webview", None)
                if wv is not None:
                    from System.Drawing import Color as _GColor
                    wv.DefaultBackgroundColor = _GColor.FromArgb(
                        255, rgb[0], rgb[1], rgb[2])
                    log(f"pre-show webview bg color={rgb}")
            except Exception as ex:
                log(f"pre-show webview bg failed: {ex}")
            # 注册文档创建背景脚本: 在首次导航前注入, 首帧 html/body 即主题色。
            # 若 CoreWebView2 已就绪直接注册; 否则由 _on_dsh_wv_ready 兜底。
            self._dsh_register_doc_bg()

    _DshPreShowForm._dsh_patched = True
    wf.BrowserView.BrowserForm = _DshPreShowForm
    log("winforms BrowserForm patched (pre-show initial theme colors)")
    # [DEBUG] 追踪 winforms cache_dir 的实际值 (诊断 <exe>.WebView2 来源)
    _orig_init_storage = wf.init_storage

    def _dsh_trace_init_storage():
        _orig_init_storage()
        log(f"[DEBUG] winforms init_storage -> cache_dir = {wf.cache_dir!r}")

    wf.init_storage = _dsh_trace_init_storage


def main() -> int:
    log(f"launcher started, base={BASE}, source={SOURCE}")

    # 0) 单实例判定: 已有实例 -> 通知其显示窗口, 本实例立即退出
    #    (必须放在构建/后端之前, 第二实例不得干扰第一实例的后端)
    if not _acquire_single_instance():
        log("exiting: another instance already running")
        return 0
    threading.Thread(target=_watch_show_window_event, daemon=True).start()

    # 0.5) 启动加载窗 (构建/后端启动/等待就绪期间显示, 主窗口显示后关闭)
    _start_splash()
    _splash_set_progress(3, "正在启动 DSH Desktop…")

    # 0.6) 首次安装: release 包不带后端仓库, 首次启动用内嵌 git 拉取官方仓库
    if not _repo_valid():
        if _git_worktree_ok():
            # 有有效 .git: 断点续传 (进度条从 0 走是本次传输进度, 不是重新下载)
            _splash_set_progress(5, "首次安装：检测到已下载内容，正在续传…")
        else:
            _splash_set_progress(5, "首次安装：正在从官方仓库拉取代码（需联网）…")
        if not _clone_repo():
            if _ACTIVE["cancel"]:
                log("startup cancelled during clone")
                return 0
            _close_splash()
            log("first-run clone failed")
            if _git_worktree_ok():
                # .git 有效 = 网络拉取失败, 已下载部分可续传
                _show_fatal("拉取官方仓库失败",
                            "无法从官方仓库拉取代码（网络或代理问题）。\n"
                            "建议配置好 GitHub SSH key（推荐）：SSH 拉取最稳定，\n"
                            "不受 HTTPS 认证/限流影响，请检查网络后重新启动应用。\n"
                            "已下载的部分已保留，下次启动会自动继续，无需删除文件夹；\n"
                            "若直连失败，可设置 DSH_GIT_PROXY 代理后重试。")
            else:
                # .git 无效 = 初始化/残留清理失败 (权限等)
                _show_fatal("拉取官方仓库失败",
                            "无法初始化官方仓库（残留目录清理失败，可能权限不足）。\n"
                            f"请手动删除 {SOURCE} 后重试，或以管理员身份运行。")
            return 1
    # 依赖完整判定: 以 pnpm 的完成标记 node_modules/.modules.yaml 为准。
    # 只查目录存在会误判"上次失败留下的部分 node_modules"为已装好,
    # 导致跳过 install 直接 build 失败。
    if not (SOURCE / "node_modules" / ".modules.yaml").is_file():
        _splash_set_progress(30, "首次安装：正在安装依赖（需要几分钟）…")
        if not _install_deps():
            if _ACTIVE["cancel"]:
                log("startup cancelled during pnpm install")
                return 0
            _close_splash()
            log("first-run pnpm install failed")
            _show_fatal("安装依赖失败",
                        "依赖安装未完成（网络或磁盘问题）。\n请检查后重新启动应用。")
            return 1

    # 1) 检测更新, 需要时构建
    rebuild_needed, cur_fp = needs_build()
    if rebuild_needed:
        _splash_set_progress(10, "正在构建后端（首次启动需要几分钟）…")
        if not run_build():
            if _ACTIVE["cancel"]:
                log("startup cancelled during build")
                return 0
            log("build failed, see build console window")
            _close_splash()
            return 1
        if cur_fp:
            record_fingerprint(cur_fp)
        _splash_set_progress(60, "构建完成")

    # 2) 创建 Job (KILL_ON_JOB_CLOSE): 本进程退出 -> 后端必死 (内核级, 含强杀)
    global _JOB_HANDLE
    _JOB_HANDLE = _create_kill_job()

    # 3) 启动后端 (强相关: 尽量由本进程启动并纳入 Job)
    proc = None
    started_by_us = False
    port_in_use = port_open("127.0.0.1", PORT)
    if port_in_use:
        pid = _find_listener_pid(PORT)
        if pid is not None and _is_our_backend(pid):
            # 残留的旧后端 (非本进程 Job 管理): 杀掉重启, 纳入新 Job 保证强相关
            log(f"residual backend pid={pid}, killing and restarting under job")
            kill_tree(pid)
            for _ in range(20):
                if not port_open("127.0.0.1", PORT):
                    break
                time.sleep(0.25)
            port_in_use = port_open("127.0.0.1", PORT)
        else:
            # 被非 DSH 进程占用: 不敢杀, 只能复用 (此场景无法保证强相关)
            log("port occupied by non-DSH process, reusing (no job control)")
    if not port_in_use:
        log("port free, starting backend")
        _splash_set_progress(65, "正在启动后端…")
        proc = start_backend()
        started_by_us = True
        if _assign_pid_to_job(_JOB_HANDLE, proc.pid):
            log(f"backend pid={proc.pid} assigned to kill-on-close job")
        else:
            log("assign to job failed, fallback to kill_tree on exit")

    # 4) 等待就绪
    deadline = time.time() + WAIT_TIMEOUT
    ready = False
    t0 = time.time()
    while time.time() < deadline:
        if _ACTIVE["cancel"]:
            break
        if proc is not None and proc.poll() is not None:
            log(f"backend exited early with code={proc.returncode}")
            break
        if http_ready():
            ready = True
            break
        time.sleep(0.5)
        # 等待就绪期间进度随时间平滑推进 (65% → 95%, 封顶)
        _splash_set_progress(min(95.0, 65.0 + (time.time() - t0) / WAIT_TIMEOUT * 30.0))
    if ready:
        log(f"backend ready after {time.time() - t0:.1f}s")
        _splash_set_progress(98, "后端就绪，正在打开界面…")

    if not ready:
        if _ACTIVE["cancel"]:
            log("startup cancelled while waiting for backend")
            if started_by_us and proc is not None:
                kill_tree(proc.pid)
            _close_splash()
            return 0
        log("backend NOT ready in time")
        if started_by_us and proc is not None:
            kill_tree(proc.pid)
        log(f"backend not ready within {WAIT_TIMEOUT}s; check pnpm install in Source")
        _close_splash()
        return 1

    # 5) WebView2 窗口 (frameless + 原生自绘标题栏)
    try:
        import webview
    except ImportError as e:
        if started_by_us and proc is not None:
            kill_tree(proc.pid)
        log(f"webview import failed: {e}; run 00_env.bat (creates DSH_Desktop/.venv with pywebview)")
        _close_splash()
        return 1

    # 启动前 patch pywebview WinForms: BrowserForm 在 Load 事件 (窗口显示前)
    # 同步设置 DWM 边框色=背景色, 消灭启动瞬间的 1px 白框闪烁。
    _patch_winforms_browser_form()
    # 防 UI 线程死锁: pywebview evaluate_js 同步等待在 UI 线程调用会死锁
    # (窗体卡死但页面在动), patch 成异步 fire-and-forget。
    pass  # evalpatch disabled (封送 UI 线程后不再需要)
    # 响应注入文档背景色 (消灭启动白屏), 见 _patch_on_webview_ready。
    _patch_on_webview_ready()

    api = WindowApi()

    log("opening WebView2 window (frameless, custom titlebar)")
    # 初始背景色/边框色: 启动时直接从前端主题 CSS 读取 token, 不硬编码颜色;
    # 跟随系统主题 (前端默认偏好 'system'), 避免启动瞬间窗口全白闪烁。
    tokens = read_theme_tokens()
    dark_bg = tokens[0] if tokens else (21, 21, 23)
    light_bg = tokens[1] if tokens else (249, 250, 251)
    # 初始背景色: 先读应用自己的主题偏好 (settings.yaml 的 ui-theme.preference),
    # 而不是用系统主题猜测 —— 用户配置 light/dark 与系统不一致时窗口首帧即正确。
    # 顺序: 读配置 -> 初始化窗口 (background_color) -> 再 show, 避免"一开始全白"。
    init_dark = resolve_initial_dark()
    init_bg_rgb = dark_bg if init_dark else light_bg
    init_bg = "#%02X%02X%02X" % init_bg_rgb
    window = webview.create_window(
        "DSH Desktop",
        URL,
        width=1366,
        height=860,
        min_size=(1024, 700),
        frameless=True,
        easy_drag=False,  # 关闭 pywebview 全窗拖动 JS (否则网页任意处拖动都会移动窗口)
        text_select=True,  # 允许网页文本选择/复制 (pywebview 默认注入 user-select:none)
        shadow=False,  # 关闭 DWM 扩展帧 (ExtendFrameIntoClientArea 会让系统绕过 WM_NCHITTEST)
        background_color=init_bg,
        js_api=api,
    )
    # 给 patch 后的 BrowserForm 子类提供初始背景色 (Load 事件里设 DWM 边框色用)
    window._dsh_init_bg_rgb = init_bg_rgb
    # 响应注入的 theme guard 用: 启动期强制 dark 属性 = 用户偏好
    window._dsh_init_dark = init_dark
    global _MAIN_WINDOW
    _MAIN_WINDOW = window
    # 不挂 closing 杀后端: 关窗 = 隐藏到托盘 (FormClosing 拦截), 后端继续跑;
    # 真正退出走托盘"退出" -> 统一清理在 webview.start() 返回后 + Job 兜底。
    window.events.closed += lambda: log("window closed event fired")

    def setup_before_show() -> None:
        """窗口显示前 (before_show 事件) 设置 DWM 边框色=背景色 + 圆角,
        避免启动瞬间出现系统默认的白色 1px 边框。"""
        try:
            window.events.before_show.wait(timeout=15)
        except Exception:
            return
        try:
            from System import Action
            from ctypes import wintypes as _wt

            def _apply() -> None:
                try:
                    form = window.native
                    hwnd = form.Handle.ToInt32()
                    dwm = ctypes.WinDLL("dwmapi")
                    dwm.DwmSetWindowAttribute.restype = ctypes.c_long
                    dwm.DwmSetWindowAttribute.argtypes = [
                        _wt.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
                    # DWMWA_BORDER_COLOR(34) = 背景色 → 1px 边框隐形
                    col = ctypes.c_int(
                        (init_bg_rgb[2] << 16) | (init_bg_rgb[1] << 8) | init_bg_rgb[0])
                    dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
                    # DWMWA_WINDOW_CORNER_PREFERENCE(33) = ROUND(2)
                    corner = ctypes.c_int(2)
                    dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
                    log(f"before_show: border color={init_bg_rgb} corner=ROUND")
                except Exception as ex:
                    log(f"before_show apply failed: {ex}")

            try:
                window.native.Invoke(Action(_apply))
            except Exception:
                _apply()
        except Exception as ex:
            log(f"before_show setup failed: {ex}")

    threading.Thread(target=setup_before_show, daemon=True).start()

    def install_titlebar() -> None:
        # UI 线程: 原生标题栏 + 边缘缩放 + 系统托盘
        global _MAIN_FORM
        try:
            bar = TitleBar(window)
            bar.install()
            api.bind(bar)
            bar.start_update_checker()  # 后台定期检测官方仓库更新 (蓝色提示)
            window._titlebar_ref = bar  # 保活, 防 GC 导致事件失效
            log("custom titlebar installed")
        except Exception as e:
            log(f"titlebar install failed: {e}")
        # 记录 form 引用 (托盘"显示窗口/退出"用)
        try:
            _MAIN_FORM = window.native
        except Exception as ex:
            log(f"main form ref failed: {ex}")
        # 拦截窗口关闭 (X 按钮/Alt+F4): 非退出模式 -> 隐藏到托盘
        try:
            form = window.native

            def _on_form_closing(sender, args) -> None:
                if not _ALLOW_CLOSE:
                    args.Cancel = True
                    _hide_main_window()

            form.FormClosing += _on_form_closing
            log("form closing interception installed (close -> tray)")
        except Exception as ex:
            log(f"form closing interception failed: {ex}")
        # 系统托盘 (右键: 显示窗口 / 退出)
        try:
            _setup_tray(window.native)
        except Exception as ex:
            log(f"tray setup failed: {ex}")

    def on_shown() -> None:
        # shown 时窗口已创建 (start 回调在创建前, native 尚为 None)
        try:
            from System import Action
            window.native.Invoke(Action(install_titlebar))
        except Exception as e:
            log(f"titlebar install invoke failed: {e}")

    def on_before_show() -> None:
        """窗口即将显示时激活到前台 (最早时机, 不等延迟)。

        exe 从其他应用背后打开时, 窗口必须跳到最前而不是出现在背后。
        Windows 前台锁只放行"最近有用户输入"的进程, 模拟一次 ALT 按键
        (keybd_event) 解锁, 再 SetForegroundWindow —— 显示前执行, 窗口
        一出现即在最前。"""
        try:
            from System import Action
            window.native.Invoke(Action(_activate_foreground))
        except Exception as e:
            log(f"before_show activate invoke failed: {e}")

    def _activate_foreground() -> None:
        try:
            hwnd = window.native.Handle.ToInt32()
            user32 = ctypes.windll.user32
            # 模拟 ALT 键按下/释放, 解除前台锁限制 (标准绕过手法)
            user32.keybd_event(0x12, 0, 0, 0)        # VK_MENU down
            user32.keybd_event(0x12, 0, 2, 0)        # VK_MENU up (KEYEVENTF_KEYUP)
            user32.ShowWindow(hwnd, 9)               # SW_RESTORE (若最小化)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.SetActiveWindow(hwnd)
            log("window activated to foreground (before_show)")
        except Exception as ex:
            log(f"window activate failed: {ex}")

    def on_loaded() -> None:
        inject_theme_sync(window)

    window.events.before_show += on_before_show
    window.events.shown += on_shown
    window.events.shown += _close_splash  # 主窗口显示后关闭加载窗
    window.events.loaded += on_loaded
    # 清理已退出实例残留的独立 WebView2 数据目录 (本实例目录随窗口创建)
    _cleanup_old_webview2_dirs()
    log(f"[DEBUG] storage_path passed to webview.start = {str(_webview2_data_dir())!r}")
    webview.start(storage_path=str(_webview2_data_dir()))
    log("launcher exiting")
    # 清理托盘 (进程即将退出, 图标随之消失)
    try:
        if _TRAY is not None:
            _TRAY.Visible = False
            _TRAY.Dispose()
    except Exception as ex:
        log(f"tray cleanup failed: {ex}")
    # 无论窗口以何种方式关闭 (X 按钮/Alt+F4/托盘退出), 统一清理后端:
    # 显式杀进程树 + 关闭 Job 句柄 (KILL_ON_JOB_CLOSE 兜底, 防强杀/崩溃)。
    if started_by_us and proc is not None:
        log("launcher exit, killing backend tree")
        kill_tree(proc.pid)
    if _JOB_HANDLE is not None:
        _kernel32.CloseHandle(_JOB_HANDLE)
        log("kill-on-close job handle closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
