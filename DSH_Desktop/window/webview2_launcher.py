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
    5. 从后端日志解析 `dsh web: <带 token 的 URL>` (新版后端的浏览器会话
       认证: 裸 URL 一律 401), 弹出 WebView2 窗口加载该 URL 完成认证
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
import re
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
# 后端打印 'dsh web: <url>' 的等待上限: 该 URL 行在 Loader 树 settle 后打印,
# http_ready 探测到 401 (认证模式下服务已就绪) 时 announceReady 通常已执行
# 或即将执行, 轮询数秒内即可命中; 超时回退裸 URL (旧版后端/异常场景)。
TOKEN_WAIT_SECONDS = float(os.environ.get("DSH_TOKEN_WAIT", "8"))
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

# ==================== 桌面端设置持久化 ====================
# 应用级桌面设置 (与 harness 的 settings.yaml 分开, 只存桌面端的本地偏好)。
# 目前一项: 关闭窗口的行为 = "结束进程"(真正退出/含后端) 或 "隐藏到系统托盘"
# (后端继续跑, 托盘"退出"才真正退出)。默认隐藏到托盘 (与原行为一致)。
DEFAULT_CLOSE_BEHAVIOR = "tray"          # "tray"=隐藏到托盘 | "exit"=结束进程
APP_CONFIG_FILE = DATA_DIR / "app-config.json"


def _app_config() -> dict:
    """读取桌面端本地配置 (JSON), 失败/缺失返回空 dict。"""
    import json
    try:
        if APP_CONFIG_FILE.is_file():
            return json.loads(APP_CONFIG_FILE.read_text(encoding="utf-8", errors="ignore"))
    except Exception as ex:
        log(f"app config read failed: {ex}")
    return {}


def _save_app_config(conf: dict) -> None:
    """原子写桌面端本地配置 (JSON)。"""
    import json
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = APP_CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(conf, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(APP_CONFIG_FILE)
    except Exception as ex:
        log(f"app config write failed: {ex}")


def get_close_behavior() -> str:
    """关闭窗口行为: "tray"=隐藏到托盘 | "exit"=结束进程。"""
    v = _app_config().get("close_behavior")
    return v if v in ("tray", "exit") else DEFAULT_CLOSE_BEHAVIOR


def set_close_behavior(v: str) -> None:
    """设置关闭窗口行为并持久化。"""
    if v not in ("tray", "exit"):
        v = DEFAULT_CLOSE_BEHAVIOR
    conf = _app_config()
    conf["close_behavior"] = v
    _save_app_config(conf)
    log(f"close behavior set to {v}")


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
        # DSH 控制按钮 GroupBox: 填充 (略亮于背景) + 框线 (可见)
        "card": (33, 34, 38), "outline": (78, 82, 90),
    },
    "light": {
        "bg": (249, 250, 251), "hover": (232, 232, 234), "active": (219, 219, 222),
        "icon": (97, 102, 107), "close_hover": (239, 68, 68),
        "close_active": (196, 52, 52),
        "upd": (37, 99, 235), "upd_hover": (29, 78, 216),
        "card": (255, 255, 255), "outline": (203, 207, 214),
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


# ==================== 日志输出区 (控制面板右侧) ====================
# 所有 cmd 输出 (git 拉取/切换、环境更新、构建、启动) 既写文件 (log) 也
# 实时追加到控制面板右侧日志文本框。控制面板创建后通过 _set_log_sink 注册
# 一个在 UI 线程追加文本的回调; log_ui 封送该回调并在后台线程也能安全调用。
_LOG_SINK = {"cb": None}  # cb(text) 在 UI 线程追加
# 内存日志 buffer (带时间戳行): 控制面板创建前 (首次 clone/install 阶段) 的
# 输出先进 buffer, 面板创建后一次性回填, 保证"所有 cmd 输出都在日志区"。
_LOG_BUFFER: list[str] = []
_LOG_BUFFER_MAX = 8000


def _set_log_sink(cb) -> None:
    """设置 UI 日志回调 (控制面板创建时调用)。cb(text) 会附加到日志区。"""
    _LOG_SINK["cb"] = cb


def log_ui(text: str) -> None:
    """把一行日志同时写入文件并追加到 UI 日志区 (任意线程可调, 内部封送)。"""
    text = str(text)
    if text:
        try:
            log(text.rstrip())
        except Exception:
            pass
    # 追加到内存 buffer (面板未创建时也不会丢)
    try:
        _LOG_BUFFER.append(text)
        if len(_LOG_BUFFER) > _LOG_BUFFER_MAX:
            del _LOG_BUFFER[: len(_LOG_BUFFER) - _LOG_BUFFER_MAX]
    except Exception:
        pass
    cb = _LOG_SINK.get("cb")
    if cb is not None:
        try:
            cb(text)
        except Exception:
            pass


def _log_buffer_snapshot() -> list[str]:
    return list(_LOG_BUFFER)


def _log_ui_ts(text: str) -> None:
    """带时间戳的日志行 (UI 展示用, 去掉文件里已有的前缀避免重复)。"""
    if str(text).strip():
        log_ui(f"[{time.strftime('%H:%M:%S')}] {text}")


# 后台任务占位: 控制面板上各按钮触发的不阻塞 UI 的后台线程操作。
# 同一时刻只允许一个任务 (构建/环境/拉取/切换等) 运行, 防并发互相踩。
_panel_busy = {"flag": False}


def _panel_is_busy() -> bool:
    return bool(_panel_busy["flag"])


def _set_panel_busy(busy: bool) -> None:
    _panel_busy["flag"] = bool(busy)
    _notify_panel_busy()


def _notify_panel_busy() -> None:
    """忙碌状态变化时通知控制面板刷新按钮启用 (有回调则调用)。"""
    ui = getattr(sys, "_dsh_control_panel", None)
    if ui is not None:
        try:
            from System import Action
            form = getattr(ui, "form", None)
            if form is not None:
                form.Invoke(Action(lambda: getattr(ui, "refresh_buttons", lambda: None)()))
        except Exception:
            pass


def _run_captured(cmd, cwd=None, env=None, timeout=None,
                  emit_lines: bool = True, prefix: str = "") -> tuple[int, list[str]]:
    """在仓库 (SOURCE) 内运行命令, 逐行捕获 stdout/stderr 追加到日志区。

    替代旧的"弹独立控制台窗口" (_show_console_step / run_build): 不闪控制台,
    输出实时回填到控制面板右侧日志区。cmd 为 list (Popen list 模式) 或字符串。
    返回 (returncode, lines)。支持取消 (_ACTIVE["cancel"]) 与超时杀进程树。"""
    flags, si = _no_window_startup()
    lines: list[str] = []

    def _emit(line: str) -> None:
        line = line.rstrip("\r\n")
        if not line:
            return
        lines.append(line)
        if emit_lines and prefix:
            _log_ui_ts(prefix + line)
        elif emit_lines:
            _log_ui_ts(line)

    try:
        if isinstance(cmd, str):
            p = subprocess.Popen(cmd, cwd=str(cwd) if cwd is not None else str(SOURCE),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=env if env is not None else _node_env(),
                                 creationflags=flags, startupinfo=si,
                                 text=True, encoding="utf-8", errors="replace")
        else:
            p = subprocess.Popen(list(cmd),
                                 cwd=str(cwd) if cwd is not None else str(SOURCE),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=env if env is not None else _node_env(),
                                 creationflags=flags, startupinfo=si,
                                 text=True, encoding="utf-8", errors="replace")
    except OSError as ex:
        _emit(f"[exec error] {ex}")
        return -1, lines
    _ACTIVE["proc"] = p

    def _reader(p=p):
        try:
            for line in p.stdout:
                _emit(line)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.time() + timeout if timeout else None
    rc: int | None = None
    try:
        while True:
            try:
                rc = p.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if deadline is not None and time.time() > deadline:
                    _emit(f"[timeout after {timeout}s]")
                    try:
                        kill_tree(p.pid)
                    except Exception:
                        pass
                    break
                if _ACTIVE["cancel"]:
                    _emit("[cancelled by user]")
                    try:
                        kill_tree(p.pid)
                    except Exception:
                        pass
                    break
                continue
    finally:
        _ACTIVE["proc"] = None
    try:
        if p.stdout:
            p.stdout.close()
    except Exception:
        pass
    log(f"run_captured finished, rc={rc}")
    return (rc if rc is not None else -1), lines


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


def _clean_build_artifacts() -> None:
    """构建前删除仓库内旧的构建产物 (lib/ dist/ .dsh-build/ .typecheck/ *.tsbuildinfo)。

    根因: tsc -b 增量编译会把旧 lib/ 残留下来 (含已删除/重命名 API 的过时
    import), tsdown 以 lib/ 为输入打包时报 MISSING_EXPORT (如
    @deepseek-ai/dsh-api-remotes 的 ApiRemoteSessionNotFound 等)。每次构建前
    清理, 保证产物与当前源码严格一致。删除范围与官方 `pnpm run clean`
    (scripts/clean.ts) 一致, 但不依赖 tsx / node 可用性。
    只删 gitignore 的产物目录, 跳过 node_modules / .git (不碰依赖与版本库)。"""
    import shutil
    if not SOURCE.is_dir():
        return
    target_names = {"lib", "dist", ".dsh-build", ".typecheck"}
    skip = {"node_modules", ".git"}
    dirs = 0
    files = 0
    try:
        for dirpath, dirnames, filenames in os.walk(str(SOURCE), topdown=True):
            dirnames[:] = [d for d in dirnames if d not in skip]
            p = Path(dirpath)
            if p != SOURCE and p.name in target_names:
                try:
                    shutil.rmtree(str(p))
                    dirs += 1
                    dirnames[:] = []  # 已整树删除, 不再下钻
                except OSError as ex:
                    log(f"clean artifacts: remove failed {p}: {ex}")
                continue
            for fn in filenames:
                if fn.endswith(".tsbuildinfo"):
                    try:
                        (p / fn).unlink()
                        files += 1
                    except OSError as ex:
                        log(f"clean artifacts: remove failed {p / fn}: {ex}")
    except OSError as ex:
        log(f"clean artifacts: walk failed: {ex}")
    log(f"clean artifacts done (dirs={dirs}, files={files})")


def _deps_need_update() -> bool:
    """判断是否需要重新安装环境依赖 (pnpm install)。

    pnpm v9+ 安装后会把 pnpm-lock.yaml 原样复制到 node_modules/.pnpm/lock.yaml;
    两者一致 = 依赖与 lockfile 匹配, 无需更新; 缺失/不一致 = 需要。
    首次安装 (无 node_modules) 同样返回 True (缺失判定)。"""
    lock = SOURCE / "pnpm-lock.yaml"
    installed_marker = SOURCE / "node_modules" / ".pnpm" / "lock.yaml"
    if not lock.is_file() or not installed_marker.is_file():
        return True
    try:
        return lock.read_bytes() != installed_marker.read_bytes()
    except OSError:
        return True


def _show_console_step(title: str, body: str, cwd=None, env=None,
                       stdin_data: str = "", timeout: float | None = None) -> bool:
    """执行 body 中的命令 (cmd /S /c 字符串), 输出实时捕获到日志区。

    原实现弹独立可见控制台窗口 (CREATE_NEW_CONSOLE); 现改为静默执行并把
    stdout/stderr 逐行追加到控制面板右侧日志区 (需求: 所有 cmd 输出显示在
    日志区)。body 由调用方拼完整命令串; 成功/失败以子进程返回码判定
    (body 内的 `exit 0` 让成功路径返回 0; 失败路径靠非 0 返回码, 不再
    pause 弹窗)。支持超时与 splash 关闭取消 (_ACTIVE["cancel"])。"""
    _log_ui_ts("=" * 44)
    _log_ui_ts(f"{title} 开始…" if title else "开始执行…")
    cmd_str = 'cmd /S /c "' + body + '"'
    rc, _lines = _run_captured(cmd_str, cwd=cwd, env=env,
                               timeout=timeout, prefix="  ")
    if rc == 0:
        _log_ui_ts(f"{title} 完成。" if title else "完成。")
    else:
        _log_ui_ts(f"{title} 失败 (exit code={rc})。" if title else f"失败 (exit code={rc})。")
    _log_ui_ts("=" * 44)
    ok = rc == 0
    log(f"console step '{title}' finished, ok={ok}")
    return ok


def run_build() -> bool:
    log("starting build (output captured to log panel)")
    _log_ui_ts("=" * 44)
    _log_ui_ts("前后端构建 (pnpm run build) 开始…")
    # 构建前清理旧产物: tsc -b 增量残留的 lib/ 会让 tsdown 报 MISSING_EXPORT
    # (旧版 API import 未随源码更新), 清理后构建 = 全新打包, 产物与源码一致。
    _clean_build_artifacts()
    # 输出实时捕获到日志区 (不再是独立控制台弹窗)
    # CI=true: 管道捕获 (无 TTY) 时 pnpm 才不拒绝移除 modules 目录
    # (ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY); 并显式禁交互确认。
    build_env = _node_env()
    build_env["CI"] = "true"
    rc, _lines = _run_captured(
        _pnpm_list(["--config.confirmModulesPurge=false", "run", "build"]),
        env=build_env, timeout=None, prefix="  ")
    ok = rc == 0
    if _ACTIVE["cancel"]:
        _log_ui_ts("构建已取消。")
    elif ok:
        _log_ui_ts("[OK] 构建完成。")
    else:
        _log_ui_ts(f"[FAILED] 构建失败 (exit code={rc})。")
    _log_ui_ts("=" * 44)
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
                # git 拉取输出同步进日志区 (buffer, 面板创建后可见)
                _log_ui_ts(line.rstrip("\r\n"))
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
                cmd += ["fetch", "--depth", "50", "--progress", "--tags",
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
    """首次安装: 弹可见 cmd 窗口执行 pnpm install (内嵌 pnpm + node)。

    依赖 store 放 data 目录不占 C 盘; 成功窗口自动关闭, 失败 pause 供查看。
    与切换版本 / 构建统一走可见 cmd 流程 (需求: 首次启动 = 环境更新 cmd ->
    构建 cmd, 仅无 git 切换步骤)。取消 (splash 关闭) 时终止子进程。"""
    store = '"' + str(DATA_DIR / "pnpm-store") + '"'
    body = (
        "chcp 65001 >nul & set CI=true & "
        "echo. & echo ============================================ & "
        "echo  正在安装环境依赖 (首次启动, 需要几分钟) ... & "
        "echo ============================================ & "
        + _pnpm_cmd("install --config.confirmModulesPurge=false --store-dir " + store)
        + " & echo. & echo 环境依赖安装完成 & "
        "echo ------------- & exit 0"
    )
    ok = _show_console_step("环境更新 (pnpm install)", body, timeout=1800)
    installed = (SOURCE / "node_modules" / ".modules.yaml").is_file()
    if ok and installed:
        log("pnpm install OK (output captured)")
        return True
    log(f"pnpm install failed (rc ok={ok}, marker={installed})")
    if not ok and not _ACTIVE["cancel"]:
        log("pnpm install finished with error (see log panel)")
    return False


def _backend_log_path() -> Path:
    """后端子进程 stdout/stderr 落盘路径 (每次启动一个文件, 失败可诊断)。

    原实现把后端输出 DEVNULL 丢弃, 启动失败 (如覆盖安装后文件锁/杀软扫描
    导致 node 加载模块失败) 时只能看到 "exited early with code=1", 无法定位。
    现在输出落盘到 data/logs/backend-<时间戳>.log。"""
    log_dir = DATA_DIR / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return log_dir / ("backend-" + time.strftime("%Y%m%d-%H%M%S") + ".log")


_WEB_URL_RE = re.compile(r"dsh web:\s+(https?://\S+)")


def _extract_web_url() -> str | None:
    """当前后端子进程日志里解析 'dsh web: <url>' 行的 URL (带启动 token)。

    新版后端 (browser-auth) 强制浏览器带一次性启动 token 或有效 cookie,
    否则 / 一律 401; 桌面壳必须加载带 token 的 URL 才能换发 cookie 通过认证。
    后端打印的本地 URL 固定是 http://127.0.0.1:<port> (与桌面壳一致), token
    每次启动随机, 所以每次启动都要重新解析。旧版后端打印的是裸 URL
    (无认证后端起直接放行), 解析结果等同回退, 不破坏旧行为。
    行尾可能带 ' (LAN: <url>)' 后缀, 正则取冒号后第一个 http(s) URL。"""
    if _ACTIVE_BACKEND_LOG is None:
        return None
    try:
        text = _ACTIVE_BACKEND_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _WEB_URL_RE.search(text)
    return m.group(1) if m else None


def _wait_web_url(timeout: float = TOKEN_WAIT_SECONDS) -> str | None:
    """等待后端打印带 token 的 Web URL (announceReady 在 Loader settle 后);
    找到返回 URL, 超时返回 None (调用方回退裸 URL)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = _extract_web_url()
        if url is not None:
            return url
        time.sleep(0.3)
    return None


_BACKEND_PORT_IN_USE: bool = False  # 全局: 后端端口当前是否被非 DSH 进程占用
_ACTIVE_BACKEND_LOG: Path | None = None  # 最近一次由本进程启动的后端日志文件 (提取 web 启动 URL 用)


def _port_reuse_check() -> None:
    """探测后端端口: 残留的 DSH 后端 -> 杀掉重启 (纳入新 Job 保强相关);
    被非 DSH 进程占用 -> 复用 (不启动新后端, 期待现有服务就绪)。"""
    global _BACKEND_PORT_IN_USE
    if port_open("127.0.0.1", PORT):
        pid = _find_listener_pid(PORT)
        if pid is not None and _is_our_backend(pid):
            log(f"residual backend pid={pid}, killing and restarting under job")
            kill_tree(pid)
            for _ in range(20):
                if not port_open("127.0.0.1", PORT):
                    break
                time.sleep(0.25)
            _BACKEND_PORT_IN_USE = port_open("127.0.0.1", PORT)
        else:
            log("port occupied by non-DSH process, reusing (no job control)")
            _BACKEND_PORT_IN_USE = True
    else:
        _BACKEND_PORT_IN_USE = False


def start_backend() -> subprocess.Popen | None:
    global _ACTIVE_BACKEND_LOG
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    si = None
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    # 后端 stdout/stderr 落盘 (原 DEVNULL: 失败原因完全不可见)。
    # 二进制追加模式: 子进程按 fd 写入, 不经过父进程缓冲, 实时可见。
    # 顺带记录本次日志文件: 认证模式下启动 token URL 从这里解析。
    bf = None
    log_path = _backend_log_path()
    _ACTIVE_BACKEND_LOG = log_path
    try:
        bf = open(log_path, "ab", buffering=0)
    except OSError:
        pass
    p = subprocess.Popen(
        _start_cmd(), cwd=str(SOURCE), env=_node_env(),
        creationflags=flags, startupinfo=si,
        stdout=bf, stderr=bf,
    )
    if bf is not None:
        # Popen 已把该 fd 交给子进程; 父进程关闭自己的引用即可 (子进程继续持有)
        bf.close()
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
_SINGLE_INSTANCE_NAME = os.environ.get("DSH_SINGLE_INSTANCE",
                                       r"Local\DSH_Desktop_SingleInstance")
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


def _setup_tray(form, scale: float = 1.0) -> None:
    """创建系统托盘图标 (WinForms NotifyIcon, 复用 pythonnet)。
    右键菜单: 显示窗口 / 退出; 双击 = 显示窗口。
    scale = 屏幕 DPI 缩放比 (1.0=100%, 1.75=175%): 菜单字体随之放大,
    否则高分屏/大缩放下菜单字显小。"""
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
        # 托盘菜单字体: 固定 9pt, 与更新对话框文字(9.5pt)同级。
        # 不跟随系统默认菜单字体 (SystemFonts 在 175% 缩放下会放大到
        # ~15.75pt, 用户反馈偏大); pt 是物理单位, 任何分辨率/缩放下的
        # 视觉大小都适中。若需随缩放微调可改用 9.0 * (scale ** 0.3)。
        try:
            from System.Drawing import Font as _TrayFont
            menu.Font = _TrayFont("Microsoft YaHei UI", 9.0)
        except Exception:
            pass
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
        # 升级通知持久化状态: 基准 B(上次拉取的最新 commit) + pending(是否有未查看的新 commit)
        # + 已读基准 seen_hash(用户最近一次打开版本界面时 origin/master commit)
        self._update_base_hash, self._update_pending, self._update_seen_hash = _read_update_state()
        # DSH 控制按钮 (右上角最小化按钮左侧, 圆角胶囊): 0=启动 1=终止 2=重启
        self._ctl_hover = -1           # 当前悬停按钮索引 (-1=无)
        self._ctl_pressed = -1
        self._ctl_rects: list = [None, None, None]   # 各按钮命中区 (RectangleF)
        self._ctl_enabled = [True, False, False]     # 启动/终止/重启 启用态
        self._ctl_tooltip = None       # 悬停提示文字 (None=不画)
        self._ctl_tip = None           # 原生 ToolTip 控件 (悬停气泡, 独立窗口)
        self._ctl_tip_text = ""        # 当前 tooltip 显示文字 (OwnerDraw 自绘用)
        # 右上角设置按钮 (DSH 控制按钮组与最小化按钮之间): 齿轮图标
        self._set_rect = None          # RectangleF 命中区 (None=不画/不命中)
        self._set_hover = False
        self._set_pressed = False
        self._panel = None             # ControlPanel 引用 (install_titlebar 注入)

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
        # DSH 控制按钮尺寸 (圆角胶囊): 每按钮宽 34*s
        self._ctl_btn_w = max(30.0, 34.0 * self._scale)
        # 升级通知文字字体 (9pt, 标准大小; 微软雅黑缺失时回落系统字体)
        try:
            from System.Drawing import Font, FontStyle, SystemFonts
            self._upd_font = Font("Microsoft YaHei UI",
                                  9.0, FontStyle.Regular)
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

        # DSH 控制按钮 tooltip: 用 WinForms 原生 ToolTip (独立顶层窗口,
        # 不会被标题栏自绘区域/控件裁剪, 悬停提示完整可见); OwnerDraw
        # 自绘, 配色跟随当前主题 (深色主题黑底白字, 浅色主题白底深字)。
        try:
            from System.Windows.Forms import ToolTip as _TT
            from System.Drawing import Font as _Font, FontStyle as _FS
            self._ctl_tip = _TT()
            self._ctl_tip.ShowAlways = True
            self._ctl_tip.AutomaticDelay = 200
            self._ctl_tip.ReshowDelay = 100
            self._ctl_tip.OwnerDraw = True
            # 字体调大 -> 提示框随之变大 (窗口尺寸按 ToolTip.Font 计算)
            try:
                self._ctl_tip.Font = _Font("Microsoft YaHei UI", 11.0, _FS.Regular)
            except Exception:
                pass
            self._ctl_tip.Draw += self._on_ctl_tip_draw
            self._ctl_tip_text = ""
        except Exception as ex:
            log(f"ctl tooltip create failed: {ex}")
            self._ctl_tip = None
            self._ctl_tip_text = ""

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

        # 右上角 DSH 电源按钮 (最小化按钮左侧)
        self._draw_control_buttons(g)

    def _draw_control_buttons(self, g) -> None:
        """绘制单个 DSH 电源按钮，点击后在启动/关闭之间切换。"""
        from System.Drawing import (Pen, SolidBrush, RectangleF)
        from System.Drawing.Drawing2D import (SmoothingMode, LineCap)
        c = TITLEBAR_THEMES["dark" if self._dark else "light"]
        s = self._scale
        w = self.form.ClientSize.Width
        btn_w = float(self._ctl_btn_w)
        gap = 6.0 * s
        # 设置按钮与单个电源按钮并排
        set_w = 34.0 * s
        right = w - 3 * self._btn_w - (set_w + 2 * gap)
        y = 5.0 * s
        h = self._tb_h - 10.0 * s
        names = ("DSH电源",)
        g.SmoothingMode = SmoothingMode.AntiAlias
        running = backend_running()
        powered = running or backend_starting()

        # 单个电源按钮，不再绘制三按钮 GroupBox
        x0 = right - btn_w
        self._ctl_rects[0] = RectangleF(x0, y, btn_w, h)
        self._ctl_rects[1] = None
        self._ctl_rects[2] = None
        # 电源按钮始终可点: 启动/终止/构建等任意操作进行中都能点 (可随时终止),
        # 与"取消"按钮一致; 是否真正启停由 _activate_control_button 按状态分流。
        enabled = True
        self._ctl_enabled = [enabled, False, False]
        hover = self._ctl_hover == 0 and enabled
        pressed = self._ctl_pressed == 0 and enabled
        if pressed or hover:
            bg = c["active"] if pressed else c["hover"]
            try:
                g.FillRectangle(SolidBrush(self._rgb(bg)), x0 + 3 * s, y + 3 * s,
                                btn_w - 6 * s, h - 6 * s)
            except Exception:
                pass
        mono = (0, 0, 0) if not self._dark else (255, 255, 255)
        # 运行/启动中 → 红色 (点它可停止); 空闲 → 普通色
        col = (239, 68, 68) if powered else mono
        if hover:
            col = tuple(min(255, int(v * 1.25)) for v in col)
        self._draw_power_icon(g, x0 + btn_w / 2, self._tb_h / 2.0, col, s)
        self._ctl_tooltip = None
        self._draw_settings_button(g, right + gap, y, set_w, h, c)

    def _draw_power_icon(self, g, cx, cy, rgb, s) -> None:
        """绘制电源符号。"""
        from System.Drawing import Pen
        from System.Drawing.Drawing2D import LineCap
        pen = Pen(self._rgb(rgb), max(1.6, 1.9 * s))
        pen.StartCap = LineCap.Round
        pen.EndCap = LineCap.Round
        try:
            r = 6.4 * s
            g.DrawArc(pen, cx - r, cy - r, 2 * r, 2 * r, 315, 270)
            g.DrawLine(pen, cx, cy - 7.6 * s, cx, cy - 0.6 * s)
        finally:
            pen.Dispose()

    def _draw_settings_button(self, g, x, y, bw, h, c) -> None:
        """设置按钮 (齿轮): 位于控制按钮组与最小化按钮之间的小方按钮。
        常显主题色图标, 悬停/按下时加圆角底; 不圈入 GroupBox。"""
        from System.Drawing import (Pen, SolidBrush, RectangleF, PointF)
        from System.Drawing.Drawing2D import SmoothingMode, LineCap, LineJoin
        from System.Drawing.Drawing2D import GraphicsPath
        s = self._scale
        self._set_rect = RectangleF(x, y, bw, h)
        g.SmoothingMode = SmoothingMode.AntiAlias
        # 背景 (内缩的圆角方块, hover/按下时加深)
        bg = None
        if self._set_pressed:
            bg = c["active"]
        elif self._set_hover:
            bg = c["hover"]
        if bg is not None:
            try:
                from System.Drawing.Drawing2D import GraphicsPath
                path = GraphicsPath()
                ins = 3.0 * s
                rr = min(6.0 * s, h / 2.0)
                d = 2 * rr
                bx, by = x + ins, y + ins
                bww, bhh = bw - 2 * ins, h - 2 * ins
                path.AddArc(bx, by, d, d, 180, 90)
                path.AddArc(bx + bww - d, by, d, d, 270, 90)
                path.AddArc(bx + bww - d, by + bhh - d, d, d, 0, 90)
                path.AddArc(bx, by + bhh - d, d, d, 90, 90)
                path.CloseFigure()
                g.FillPath(SolidBrush(self._rgb(bg)), path)
                path.Dispose()
            except Exception:
                g.FillRectangle(SolidBrush(self._rgb(bg)),
                                x + 3 * s, y + 3 * s, bw - 6 * s, h - 6 * s)
        # 齿轮图标: 8 个规则齿牙的连续外轮廓 + 中心孔
        col = c["icon"]
        if self._set_hover or self._set_pressed:
            col = tuple(min(255, int(v * 1.25)) for v in col)
        cx, cy = x + bw / 2.0, y + h / 2.0
        import math as _m
        gear_points = []
        teeth = 6
        outer = 7.8 * s
        root = 6.1 * s
        tooth_half = 0.28 * _m.pi / teeth
        for i in range(teeth):
            center_angle = _m.radians(-90.0 + i * 360.0 / teeth)
            angles = (
                center_angle - 2.0 * tooth_half,
                center_angle - tooth_half,
                center_angle + tooth_half,
                center_angle + 2.0 * tooth_half,
            )
            radii = (root, outer, outer, root)
            for angle, radius in zip(angles, radii):
                gear_points.append(PointF(cx + radius * _m.cos(angle),
                                          cy + radius * _m.sin(angle)))
        gear_brush = SolidBrush(self._rgb(col))
        try:
            g.FillPolygon(gear_brush, gear_points)
        finally:
            gear_brush.Dispose()
        hole_brush = SolidBrush(self._rgb(c["bg"]))
        try:
            g.FillEllipse(hole_brush, cx - 2.4 * s, cy - 2.4 * s,
                          4.8 * s, 4.8 * s)
        finally:
            hole_brush.Dispose()

    def _draw_ctl_icon(self, g, idx: int, cx: float, cy: float, rgb, s,
                       filled: bool = False) -> None:
        """SVG 风格控制图标 (圆头线帽):
        0=启动 ▶ (播放三角, 圆角), 1=终止 ■ (圆角方块), 2=重启 ⟳ (圆环箭头)。
        filled=True 时实心填充 (运行后), False 空心轮廓 (未运行)。"""
        from System.Drawing import Pen, SolidBrush
        from System.Drawing.Drawing2D import LineCap
        pen = Pen(self._rgb(rgb), max(1.6, 1.9 * s))
        pen.StartCap = LineCap.Round
        pen.EndCap = LineCap.Round
        brush = SolidBrush(self._rgb(rgb))
        try:
            if idx == 0:   # 启动: 圆弧包围中央播放三角
                r = 6.6 * s
                g.DrawArc(pen, cx - r, cy - r, 2 * r, 2 * r, 45, 270)
                play = [(cx - 2.7 * s, cy - 3.7 * s),
                        (cx + 3.1 * s, cy),
                        (cx - 2.7 * s, cy + 3.7 * s)]
                g.DrawLines(pen, self._points(play + [play[0]]))
            elif idx == 1:  # 关闭: 顶部开口的电源符号
                r = 6.4 * s
                g.DrawArc(pen, cx - r, cy - r, 2 * r, 2 * r, 315, 270)
                g.DrawLine(pen, cx, cy - 7.6 * s, cx, cy - 0.6 * s)
            else:            # 重启: 顶部开口 + 正上方朝右的实心箭头
                r = 6.2 * s
                restart_pen = Pen(self._rgb(rgb), max(2.0, 2.2 * s))
                restart_pen.StartCap = LineCap.Round
                restart_pen.EndCap = LineCap.Round
                # 圆弧顶部开口，给上方箭头留出完整空间
                g.DrawArc(restart_pen, cx - r, cy - r, 2 * r, 2 * r, 60, 240)
                restart_pen.Dispose()
                arrow_tip = [(cx + 3.8 * s, cy - 6.2 * s),
                             (cx - 1.8 * s, cy - 9.0 * s),
                             (cx - 1.8 * s, cy - 3.4 * s)]
                g.FillPolygon(brush, self._points(arrow_tip))
        finally:
            pen.Dispose()
            brush.Dispose()

    def _points(self, pts):
        from System.Drawing import PointF
        return [PointF(x, y) for x, y in pts]

    def _draw_ctl_tooltip(self, g, idx: int) -> None:
        """按钮上方的小气泡提示 (悬停时显示, 深色浮层 + 白字)。"""
        try:
            from System.Drawing import (SolidBrush, RectangleF, StringFormat, Font)
            from System.Drawing.Drawing2D import SmoothingMode
            from System.Drawing.Text import TextRenderingHint
            s = self._scale
            r = self._ctl_rects[idx]
            if r is None:
                return
            text = ("DSH启动" if idx == 0 else "终止DSH" if idx == 1 else "重启DSH")
            font = self._upd_font
            size = g.MeasureString(text, font)
            bw = size.Width + 16.0 * s
            bh = size.Height + 8.0 * s
            cx = r.X + r.Width / 2.0
            x = cx - bw / 2.0
            y = r.Y - bh - 4.0 * s
            if y < 2.0 * s:           # 标题栏太矮时画在按钮下方
                y = r.Y + r.Height + 4.0 * s
            bg = SolidBrush(self._rgb((30, 30, 33) if not self._dark else (56, 56, 60)))
            fg = SolidBrush(self._rgb((255, 255, 255)))
            g.SmoothingMode = SmoothingMode.AntiAlias
            g.FillRectangle(bg, x, y, bw, bh)
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
            sf = StringFormat()
            from System.Drawing import StringAlignment
            sf.Alignment = StringAlignment.Center
            sf.LineAlignment = StringAlignment.Center
            try:
                g.DrawString(text, font, fg, RectangleF(x, y, bw, bh), sf)
            finally:
                bg.Dispose()
                fg.Dispose()
                sf.Dispose()
        except Exception as ex:
            log(f"ctl tooltip draw failed: {ex}")

    def _on_ctl_tip_draw(self, sender, e) -> None:
        """ToolTip OwnerDraw: 大号圆角矩形气泡, 配色跟随当前主题
        (深色=深底白字, 浅色=浅底深字)。"""
        try:
            from System.Drawing import (Pen, SolidBrush, StringFormat,
                                        StringAlignment, RectangleF)
            from System.Drawing.Drawing2D import (GraphicsPath, SmoothingMode)
            text = getattr(self, "_ctl_tip_text", "") or ""
            if not text:
                return
            dark = self._dark
            if dark:
                bg_rgb = (45, 46, 50)
                fg_rgb = (255, 255, 255)
                edge_rgb = (90, 94, 102)
            else:
                bg_rgb = (250, 250, 250)
                fg_rgb = (31, 41, 55)
                edge_rgb = (190, 195, 205)
            g = e.Graphics
            g.SmoothingMode = SmoothingMode.AntiAlias
            b = e.Bounds
            w_, h_ = b.Width, b.Height
            # 圆角矩形背景 (含 6px 内边距, 圆角 10)
            r = 10
            d = 2 * r
            path = GraphicsPath()
            path.AddArc(0, 0, d, d, 180, 90)
            path.AddArc(w_ - d, 0, d, d, 270, 90)
            path.AddArc(w_ - d, h_ - d, d, d, 0, 90)
            path.AddArc(0, h_ - d, d, d, 90, 90)
            path.CloseFigure()
            br = SolidBrush(self._rgb(bg_rgb))
            g.FillPath(br, path)
            br.Dispose()
            # 边框
            pen = Pen(self._rgb(edge_rgb), 1.2)
            g.DrawPath(pen, path)
            pen.Dispose()
            path.Dispose()
            # 文字 (居中)
            fr = SolidBrush(self._rgb(fg_rgb))
            sf = StringFormat()
            sf.Alignment = StringAlignment.Center
            sf.LineAlignment = StringAlignment.Center
            try:
                g.DrawString(text, e.Font, fr,
                             RectangleF(0, 0, w_, h_), sf)
            finally:
                fr.Dispose()
                sf.Dispose()
        except Exception as ex:
            log(f"ctl tooltip draw failed: {ex}")

    def _update_ctl_tip(self, idx: int) -> None:
        """原生 ToolTip 显示/隐藏 (独立顶层窗口, 不被标题栏裁剪/控件遮挡)。
        idx=-1 或非启用按钮 -> 隐藏; 否则在按钮旁显示功能名。"""
        try:
            tip = self._ctl_tip
            form = self.form
            if tip is None or form is None:
                return
            if idx < 0 or idx >= 3 or not self._ctl_enabled[idx]:
                try:
                    tip.Hide(form)
                except Exception:
                    pass
                self._ctl_tip_text = ""
                return
            r = self._ctl_rects[idx]
            if r is None:
                return
            names = ("DSH启动", "终止DSH", "重启DSH")
            self._ctl_tip_text = names[idx]
            # ToolTip.Show(text, control, x, y): x/y 为控件客户坐标
            px = int(r.X + r.Width / 2.0)
            py = int(r.Y + r.Height + 6)
            tip.Show(names[idx], form, px, py)
        except Exception as ex:
            log(f"ctl tooltip show failed: {ex}")

    def refresh_control_buttons(self) -> None:
        """刷新电源按钮启用状态: 电源按钮始终可点 (任意忙碌中都能点)。"""
        try:
            # 电源按钮始终可点 (同 _draw_control_buttons): 任意忙碌中都能点
            self._ctl_enabled = [
                True, False, False,
            ]
            if self._ctl_hover != -1 or self._ctl_pressed != -1:
                self._ctl_hover = -1
                self._ctl_pressed = -1
            self._invalidate_titlebar()
        except Exception as ex:
            log(f"refresh control buttons failed: {ex}")

    def _hit_control_button(self, x: int, y: int) -> int:
        """x/y (窗口客户坐标) -> 命中控制按钮索引 (0..2) / -1。"""
        if y < 0 or y >= self._tb_h:
            return -1
        for i, r in enumerate(self._ctl_rects):
            if r is not None and r.Contains(x, y):
                return i
        return -1

    def _hit_settings(self, x: int, y: int) -> bool:
        """x/y (窗口客户坐标) -> 是否命中设置按钮。"""
        r = self._set_rect
        return (r is not None and y >= 0 and y < self._tb_h
                and r.Contains(x, y))

    def _activate_control_button(self, idx: int) -> None:
        """触发 DSH 电源按钮动作。"""
        panel = self._panel
        try:
            if idx != 0 or panel is None:
                return
            if backend_running() or backend_starting():
                log("power button: stop DSH")
                panel._on_stop_dsh()
            else:
                log("power button: start DSH")
                panel._on_start_dsh()
        except Exception as ex:
            log(f"control button action failed idx={idx}: {ex}")

    def _draw_update_notice(self, g, info) -> None:
        """绘制常驻"检查更新"按钮 (最小化按钮左侧, 无背景色):
        图标 + "检查更新"文字, 常态灰色; 有未查看的新 commit (pending=1)
        时变蓝色 + 红点 (点开对话框=已查看后恢复灰色, 持久化)。"""
        from System.Drawing import (Pen, SolidBrush, RectangleF, PointF)
        from System.Drawing.Drawing2D import SmoothingMode, LineCap, LineJoin
        from System.Drawing.Text import TextRenderingHint
        c = TITLEBAR_THEMES["dark" if self._dark else "light"]
        font = self._upd_font
        if font is None:
            return
        # 亮色 = pending (未查看的新 commit), 与 info.available 无关
        available = bool(self._update_pending)
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
            # 图标: 圆形旋转箭头 (更新/刷新): 圆环顶部留缺口 + 缺口里的箭头,
            # 尖端指向顺时针方向 (两个箭头围圆的简化版, GDI+ 抗锯齿绘制)
            g.SmoothingMode = SmoothingMode.AntiAlias
            pen = Pen(self._rgb(color), max(1.0, 1.4 * s))
            pen.StartCap = LineCap.Round
            pen.EndCap = LineCap.Round
            ix = x_icon + icon_w / 2.0
            r = 5.0 * s
            try:
                # 圆环: 从 25° 顺时针画 310°, 顶部留 ~50° 缺口 (335°~25°)
                g.DrawArc(pen, ix - r, cy - r, 2 * r, 2 * r, 25, 310)
                # 缺口中央 (正上方) 的箭头, 尖端指向顺时针 (右侧)
                ax = ix + 2.4 * s
                ay = cy - r + 1.4 * s
                g.DrawLine(pen, ax, ay, ix - 2.0 * s, cy - r - 1.4 * s)
                g.DrawLine(pen, ax, ay, ix - 0.8 * s, cy - r + 2.8 * s)
            finally:
                pen.Dispose()
            # 文字 "检查更新"
            x_text = x_icon + icon_w + gap
            y_text = (self._tb_h - size.Height) / 2.0
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
            g.DrawString(text, font, brush, x_text, y_text)
            # 红点: pending=1 (有未查看的新 commit, 持久化, 点开对话框后消失)
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
        """红点是否显示: pending=1 (有未查看的新 commit)。"""
        return bool(self._update_pending)

    def _refresh_panel_update(self) -> None:
        """把控制面板"有更新"提示 (release tag 右侧箭头图标) 同步为当前 pending 状态。

        打开/关闭版本切换画面时 pending 会变化 (0=已查看), 但只改标题栏不够,
        控制面板的箭头 badge 由 panel.set_update 控制, 需一并刷新才消失。"""
        try:
            panel = self._panel
            if panel is not None:
                from System import Action
                has = bool(self._update_pending)
                panel.form.Invoke(Action(lambda: panel.set_update(has)))
        except Exception:
            pass

    def _mark_update_seen(self) -> None:
        """打开"切换版本"对话框瞬间 = 开始查看: 清 pending (主页面蓝字红点恢复灰色),
        持久化到文件; 已读基准 seen_hash 暂不推进 —— 这样列表标红的"新版本"仍按
        打开前的 seen_hash 判定 (用户能在列表里看到哪些是新版本)。"""
        self._update_pending = 0
        _save_update_state(self._update_base_hash, 0, self._update_seen_hash)
        self._invalidate_titlebar()
        self._refresh_panel_update()

    def _commit_update_seen(self) -> None:
        """关闭"切换版本"对话框 = 查看完毕: 把已读基准推进到当前最新 (origin/master),
        清 pending, 持久化。之后只有检测到晚于该基准的新 release tag 才会重新亮灯,
        列表里刚才看过的版本下次也不再标红点。"""
        latest = ""
        info = getattr(self, "_update_info", None)
        if info:
            latest = (info.get("latest") or "").strip()
        if latest and latest != self._update_seen_hash:
            self._update_seen_hash = latest
            _save_update_state(self._update_base_hash, 0, latest)
            log(f"update: seen base advanced to {latest[:7]}")
        else:
            self._update_pending = 0
            _save_update_state(self._update_base_hash, 0, self._update_seen_hash)
        self._invalidate_titlebar()
        self._refresh_panel_update()

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
        # DSH 控制按钮悬停 (右上角): 高亮 + 手型光标 + tooltip
        hc = self._hit_control_button(e.X, e.Y)
        if hc >= 0 and not self._ctl_enabled[hc]:
            hc = -1  # 禁用按钮: 不进入 hover, 不显示高亮/手型/tooltip
        if hc != self._ctl_hover:
            self._ctl_hover = hc
            try:
                from System.Windows.Forms import Cursors
                self.form.Cursor = Cursors.Hand if hc >= 0 else Cursors.Default
            except Exception:
                pass
            self._update_ctl_tip(hc)
            self._invalidate_titlebar()
        # 设置按钮悬停 (控制按钮组右侧, 最小化按钮左侧)
        hset = self._hit_settings(e.X, e.Y)
        if hset != self._set_hover:
            self._set_hover = hset
            try:
                from System.Windows.Forms import Cursors
                self.form.Cursor = Cursors.Hand if (hset or hc >= 0) else Cursors.Default
            except Exception:
                pass
            self._invalidate_titlebar()
        idx = self._hit_button(e.X, e.Y)
        if idx != self._hover:
            self._hover = idx
            self._invalidate_titlebar()

    def _on_mouse_leave(self, sender, e) -> None:
        if (self._hover != -1 or self._pressed != -1
                or self._ctl_hover != -1 or self._ctl_pressed != -1
                or self._set_hover or self._set_pressed):
            self._hover = -1
            self._pressed = -1
            self._ctl_hover = -1
            self._ctl_pressed = -1
            self._set_hover = False
            self._set_pressed = False
            self._ctl_tooltip = None
            try:
                tip = self._ctl_tip
                if tip is not None:
                    tip.Hide(self.form)
            except Exception:
                pass
            self._invalidate_titlebar()

    def _on_mouse_down(self, sender, e) -> None:
        log(f"titlebar mousedown x={e.X} y={e.Y} clicks={e.Clicks}")
        # DSH 控制按钮: 按下进入按下态 (仅启用时响应, 不触发窗口拖动/最大化)
        hc = self._hit_control_button(e.X, e.Y)
        if hc >= 0 and self._ctl_enabled[hc]:
            self._ctl_pressed = hc
            self._invalidate_titlebar()
            return
        # 设置按钮: 按下进入按下态 (不触发窗口拖动/最大化)
        if self._hit_settings(e.X, e.Y):
            self._set_pressed = True
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
        # 设置按钮: 松开时仍在区域内且原按下了 -> 打开设置
        was_set = self._set_pressed
        if was_set:
            self._set_pressed = False
            self._invalidate_titlebar()
            if self._hit_settings(e.X, e.Y):
                self._on_open_settings()
            return
        # DSH 控制按钮: 松开时仍在区域内且原按下了 -> 触发动作
        was_c = self._ctl_pressed
        if was_c >= 0:
            self._ctl_pressed = -1
            self._invalidate_titlebar()
            if self._hit_control_button(e.X, e.Y) == was_c and self._ctl_enabled[was_c]:
                self._activate_control_button(was_c)
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
                # 关闭按钮: 按设置行为 —— "结束进程"=真正退出 (含后端);
                # "隐藏到托盘"=隐藏窗口 (真正退出走托盘"退出"菜单)
                if get_close_behavior() == "exit":
                    _quit_application()
                else:
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

    def _handle_check_result(self, info) -> None:
        """后台自动检测结果处理 (start_update_checker 调用):

        "有新版本" = origin/master 上出现了**从未见过的新 release tag**:
        取 info["commits"] (当前 HEAD 之后的新 release), 再用已读基准 seen_hash
        过滤 (已读基准之后的 release 才算"从未出现过"), 有则 pending=1, 无则 0。
        普通 PR 合并 / 未打 tag 的 release 分支合并不算 (不亮灯)。
        - seen_hash 为空 (从未打开过版本界面): info 里的新 release 全部算新;
          但一旦用户打开过版本界面, seen_hash 推进, 之后这些 release 不再算新,
          只有 origin/master 又出现更新的 release tag 时才重新亮灯。
        - 本地 HEAD 停留旧版本时, info["commits"] 可能含很多历史 release,
          用 seen_hash 过滤后只保留真正没看过的, 避免"明明看过了还亮"。
        - fetch 失败 (info=None): 不动, 保持现状。
        demo 模式: 直接亮 (测试钩子, 不走持久化, 避免污染真实状态)。"""
        if not info:
            return
        if info.get("demo"):
            self._update_pending = 1
            self._invalidate_titlebar()
            self.set_update_info(info)
            return
        latest = (info.get("latest") or "").strip()
        if not latest:
            self.set_update_info(info)
            return
        # 未看过的新 release (相对已读基准 seen_hash)
        unseen = _new_release_commits(info, self._update_seen_hash)
        pending = 1 if unseen else 0
        self._update_pending = pending
        self._update_base_hash = latest
        _save_update_state(latest, pending, self._update_seen_hash)
        log(f"update: {len(unseen)} unseen release(s) (seen={self._update_seen_hash[:7] or 'none'}), "
            + ("badge on" if pending else "no badge"))
        self._invalidate_titlebar()
        self.set_update_info(info)

    def _handle_manual_fetch(self, info) -> None:
        """对话框内手动"获取最新仓库"结果处理 (_fetch_done 调用):

        用户已打开对话框 (=已看列表), 只更新基准 B (fetch 位置), 不置 pending
        (红点蓝字不需要); pending 与已读基准 seen_hash 都保持原值 —— 列表标红的
        "新版本"判定仍基于打开前的 seen_hash, 等对话框关闭 (用户确认查看完) 时
        _commit_update_seen 才把 seen_hash 推进到最新。"""
        if not info:
            return
        latest = (info.get("latest") or "").strip()
        if latest and latest != self._update_base_hash:
            self._update_base_hash = latest
            _save_update_state(latest, self._update_pending, self._update_seen_hash)
            log(f"update: manual fetch, baseline updated to {latest[:7]}")
        self._invalidate_titlebar()
        self.set_update_info(info)

    def set_update_info(self, info) -> None:
        """更新检测结果 (后台线程调用, 内部封送 UI 线程)。

        info=None 表示检测失败 (保持现状)。亮/灰由持久化的 pending 状态
        (_update_pending) 驱动, 与 info 的 available 无关; 灰色同样可点击
        (打开版本列表查看/切换)。"""
        def _apply() -> None:
            self._update_info = info
            self._update_hover = False
            self._update_pressed = False
            self._invalidate_titlebar()
            # 控制面板"有更新"提示 (版本 label 右侧 SVG 图标): 由已读判定 pending 驱动,
            # 与标题栏红点一致 (只有"从未见过的新 release"才亮, 打开版本界面后熄灭)
            try:
                panel = self._panel
                if panel is not None:
                    from System import Action
                    has = bool(self._update_pending)
                    panel.form.Invoke(Action(lambda: panel.set_update(has)))
            except Exception:
                pass
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
            # 总是打开版本列表界面 (git 样式): 不管有没有更新都可选历史版本切换。
            # 点开 = 已查看: 清 pending (蓝字红点恢复灰色, 持久化; 基准 B 不动)
            self._mark_update_seen()
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
            # 对话框已关闭 = 用户查看完毕: 推进已读基准, 列表红点与主页提示随之消失
            try:
                self._commit_update_seen()
            except Exception:
                pass

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

        # 记录启用态配色, 供禁用时切统一灰底 (无 hover)
        def _cap_style(btn):
            try:
                return (btn.BackColor, btn.ForeColor, btn.FlatAppearance.MouseOverBackColor)
            except Exception:
                return None

        _close_style = _cap_style(btn_close)
        _check_style = _cap_style(btn_check)

        def _set_check_btns(enabled: bool) -> None:
            btn_check.Enabled = enabled
            btn_close.Enabled = enabled
            _style_winforms_button(btn_check, enabled, _check_style, dark)
            _style_winforms_button(btn_close, enabled, _close_style, dark)

        def _done(info2) -> None:
            try:
                if form.IsDisposed:
                    return
                if info2 is None:
                    status.Text = "检测失败（网络不可用？），请检查代理设置后重试。"
                    _set_check_btns(True)
                    return
                if info2.get("available"):
                    form.Close()
                    self.set_update_info(info2)
                    # 直接打开升级对话框 (绕过 _update_dialog_open 标志, 已在打开中)
                    show_update_dialog(self)
                    return
                status.Text = f"仍然是最新（{info2.get('head_short', '?')}）。"
                _set_check_btns(True)
            except Exception as ex:
                log(f"recheck done failed: {ex}")

        def _recheck(_s, _e) -> None:
            _set_check_btns(False)
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

    def _on_open_settings(self) -> None:
        """设置对话框: 关闭窗口后的行为 (隐藏到系统托盘 / 结束进程)。"""
        from System.Windows.Forms import (
            Form, Label, Button, RadioButton, FormBorderStyle)
        from System.Drawing import Color, Point, Size, Font
        dark = self._dark
        s = self._scale
        cur = get_close_behavior()

        form = Form()
        form.Text = "设置"
        form.FormBorderStyle = FormBorderStyle(0)  # None (python 关键字冲突, 用枚举构造)
        try:
            from System import Enum as _Enum
            form.StartPosition = _Enum.ToObject(form.StartPosition.GetType(), 4)
        except Exception:
            pass
        form.ShowInTaskbar = False
        form.MaximizeBox = False
        form.MinimizeBox = False
        form.ClientSize = Size(int(430 * s), int(240 * s))
        try:
            form.Font = Font("Microsoft YaHei UI", 9.5)
        except Exception:
            pass
        # 自绘标题栏 (主题色背景 + 图标 + 标题 + 关闭按钮, 可拖动)
        tb = _install_dialog_chrome(form, "设置", dark, s,
                                    lambda: form.Close())
        form.ClientSize = Size(int(430 * s), int(240 * s) + tb)

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
                log(f"settings dialog dwm failed: {ex}")

        form.Shown += _apply_dwm
        if dark:
            form.BackColor = Color.FromArgb(21, 21, 23)
            form.ForeColor = Color.FromArgb(229, 231, 235)

        def theme(ctrl) -> None:
            # 单选选项(隐藏到托盘/结束进程)不设背景色: 背景继承窗体, 避免出现
            # 比窗体略亮的色块; "确定"按钮的蓝色背景在后面单独设置。
            if dark:
                ctrl.ForeColor = Color.FromArgb(229, 231, 235)

        # 分组标题
        group = Label()
        group.SetBounds(int(22 * s), int(20 * s) + tb, int(386 * s), int(28 * s))
        group.AutoSize = False
        group.Text = "关闭窗口后："
        group.ForeColor = Color.FromArgb(148, 163, 184) if dark else Color.Gray
        form.Controls.Add(group)

        # 两个单选项
        radio_hide = RadioButton()
        radio_hide.SetBounds(int(34 * s), int(56 * s) + tb, int(380 * s), int(32 * s))
        radio_hide.Text = "隐藏到系统托盘（后端继续运行，从托盘\"退出\"才结束进程）"
        radio_hide.Checked = (cur == "tray")
        theme(radio_hide)
        form.Controls.Add(radio_hide)

        radio_exit = RadioButton()
        radio_exit.SetBounds(int(34 * s), int(96 * s) + tb, int(380 * s), int(32 * s))
        radio_exit.Text = "关闭窗口即结束进程（后端一并退出）"
        radio_exit.Checked = (cur == "exit")
        theme(radio_exit)
        form.Controls.Add(radio_exit)

        tip = Label()
        tip.SetBounds(int(22 * s), int(140 * s) + tb, int(386 * s), int(36 * s))
        tip.AutoSize = False
        tip.Text = "选择\"隐藏到系统托盘\"时，关闭按钮将最小化到托盘且不退出应用。"
        tip.ForeColor = Color.FromArgb(148, 163, 184) if dark else Color.Gray
        form.Controls.Add(tip)

        btn_ok = Button()
        btn_ok.SetBounds(int(430 * s - 22 * s - 92 * s), int(186 * s) + tb, int(92 * s), int(32 * s))
        btn_ok.Text = "确定"
        theme(btn_ok)
        form.Controls.Add(btn_ok)
        if dark:
            try:
                btn_ok.BackColor = Color.FromArgb(37, 99, 235)
                btn_ok.ForeColor = Color.White
            except Exception:
                pass

        def _save(_s, _e) -> None:
            new_v = "exit" if radio_exit.Checked else "tray"
            if new_v != get_close_behavior():
                set_close_behavior(new_v)
                log(f"close behavior changed to {new_v}")
            form.Close()

        btn_ok.Click += _save
        form.ShowDialog(self.form)
        try:
            form.Dispose()
        except Exception:
            pass

    def start_update_checker(self) -> None:
        """后台线程: 定期检测官方仓库 (origin/master) 更新。

        每次启动立即后台获取代码 (首个检测延迟 DSH_UPDATE_FIRST_DELAY 秒,
        默认 3, 仅等窗口先显示, 不阻塞启动); 之后每 DSH_UPDATE_INTERVAL 秒
        一次 (默认 1800 = 30 分钟)。fetch 静默更新 refs/remotes/origin/master,
        不动工作区 (不影响当前版本); 检测到 origin/master 领先本地时有更新,
        标题栏"检查更新"变蓝 + 红点。检测失败 (断网等) 不改变现有提示状态。"""
        if self._upd_thread_started:
            return
        self._upd_thread_started = True
        first_delay = float(os.environ.get("DSH_UPDATE_FIRST_DELAY", "3"))
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
                        self._handle_check_result(info)
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


# ==================== 控制面板 (原生主页) ====================
# 窗体打开时显示控制面板 (不自动构建 / 不自动启动后端与 webview):
#   Grid 布局 (嵌套 TableLayoutPanel 网格, 见 install 的"Grid 布局"段):
#   主网格 = 左列 | 间隙 | 右列, 底部一行沉底功能按钮;
#   左侧列: 当前版本卡片 (release tag 大字 + commit + 更新 SVG 提示图标)、
#           版本切换(检查更新)、运行环境检测、前后端构建 + 构建物清除;
#   底部行: DSH 启动 (蓝底白字) + 打开日志路径 + 清除日志 (同一行沉底);
#   右侧:   日志输出区 (所有 cmd 输出实时透传: git 拉取/切换、环境更新、
#           前后端构建、后端启动), 底边与"DSH 启动"行下沿齐平。
# 点 DSH 启动 -> 后端就绪后 webview 直接覆盖内容区; 点右上角终止 ->
# 停后端 + webview 关闭并显露控制面板。全部为原生 WinForms 控件 +
# GDI+ 矢量自绘 (更新提示图标按"圆形+中空向上箭头+红点"绘制)。
#
# 与既有代码的关系:
#   - 版本切换按钮 -> show_update_dialog(titlebar) (升级对话框不变)
#   - 构建/清除/环境检测/启动 的后台线程动作复用 run_build /
#     _clean_build_artifacts / start_backend / _stop_backend 等;
#   - 日志 sink 通过 _set_log_sink 注册, _log_ui_ts 追加带时间戳行。


def _round_region(ctrl, radius: int):
    """圆角矩形 Region (与升级对话框同一手法)。"""
    from System.Drawing import Region
    from System.Drawing.Drawing2D import GraphicsPath
    w, h = ctrl.Width, ctrl.Height
    path = GraphicsPath()
    d = 2 * radius
    path.AddArc(0, 0, d, d, 180, 90)
    path.AddArc(w - d, 0, d, d, 270, 90)
    path.AddArc(w - d, h - d, d, d, 0, 90)
    path.AddArc(0, h - d, d, d, 90, 90)
    path.CloseFigure()
    return Region(path)


# 后端运行状态 (供标题栏/控制面板按钮 enable 判定)
_BACKEND_RUNNING = {"flag": False}
# 启动中状态: 点"DSH 启动"后、后端完全就绪前为 True, 让终止/重启立即可用
# (用户要求: 点击启动后关闭/重启立刻可点, 以便随时终止)
_BACKEND_STARTING = {"flag": False}


def backend_running() -> bool:
    return bool(_BACKEND_RUNNING["flag"])


def backend_starting() -> bool:
    return bool(_BACKEND_STARTING["flag"])


def _set_backend_running(v: bool) -> None:
    _BACKEND_RUNNING["flag"] = bool(v)
    if v:
        _BACKEND_STARTING["flag"] = False   # 就绪后清除启动中状态
    _notify_panel_busy()  # 状态变化时刷新按钮


def _set_backend_starting(v: bool) -> None:
    _BACKEND_STARTING["flag"] = bool(v)
    _notify_panel_busy()


class ControlPanel:
    """主窗体内容区控制面板 (WinForms 控件 + GDI 自绘, 无 webview 渲染)。

    控件直接 Add 到主 form, z-order 高于 WebView2 控件: 控制面板可见时
    盖住 webview; hide() 后 webview 露出 (启动 DSH 覆盖内容区); show()
    后重新显露 (终止 DSH)。
    """

    def __init__(self, window, titlebar) -> None:
        self.window = window
        self.titlebar = titlebar
        self.form = titlebar.form
        self._scale = max(1.0, float(getattr(titlebar, "_scale", 1.0)))
        self._dark = bool(getattr(titlebar, "_dark", True))
        self._version = _current_version_info()
        self._update_available = False
        self._busy = False
        self._ctrls: list = []
        self._log_text = None
        self._lv_version = None
        self._lv_commit = None
        self._badge = None          # 更新 SVG 图标 (PictureBox 自绘)
        self._btn_version = None
        self._btn_env = None
        self._btn_build = None
        self._btn_clean = None
        self._btn_start = None
        self._btn_openlog = None
        self._btn_clearlog = None
        self._btn_cancel = None
        self._installed = False
        # 各按钮的启用态配色 (bg, fg, hover), 按按钮引用记录, 切换启用/禁用时还原
        self._btn_style: dict = {}

    # ---------- 主题/颜色 ----------

    def _color(self, key: str):
        from System.Drawing import Color
        dark = self._dark
        if dark:
            pal = {
                "bg": (21, 21, 23), "card": (30, 30, 33),
                "card_border": (47, 47, 49), "fg": (229, 231, 235),
                "sub": (148, 163, 184), "hover": (47, 47, 49),
                "active": (64, 64, 66), "logbg": (15, 15, 17),
                "blue": (37, 99, 235), "blue_hover": (29, 78, 216),
                "upd": (96, 165, 250),
                "red": (220, 38, 38), "red_hover": (239, 68, 68),
                "red_d": (76, 46, 46), "red_d_fg": (198, 156, 156),
                # 禁用态: 统一灰底 + 灰字 (所有按钮不可用时可辨)
                "disabled": (56, 56, 60), "disabled_fg": (120, 124, 130),
            }
        else:
            pal = {
                "bg": (249, 250, 251), "card": (255, 255, 255),
                "card_border": (226, 230, 236), "fg": (31, 41, 55),
                "sub": (107, 114, 128), "hover": (232, 232, 234),
                "active": (219, 219, 222), "logbg": (255, 255, 255),
                "blue": (37, 99, 235), "blue_hover": (29, 78, 216),
                "upd": (37, 99, 235),
                "red": (220, 38, 38), "red_hover": (239, 68, 68),
                "red_d": (243, 210, 210), "red_d_fg": (161, 116, 116),
                "disabled": (226, 230, 236), "disabled_fg": (148, 163, 184),
            }
        rgb = pal[key]
        return Color.FromArgb(*rgb)

    # ---------- 安装 (UI 线程) ----------

    def install(self) -> None:
        from System.Windows.Forms import (Button, Label, RichTextBox,
                                          PictureBox, FlatStyle, Cursors,
                                          TableLayoutPanel, RowStyle, ColumnStyle,
                                          SizeType, AnchorStyles, DockStyle,
                                          Padding, ControlStyles)
        from System.Drawing import (Font, FontStyle, Size as _Size,
                                    ContentAlignment)
        s = self._scale
        form = self.form
        self._ctrls = []

        def mk_button(text, primary=False, height=None, font_size=7.75,
                      back=None, fore=None, hover=None):
            b = Button()
            b.Text = text
            b.FlatStyle = FlatStyle.Flat
            b.FlatAppearance.BorderSize = 0
            # 关键: Selectable=False 让按钮鼠标点击也无法获焦 (TabStop 只管 Tab 键,
            # 拦不住鼠标点击获焦)。获焦的 Flat 按钮会画系统焦点白框, 且焦点只落在
            # 被点的那个按钮上; 设为不可 Selectable 即不画焦点框。
            b.SetStyle(ControlStyles.Selectable, False)
            if back is not None:
                b.BackColor = back
            else:
                b.BackColor = self._color("blue") if primary else self._color("card")
            if fore is not None:
                b.ForeColor = fore
            else:
                b.ForeColor = Color_White() if primary else self._color("fg")
            if primary:
                b.FlatAppearance.MouseOverBackColor = self._color("blue_hover")
            elif hover is not None:
                b.FlatAppearance.MouseOverBackColor = hover
            else:
                b.FlatAppearance.MouseOverBackColor = self._color("hover")
            try:
                b.Font = Font("Microsoft YaHei UI", font_size * max(1.0, s * 0.95))
            except Exception:
                pass
            try:
                b.Cursor = Cursors.Hand
            except Exception:
                pass
            # 记录启用态配色 (背景/文字/hover), 供 refresh_buttons 切换禁用灰底
            try:
                self._btn_style[b] = (
                    b.BackColor, b.ForeColor, b.FlatAppearance.MouseOverBackColor)
            except Exception:
                pass
            self._ctrls.append(b)
            return b

        # 版本卡片: "当前版本:" 小字 + 大 label (release tag 大字)
        #        + 小 label (commit 小字)
        lvc = Label()
        lvc.AutoSize = False
        try:
            from System.Drawing import Color as _ColorT
            lvc.BackColor = _ColorT.Transparent   # 不画背景色块
        except Exception:
            lvc.BackColor = self._color("card")
        lvc.ForeColor = self._color("sub")
        lvc.Text = "当前版本："
        lvc.TextAlign = ContentAlignment.MiddleLeft   # 垂直居中, 防止行高内文本截断
        try:
            lvc.Font = Font("Microsoft YaHei UI", 7.0 * max(1.0, s * 0.95))
        except Exception:
            pass
        self._ctrls.append(lvc)
        self._lv_caption = lvc

        lv = Label()
        lv.AutoSize = False
        try:
            from System.Drawing import Color as _ColorT
            lv.BackColor = _ColorT.Transparent   # 不画背景色块
        except Exception:
            lv.BackColor = self._color("card")
        lv.ForeColor = self._color("fg")
        try:
            lv.Font = Font("Microsoft YaHei UI", 15.0 * max(1.0, s * 0.9),
                           FontStyle.Bold)
        except Exception:
            pass
        self._ctrls.append(lv)
        self._lv_version = lv

        lv2 = Label()
        lv2.AutoSize = False
        try:
            from System.Drawing import Color as _ColorT
            lv2.BackColor = _ColorT.Transparent   # 不画背景色块
        except Exception:
            lv2.BackColor = self._color("card")
        lv2.ForeColor = self._color("sub")
        try:
            lv2.Font = Font("Microsoft YaHei UI", 7.5 * max(1.0, s * 0.95))
        except Exception:
            pass
        self._ctrls.append(lv2)
        self._lv_commit = lv2

        # 更新 SVG 提示图标 (圆形 + 中空向上箭头 + 红点; 自绘, 不用 Panel)
        badge = PictureBox()
        try:
            from System.Drawing import Color as _ColorT
            badge.BackColor = _ColorT.Transparent   # 不画背景色块
        except Exception:
            badge.BackColor = self._color("card")
        badge.Visible = False
        badge.Paint += self._paint_update_badge
        self._ctrls.append(badge)
        self._badge = badge

        self._render_version()

        # 按钮
        self._btn_version = mk_button("版本切换 (检查更新)")
        self._btn_env = mk_button("运行环境检测")
        self._btn_build = mk_button("前后端构建")
        self._btn_clean = mk_button("构建物清除")
        self._btn_start = mk_button("DSH 启动", primary=True, height=46)

        # 取消按钮: 位于"构建物清除"按钮下方, 仅忙碌 (环境检测/构建/清除
        # 进行中) 时可用; 正常状态红底白字, 空闲时灰置 (禁用, 一眼可辨)
        self._btn_cancel = mk_button("取消", font_size=7.5,
                                     back=self._color("red_d"),
                                     fore=self._color("red_d_fg"),
                                     hover=self._color("red_d"))
        self._apply_cancel_style(False)   # 初始空闲: 灰置
        self._btn_openlog = mk_button("打开日志路径", font_size=7.5)
        self._btn_clearlog = mk_button("清除日志", font_size=7.5)

        # 日志区 (RichTextBox, 只读, 等宽字体, 可滚动/选择); 紧贴右列顶部
        txt = RichTextBox()
        txt.ReadOnly = True
        try:
            from System.Windows.Forms import BorderStyle as _BS
            txt.BorderStyle = _BS(0)   # None (python 关键字冲突, 用枚举构造)
        except Exception:
            pass
        txt.BackColor = self._color("logbg")
        txt.ForeColor = self._color("fg")
        try:
            txt.Font = Font("Consolas", 9.5 * s)
        except Exception:
            pass
        txt.WordWrap = False
        try:
            from System.Windows.Forms import RichTextBoxScrollBars as _RBS
            txt.ScrollBars = _RBS(3)   # Both (0=None 1=Horizontal 2=Vertical 3=Both)
        except Exception:
            pass
        txt.HideSelection = False
        txt.DetectUrls = False
        self._ctrls.append(txt)
        self._log_text = txt

        # ---------- Grid 布局 (TableLayoutPanel 嵌套网格) ----------
        # 原手动 SetBounds 绝对定位废弃 (且曾因 tag_top 未定义导致 layout 失效):
        # 全部控件放入网格 cell, 由表格自动排列; 窗体缩放时顶层网格
        # Anchor 四边自动伸缩, 内部 Dock=Fill 逐级跟随, 无需逐控件算坐标。
        def _mk_bar(cols, rows):
            """创建满格 TableLayoutPanel: cols=[(宽, SizeType)…], rows=[(高, SizeType)…]。"""
            t = TableLayoutPanel()
            t.ColumnCount = len(cols)
            t.RowCount = len(rows)
            for w_, ty in cols:
                t.ColumnStyles.Add(ColumnStyle(ty, w_))
            for h_, ty in rows:
                t.RowStyles.Add(RowStyle(ty, h_))
            t.Dock = DockStyle.Fill
            t.BackColor = self._color("bg")
            return t

        def _add(panel, ctrl, col, row, margin=None):
            """把控件放入 cell: 默认 Dock=Fill 填满 cell (可用 Margin 留出间隙)。"""
            ctrl.Dock = DockStyle.Fill
            if margin is not None:
                ctrl.Margin = margin
            panel.Controls.Add(ctrl, col, row)
            return ctrl

        s = self._scale
        LW = 300.0 * s             # 左列宽 (与旧版一致)
        gap_col = 22.0 * s         # 左列/右列间隙
        bh = 46.0 * s              # 底部行按钮高 (DSH 启动/打开日志/清除日志)
        m12 = Padding(0, int(12 * s), 0, 0)   # 与上一行拉开 12*s (按钮列/底部行)

        # 主网格: [左列 | 间隙 | 右列] x [主区(弹性) | 底部行(固定+上间隙)]
        main = _mk_bar([(LW, SizeType.Absolute), (gap_col, SizeType.Absolute),
                        (100, SizeType.Percent)],
                       [(100, SizeType.Percent), (bh + 12 * s, SizeType.Absolute)])
        main.Anchor = (AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
                       | AnchorStyles.Bottom)
        self._main_panel = main

        # 左列: 版本卡片(标题/tag+badge/commit) + 功能按钮 + 取消 + 弹性空白沉底
        left = _mk_bar([(100, SizeType.Percent)],
                       [(26 * s, SizeType.Absolute),          # "当前版本:" (行高留足, 防文本截断)
                        (42 * s, SizeType.Absolute),          # tag 行 (紧贴上方 caption)
                        (28 * s, SizeType.Absolute),          # commit 小字
                        (42 * s + 12 * s, SizeType.Absolute), # 版本切换
                        (42 * s + 12 * s, SizeType.Absolute), # 运行环境检测
                        (54 * s, SizeType.Absolute),          # 构建/清除 并排
                        (42 * s + 12 * s, SizeType.Absolute), # 取消 (与版本切换等单按钮等高)
                        (100, SizeType.Percent)])             # 弹性占位 (沉底)
        # tag 行: [tag 大字 | 更新 badge]
        tag_bar = _mk_bar([(100, SizeType.Percent), (44 * s, SizeType.Absolute)],
                          [(100, SizeType.Percent)])
        tag_bar.Margin = Padding(0)          # 紧贴 "当前版本:" 下方, 不再下移
        _add(tag_bar, self._lv_version, 0, 0)
        badge = self._badge
        badge.Dock = DockStyle(0)   # None (python 关键字冲突, 用枚举构造)
        badge.Anchor = AnchorStyles.Top | AnchorStyles.Right
        badge.Margin = Padding(0, int(4 * s), int(14 * s), 0)
        badge.Size = _Size(int(30 * s), int(30 * s))
        tag_bar.Controls.Add(badge, 1, 0)   # Dock=None: 固定尺寸, 靠 cell 右上
        # 构建/清除 并排行 (两列各半宽, 中间留 12*s 间隙)
        build_bar = _mk_bar([(50, SizeType.Percent), (50, SizeType.Percent)],
                            [(100, SizeType.Percent)])
        build_bar.Margin = m12
        self._btn_build.Margin = Padding(0)                    # 与 clean 等高
        self._btn_clean.Margin = Padding(int(12 * s), 0, 0, 0)
        _add(build_bar, self._btn_build, 0, 0)
        _add(build_bar, self._btn_clean, 1, 0)

        left.Controls.Add(tag_bar, 0, 1)
        left.Controls.Add(build_bar, 0, 5)
        _add(left, self._lv_caption, 0, 0)
        _add(left, self._lv_commit, 0, 2)
        _add(left, self._btn_version, 0, 3, margin=m12)
        _add(left, self._btn_env, 0, 4, margin=m12)
        _add(left, self._btn_cancel, 0, 6, margin=m12)   # 构建/清除 下方

        # 右列: 日志框 (无标题 label, 紧贴右列顶部; 弹性填满)
        right = _mk_bar([(100, SizeType.Percent)],
                        [(100, SizeType.Percent)])
        _add(right, self._log_text, 0, 0)

        # 底部行: DSH 启动 (左列) | 打开日志/清除日志 (右列, 靠右并排);
        # 取消按钮已移至左列"构建物清除"下方
        bottom = _mk_bar([(50, SizeType.Percent), (50, SizeType.Percent)],
                         [(100, SizeType.Percent)])
        bottom.Margin = m12
        self._btn_clearlog.Margin = Padding(0, 0, int(10 * s), 0)
        self._btn_openlog.Margin = Padding(0, 0, int(10 * s), 0)
        _add(bottom, self._btn_clearlog, 0, 0)
        _add(bottom, self._btn_openlog, 1, 0)

        main.Controls.Add(left, 0, 0)
        main.Controls.Add(right, 2, 0)
        _add(main, self._btn_start, 0, 1, margin=m12)
        main.Controls.Add(bottom, 2, 1)
        self._ctrls.append(main)      # 顶层容器纳入显隐管理 (子控件随之隐藏)

        # 加入窗体 (Add 顺序在 webview 之后 -> z-order 高于 webview);
        # webview 沉底, 控制面板常驻上层 (启动后 hide() 露出 webview)
        try:
            wv = getattr(self.titlebar, "_webview_ctrl", None)
            if wv is not None:
                wv.SendToBack()
        except Exception as ex:
            log(f"webview sendtoback failed: {ex}")
        form.Controls.Add(main)
        self._installed = True

        # 事件
        self._btn_version.Click += lambda s, e: self._on_version_switch()
        self._btn_env.Click += lambda s, e: self._on_env_check()
        self._btn_build.Click += lambda s, e: self._on_build()
        self._btn_clean.Click += lambda s, e: self._on_clean()
        self._btn_start.Click += lambda s, e: self._on_start_dsh()
        self._btn_openlog.Click += lambda s, e: self._on_open_log()
        self._btn_clearlog.Click += lambda s, e: self._on_clear_log()
        self._btn_cancel.Click += lambda s, e: self._on_cancel()

        # 注册日志 sink: 后续所有 _log_ui_ts 输出进入日志区
        _set_log_sink(self.append_log)
        # 回填 sink 注册前的输出 (首次 clone/install 阶段, buffer 已积累)
        try:
            for line in _log_buffer_snapshot():
                self._append_log_ui_bulk(line)
        except Exception as ex:
            log(f"log buffer backfill failed: {ex}")
        # 供全局忙碌通知 (_notify_panel_busy) 查找控制面板实例
        sys._dsh_control_panel = self

        # 窗体缩放时重排控制面板 (日志区/按钮锚定)
        try:
            form.Resize += lambda s, e: self.layout()
        except Exception as ex:
            log(f"control panel resize hook failed: {ex}")

        self.layout()
        log("control panel installed")

    def _render_version(self) -> None:
        lv = self._lv_version
        lv2 = self._lv_commit
        if lv is None:
            return
        info = self._version
        tag = info.get("tag") or "（无 release tag）"
        commit = info.get("commit") or "?"
        short = info.get("short") or (commit[:7] if commit else "?")
        lv.Text = tag
        if lv2 is not None:
            lv2.Text = f"commit: {short}"

    # ---------- 更新 SVG 提示图标 (圆形 + 中空向上箭头 + 红点) ----------

    def set_update(self, has_update: bool) -> None:
        self._update_available = bool(has_update)
        if self._badge is not None:
            self._badge.Visible = bool(has_update)
            try:
                self._badge.Invalidate()
            except Exception:
                pass
        self.refresh_buttons()

    def _paint_update_badge(self, sender, e) -> None:
        """完整蓝色圆环 + 中央实心向上箭头 (三角尖头 + 矩形杆) + 底部短横线
        (升级图标, 参考 "circular upgrade" 风格, 透明底单色蓝)。

        不填充背景、无红点: 圆环细描边, 中央箭头实心填充 + 同色描边,
        由 _update_available 控制显隐 (set_update)。"""
        try:
            from System.Drawing import (Pen, SolidBrush, PointF, RectangleF)
            from System.Drawing.Drawing2D import (SmoothingMode, GraphicsPath,
                                                  LineCap, LineJoin)
            g = e.Graphics
            s = self._scale
            w, h = float(sender.Width), float(sender.Height)
            g.SmoothingMode = SmoothingMode.AntiAlias
            blue = self._color("upd")
            cx = w / 2.0
            cy = h / 2.0
            # 外圈: 完整圆环 (细描边), 无缺口无小箭头
            pen = Pen(blue, max(1.0, 1.2 * s))
            g.DrawEllipse(pen, 2.0 * s, 2.0 * s, w - 4.0 * s, h - 4.0 * s)
            pen.Dispose()
            R = min(w, h) / 2.0 - 2.0 * s
            # 中央上箭头: 使用连续线段绘制空心轮廓，避免填充和接缝
            head_h = 0.48 * R
            head_w_half = 0.48 * R
            stick_w_half = 0.24 * R
            head_bottom = cy - 0.12 * R
            tip_y = head_bottom - head_h
            base_y = cy + 0.30 * R
            arrow_pen = Pen(blue, max(1.0, 1.2 * s))
            arrow_pen.StartCap = LineCap.Round
            arrow_pen.EndCap = LineCap.Round
            arrow_pen.LineJoin = LineJoin.Round
            arrow_points = [
                PointF(cx, tip_y),
                PointF(cx - head_w_half, head_bottom),
                PointF(cx - stick_w_half, head_bottom),
                PointF(cx - stick_w_half, base_y),
                PointF(cx + stick_w_half, base_y),
                PointF(cx + stick_w_half, head_bottom),
                PointF(cx + head_w_half, head_bottom),
                PointF(cx, tip_y),
            ]
            for start, end in zip(arrow_points, arrow_points[1:]):
                g.DrawLine(arrow_pen, start, end)
            arrow_pen.Dispose()
            # 底部短横线
            lw = 0.24 * R
            line_y = base_y + 0.24 * R
            lp = Pen(blue, max(1.0, 1.2 * s))
            lp.StartCap = LineCap.Round
            lp.EndCap = LineCap.Round
            g.DrawLine(lp, cx - lw, line_y, cx + lw, line_y)
            lp.Dispose()
        except Exception as ex:
            log(f"update badge paint failed: {ex}")

    # ---------- 布局 ----------

    def layout(self) -> None:
        """Grid 布局 (TableLayoutPanel): 只定位顶层主网格, 内部控件
        由嵌套表格自动排列; 主网格 Anchor 四边, 窗体缩放自动伸缩。"""
        if not self._installed:
            return
        try:
            s = self._scale
            form = self.form
            w = form.ClientSize.Width
            h = form.ClientSize.Height
            tb_h = int(getattr(self.titlebar, "_tb_h", 36))
            pad = int(18 * s)
            top = tb_h + int(8 * s)            # 内容区顶: 标题栏下方
            bottom_gap = int(16 * s)           # 内容区底: 距窗体下缘
            self._main_panel.SetBounds(pad, top,
                                       max(200, w - 2 * pad),
                                       max(120, h - top - bottom_gap))
            # 圆角 (容器表格本身保留直角, 只圆角具体控件; badge 图标是圆形
            # 自绘, 不加圆角 Region, 否则方形圆角会切掉右上角红点)
            radius = int(8 * s)
            for c in self._ctrls:
                try:
                    if type(c).__name__ == "TableLayoutPanel":
                        continue
                    if c is self._badge:
                        continue
                    r = _round_region(c, radius)
                    old = getattr(c, "Region", None)
                    c.Region = r
                    if old is not None:
                        try:
                            old.Dispose()
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as ex:
            log(f"control panel layout failed: {ex}")

    # ---------- 显隐 (webview 覆盖 / 显露) ----------

    def _toggle_webview(self, visible: bool) -> None:
        """WebView2 是原生 HWND (airspace), 普通控件盖不住它:
        控制面板显示时必须把 webview 隐藏, 否则空白页会盖住面板内容。"""
        try:
            wv = getattr(self.titlebar, "_webview_ctrl", None)
            if wv is not None:
                wv.Visible = bool(visible)
        except Exception as ex:
            log(f"toggle webview visible={visible} failed: {ex}")

    def show(self) -> None:
        for c in self._ctrls:
            try:
                # 升级徽章的可见性由 set_update 结果 (_update_available) 决定,
                # 不能被 show() 无条件点亮 (否则即使检测到无更新, 每次回到
                # 控制面板徽章都会复活, 与 last-update-seen.txt 的 pending 不一致)。
                if c is self._badge:
                    c.Visible = bool(self._update_available)
                else:
                    c.Visible = True
            except Exception:
                pass
        self._toggle_webview(False)   # 面板露出前先藏 webview

    def hide(self) -> None:
        for c in self._ctrls:
            try:
                c.Visible = False
            except Exception:
                pass
        self._toggle_webview(True)    # 进入 webview 前恢复可见

    # ---------- 日志 ----------

    def append_log(self, text: str) -> None:
        """日志 sink 回调 (任意线程): 追加到日志区并自动滚动到底。"""
        try:
            from System import Action
        except Exception:
            return
        try:
            def _do() -> None:
                txt = self._log_text
                if txt is None:
                    return
                try:
                    txt.AppendText(str(text) + "\r\n")
                    txt.SelectionStart = txt.TextLength
                    txt.ScrollToCaret()
                except Exception:
                    pass
            if self.form.InvokeRequired:
                self.form.Invoke(Action(_do))
            else:
                _do()
        except Exception:
            pass

    def _append_log_ui_bulk(self, text: str) -> None:
        """UI 线程批量回填 (install 时调用, 不逐行滚动)。"""
        txt = self._log_text
        if txt is None:
            return
        try:
            txt.AppendText(str(text) + "\r\n")
        except Exception:
            pass

    def clear_log(self) -> None:
        """只清空框内日志, 不动磁盘文件。"""
        try:
            from System import Action
            def _do() -> None:
                txt = self._log_text
                if txt is not None:
                    try:
                        txt.Clear()
                    except Exception:
                        pass
            if self.form.InvokeRequired:
                self.form.Invoke(Action(_do))
            else:
                _do()
        except Exception:
            pass

    # ---------- 忙碌 / 按钮启用 ----------

    def is_busy(self) -> bool:
        return self._busy or bool(_panel_busy["flag"])

    def refresh_buttons(self) -> None:
        try:
            busy = self.is_busy()
            running = backend_running()
            ctrls = [self._btn_version, self._btn_env, self._btn_build,
                     self._btn_clean]
            for b in ctrls:
                if b is not None:
                    b.Enabled = not busy
                    self._apply_button_visual(b, not busy)
            if self._btn_start is not None:
                en = not busy and not running
                self._btn_start.Enabled = en
                self._apply_button_visual(self._btn_start, en)
            # 打开日志/清除日志始终可用
            if self._btn_openlog is not None:
                self._btn_openlog.Enabled = True
                self._apply_button_visual(self._btn_openlog, True)
            if self._btn_clearlog is not None:
                self._btn_clearlog.Enabled = True
                self._apply_button_visual(self._btn_clearlog, True)
            # 取消按钮: 仅忙碌 (命令进行中) 时可点, 空闲灰置; 颜色随启用态切换
            # (可用红底白字 / 禁用统一灰底灰字, 一眼可辨)
            if self._btn_cancel is not None:
                self._btn_cancel.Enabled = busy
                self._apply_cancel_style(busy)
            # 标题栏三控制按钮同步刷新
            tb = self.titlebar
            if tb is not None and hasattr(tb, "refresh_control_buttons"):
                tb.refresh_control_buttons()
        except Exception as ex:
            log(f"refresh buttons failed: {ex}")

    def _apply_button_visual(self, b, enabled: bool) -> None:
        """按启用态切换按钮配色: 启用 = 本色; 禁用 = 统一灰底灰字且无 hover。

        WinForms 对 Enable=False 的默认渲染不可控 (可能保留彩底、或把白字
        翻成黑字), 这里显式控制: 禁用时背景/前景/hover 都换成统一的禁用灰,
        让"不可用"一眼可辨且不随主题/原色漂移。"""
        if b is None:
            return
        try:
            style = self._btn_style.get(b)
            if style is None:
                return
            bg, fg, hover = style
            b.UseVisualStyleBackColor = False
            if enabled:
                b.BackColor = bg
                b.ForeColor = fg
                b.FlatAppearance.MouseOverBackColor = hover
            else:
                b.BackColor = self._color("disabled")
                b.ForeColor = self._color("disabled_fg")
                b.FlatAppearance.MouseOverBackColor = self._color("disabled")
        except Exception as ex:
            log(f"apply button visual failed: {ex}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        if busy:
            _ACTIVE["cancel"] = False   # 新命令开始: 复位取消标志 (上次取消不残留)
        _set_panel_busy(busy)
        self.refresh_buttons()

    # ---------- 取消按钮样式 (红底白字 / 禁用灰置) ----------

    def _apply_cancel_style(self, enabled: bool) -> None:
        """取消按钮配色: 可用 = 红底白字 (悬停亮红), 一眼醒目;
        禁用 = 灰底灰字 (与普通卡色按钮区分但又明显不可点)。"""
        b = self._btn_cancel
        if b is None:
            return
        try:
            b.UseVisualStyleBackColor = False
            if enabled:
                b.BackColor = self._color("red")
                b.ForeColor = Color_White()
                b.FlatAppearance.MouseOverBackColor = self._color("red_hover")
            else:
                # 禁用 = 与其他按钮一致的统一灰底灰字 (无 hover)
                b.BackColor = self._color("disabled")
                b.ForeColor = self._color("disabled_fg")
                b.FlatAppearance.MouseOverBackColor = self._color("disabled")
        except Exception as ex:
            log(f"cancel button style failed: {ex}")

    # ---------- 动作: 版本切换 ----------

    def _on_version_switch(self) -> None:
        if self.is_busy():
            return
        try:
            # 统一走 titlebar 的"打开=开始查看, 关闭=已读落定"入口:
            # 打开瞬间清主页面升级提示, 关闭后推进已读基准 (列表红点/主页提示随之消失)
            open_dialog = getattr(self.titlebar, "_open_update_dialog", None)
            if open_dialog is not None:
                open_dialog()
            else:
                show_update_dialog(self.titlebar)
        except Exception as ex:
            log(f"version switch failed: {ex}")
            self._msgbox("版本切换", f"无法打开版本切换窗口: {ex}")

    # ---------- 动作: 运行环境检测 (+自动补装/重建) ----------

    def _on_env_check(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        _log_ui_ts("=" * 44)
        _log_ui_ts("运行环境检测开始…")

        def _work() -> None:
            try:
                _log_ui_ts("- 检查 node / pnpm 可用性…")
                nb = _node_bin()
                pb = _pnpm_bin()
                _log_ui_ts(f"  node: {nb}")
                _log_ui_ts(f"  pnpm: {pb}")
                installed = (SOURCE / "node_modules" / ".modules.yaml").is_file()
                if not installed:
                    _log_ui_ts("- 依赖未安装, 执行 pnpm install…")
                    if not _install_deps():
                        _log_ui_ts("[FAILED] 依赖安装失败。")
                        return
                elif _deps_need_update():
                    _log_ui_ts("- lockfile 与已装依赖不一致, 更新环境依赖…")
                    if not _install_deps():
                        _log_ui_ts("[FAILED] 环境更新失败。")
                        return
                else:
                    _log_ui_ts("- 依赖已是最新 (lockfile 与 node_modules 一致)。")
                if not BACKEND_ENTRY.exists():
                    # 环境检测只报告缺失, 不触发构建 (构建由用户点
                    # "前后端构建"或"DSH 启动"时执行)。
                    _log_ui_ts("- 后端编译产物缺失 (请点\"前后端构建\"生成)。")
                else:
                    _log_ui_ts("- 后端编译产物已存在。")
                _log_ui_ts("运行环境检测完成: 环境就绪。")
            except Exception as ex:
                _log_ui_ts(f"[FAILED] 环境检测出错: {ex}")
            finally:
                self._ui_thread(lambda: self._set_busy(False))

        threading.Thread(target=_work, daemon=True).start()

    # ---------- 动作: 前后端构建 ----------

    def _on_build(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        threading.Thread(target=self._build_work, daemon=True).start()

    def _build_work(self) -> None:
        try:
            ok = run_build()
            if ok:
                fp = get_workspace_fingerprint()
                if fp:
                    record_fingerprint(fp)
            _log_ui_ts("构建流程结束。" if ok else "构建流程失败。")
        except Exception as ex:
            _log_ui_ts(f"[FAILED] 构建出错: {ex}")
        finally:
            self._ui_thread(lambda: self._set_busy(False))

    # ---------- 动作: 构建物清除 ----------

    def _on_clean(self) -> None:
        if self._busy:
            return
        self._set_busy(True)

        def _work() -> None:
            try:
                _log_ui_ts("=" * 44)
                _log_ui_ts("构建物清除开始…")
                _clean_build_artifacts()
                # 使构建指纹失效: 下次 build 后重新记录 (清除后产物缺失)
                try:
                    if MARKER.exists():
                        MARKER.unlink()
                        _log_ui_ts("- 已失效构建指纹 (last-build.txt)。")
                except OSError as ex:
                    _log_ui_ts(f"- 指纹清理失败: {ex}")
                _log_ui_ts("构建物清除完成。")
            except Exception as ex:
                _log_ui_ts(f"[FAILED] 构建物清除出错: {ex}")
            finally:
                self._ui_thread(lambda: self._set_busy(False))

        threading.Thread(target=_work, daemon=True).start()

    # ---------- 动作: DSH 启动 (后端 + webview 覆盖) ----------

    def _on_start_dsh(self) -> None:
        if self._busy or backend_running():
            return
        self._set_busy(True)
        _set_backend_starting(True)   # 启动中: 终止/重启立即可用

        def _work() -> None:
            global _JOB_HANDLE
            started_by_us = False
            try:
                try:
                    _log_ui_ts("=" * 44)
                    _log_ui_ts("正在启动 DSH…")
                    if not BACKEND_ENTRY.exists():
                        _log_ui_ts("- 后端编译产物缺失, 先执行构建…")
                        if not run_build():
                            _log_ui_ts("[FAILED] 构建失败, 启动中止。")
                            return
                        fp = get_workspace_fingerprint()
                        if fp:
                            record_fingerprint(fp)
                    # 启动中若用户点终止 (backend_starting 被清), 中止后续启动
                    if not backend_starting():
                        _log_ui_ts("- 启动已取消 (用户终止)。")
                        return
                    _port_reuse_check()
                    if not _BACKEND_PORT_IN_USE:
                        _log_ui_ts("- 启动后端进程…")
                        p = start_backend()
                        started_by_us = True
                        if _JOB_HANDLE is not None:
                            try:
                                _assign_pid_to_job(_JOB_HANDLE, p.pid)
                            except Exception as ex:
                                log(f"assign backend to job failed: {ex}")
                    if not backend_starting():
                        _log_ui_ts("- 启动已取消 (用户终止)。")
                        if started_by_us:
                            _stop_backend()
                        return
                    _log_ui_ts("- 等待后端就绪…")
                    if not _wait_backend_ready(WAIT_TIMEOUT):
                        if _ACTIVE["cancel"]:
                            _log_ui_ts("- 启动已取消。")
                        else:
                            _log_ui_ts(f"[FAILED] 后端未在 {WAIT_TIMEOUT}s 内就绪。")
                        if started_by_us:
                            _stop_backend()
                        return
                    web_url = URL
                    if started_by_us and _ACTIVE_BACKEND_LOG is not None:
                        web_url = _wait_web_url() or URL
                    _log_ui_ts(f"- 后端就绪: {URL}")
                    _set_backend_running(True)
                    # UI 线程: 控制面板隐藏 -> webview 覆盖内容区并加载页面
                    self._ui_thread(lambda: self._enter_webview(web_url))
                except Exception as ex:
                    _log_ui_ts(f"[FAILED] 启动 DSH 出错: {ex}")
                    try:
                        if started_by_us:
                            _stop_backend()
                    except Exception:
                        pass
            finally:
                _set_backend_starting(False)   # 无论成败都清除启动中
                try:
                    if not backend_running():
                        self._ui_thread(lambda: self._set_busy(False))
                    else:
                        self._ui_thread(lambda: self._set_busy(False))
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()

    def _enter_webview(self, web_url: str) -> None:
        """UI 线程: 隐藏控制面板, webview 加载后端页面覆盖内容区。"""
        try:
            self.hide()
            self.window.load_url(web_url)
            log(f"webview entered: {web_url}")
        except Exception as ex:
            log(f"enter webview failed: {ex}")
            self.show()

    # ---------- 动作: 终止 DSH ----------

    def _on_stop_dsh(self) -> None:
        # 仅当面板在跑其它操作 (构建/环境等, 非后端启动) 时才拦截;
        # 后端启动进行中 (backend_starting) 允许终止: 用户点电源立刻终止启动,
        # _ACTIVE["cancel"] 会让启动线程主动放弃, 不会并发踩踏。
        if self._busy and not backend_starting():
            return
        self._set_busy(True)
        # 立即清状态: 启动中被终止 -> 启动线程的取消检查生效;
        # 运行中被终止 -> 标题栏按钮立刻回到"未运行"。
        _set_backend_starting(False)
        _set_backend_running(False)
        # 标记取消: 让卡在 _wait_backend_ready 的启动线程立即放弃等待,
        # 终止/取消从"看似卡住"变成瞬间完成 (后端进程由 _stop_backend 杀掉)。
        _ACTIVE["cancel"] = True

        def _work() -> None:
            try:
                _log_ui_ts("=" * 44)
                _log_ui_ts("正在终止 DSH…")
                _stop_backend()
                _log_ui_ts("DSH 已终止。")
                self._ui_thread(self._exit_webview)
            except Exception as ex:
                _log_ui_ts(f"[FAILED] 终止 DSH 出错: {ex}")
            finally:
                self._ui_thread(lambda: self._set_busy(False))

        threading.Thread(target=_work, daemon=True).start()

    def _exit_webview(self) -> None:
        """UI 线程: webview 关闭内容 (about:blank), 显露控制面板。"""
        try:
            self.window.load_url("about:blank")
        except Exception as ex:
            log(f"webview close failed: {ex}")
        self.show()
        log("control panel restored")

    # ---------- 动作: 重启 DSH ----------

    def _on_restart_dsh(self) -> None:
        if self._busy:
            return
        self._set_busy(True)

        def _work() -> None:
            global _JOB_HANDLE
            try:
                _log_ui_ts("=" * 44)
                _log_ui_ts("正在重启 DSH…")
                _stop_backend()
                _set_backend_running(False)
                self._ui_thread(self._exit_webview)
                _log_ui_ts("- 重新启动后端…")
                _port_reuse_check()
                if not _BACKEND_PORT_IN_USE:
                    p = start_backend()
                    if _JOB_HANDLE is not None:
                        try:
                            _assign_pid_to_job(_JOB_HANDLE, p.pid)
                        except Exception as ex:
                            log(f"assign backend to job failed: {ex}")
                if not _wait_backend_ready(WAIT_TIMEOUT):
                    _log_ui_ts(f"[FAILED] 后端未在 {WAIT_TIMEOUT}s 内就绪。")
                    return
                web_url = URL
                if _ACTIVE_BACKEND_LOG is not None:
                    web_url = _wait_web_url() or URL
                _set_backend_running(True)
                self._ui_thread(lambda: self._enter_webview(web_url))
            except Exception as ex:
                _log_ui_ts(f"[FAILED] 重启 DSH 出错: {ex}")
            finally:
                self._ui_thread(lambda: self._set_busy(False))

        threading.Thread(target=_work, daemon=True).start()

    # ---------- 动作: 打开日志路径 / 清除日志 ----------

    def _on_open_log(self) -> None:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            os.startfile(str(LOG_FILE.parent))
            _log_ui_ts(f"已打开日志目录: {LOG_FILE.parent}")
        except Exception as ex:
            log(f"open log dir failed: {ex}")
            self._msgbox("打开日志路径", f"无法打开日志目录: {ex}")

    def _on_clear_log(self) -> None:
        _log_ui_ts("[日志] 清除日志显示 (磁盘文件不动)。")
        self.clear_log()

    # ---------- 动作: 取消进行中的命令 ----------

    def _on_cancel(self) -> None:
        """取消进行中的 环境检测/前后端构建/构建物清除 等命令。

        设置 _ACTIVE["cancel"] 让 _run_captured / _show_console_step 的
        循环自行终止, 同时立即杀掉当前活动子进程树 (taskkill /T /F)
        中断阻塞的 subprocess.run 阶段。"""
        if not self._busy:
            return
        try:
            _ACTIVE["cancel"] = True
            p = _ACTIVE.get("proc")
            if p is not None and p.poll() is None:
                try:
                    kill_tree(p.pid)
                    log(f"panel operation cancelled, killed pid={p.pid}")
                except Exception as ex:
                    log(f"panel cancel kill failed: {ex}")
            _log_ui_ts("[操作] 已请求取消, 正在终止子进程…")
        except Exception as ex:
            log(f"panel cancel failed: {ex}")

    # ---------- 工具 ----------

    def _ui_thread(self, fn) -> None:
        try:
            from System import Action
            if self.form.InvokeRequired:
                self.form.Invoke(Action(fn))
            else:
                fn()
        except Exception:
            try:
                fn()
            except Exception:
                pass

    def _msgbox(self, title: str, msg: str) -> None:
        try:
            from System.Windows.Forms import (MessageBox, MessageBoxButtons,
                                              MessageBoxIcon)
            MessageBox.Show(self.form, msg, title,
                            MessageBoxButtons.OK, MessageBoxIcon.Warning)
        except Exception:
            pass


def Color_White():
    from System.Drawing import Color
    return Color.White


def _style_winforms_button(btn, enabled: bool, enabled_style, dark: bool) -> None:
    """WinForms Button 统一启用/禁用配色 (禁用态: 灰底灰字且无 hover)。

    各对话框 (升级/设置/已是最新) 里的按钮在忙碌时会被 Enable=False 禁用,
    WinForms 默认的禁用渲染不可控 (保留彩底或把白字翻成黑字)。这里显式控制:
    启用 = 还原记录的本色 (bg, fg, hover); 禁用 = 主题灰底灰字, 且
    MouseOverBackColor 也设成灰底 (禁用按钮不接收鼠标事件, hover 天然失效)。
    enabled_style = (back, fore, hover) System.Drawing.Color, 可传 None 用当前值。"""
    try:
        from System.Drawing import Color as _C
        if enabled:
            if enabled_style is not None:
                bg, fg, hover = enabled_style
            else:
                bg, fg = btn.BackColor, btn.ForeColor
                hover = btn.FlatAppearance.MouseOverBackColor
            btn.FlatAppearance.MouseOverBackColor = hover
        else:
            if dark:
                bg, fg = _C.FromArgb(56, 56, 60), _C.FromArgb(120, 124, 130)
            else:
                bg, fg = _C.FromArgb(226, 230, 236), _C.FromArgb(148, 163, 184)
            btn.FlatAppearance.MouseOverBackColor = bg
        btn.UseVisualStyleBackColor = False
        btn.BackColor = bg
        btn.ForeColor = fg
    except Exception:
        pass


# ==================== 升级检测与版本更新 ====================
# 需求: 窗体标题栏最小化按钮左侧常驻"检查更新"按钮 (圆形旋转箭头图标 +
# 文字; 灰色=无更新, 蓝色+红点=有未查看的新版本); 点击弹出更新日志与
# 版本选择 (release 版 commit 列表, 显示 dsh-x.y.z tag 名), 确认后
# git checkout 切换版本并自动重建。
# 每次启动后台立即 fetch 官方 master (静默, 不动工作区), 检测到
# origin/master 领先本地 (有新 release 合并) 时蓝色提示即时点亮。
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

    首次调用探测并缓存。系统 pnpm 必须用 shutil.which 解析到完整路径:
    subprocess 的 CreateProcess 不按 PATHEXT 扩展自动补全, 而 npm 全局
    安装 pnpm (npm i -g pnpm / corepack enable) 只生成 pnpm.cmd / pnpm.ps1
    shim、没有 pnpm.exe —— 直接回退裸字符串 "pnpm" 会抛
    FileNotFoundError (WinError 2), 这正是首次安装报 "系统找不到指定的文件"
    的根因; 给完整 .cmd 路径则可直接启动。"""
    global _PNPM_BIN
    if _PNPM_BIN is None:
        import shutil as _shutil
        if PORTABLE_PNPM.is_file():
            _PNPM_BIN = str(PORTABLE_PNPM)
            log(f"using bundled pnpm: {_PNPM_BIN}")
        elif (SOURCE / "node_modules" / "pnpm" / "bin" / "pnpm.cjs").is_file():
            _PNPM_BIN = str(SOURCE / "node_modules" / "pnpm" / "bin" / "pnpm.cjs")
            log(f"using repo pnpm: {_PNPM_BIN}")
        else:
            sys_pnpm: str | None = _shutil.which("pnpm")
            if sys_pnpm:
                # .ps1 shim 无法被 CreateProcess 直接启动: 换 npm 全局安装的
                # pnpm.cjs 用 node 跑 (标准布局: shim 与 node_modules\pnpm\bin\ 同级)。
                if sys_pnpm.lower().endswith(".ps1"):
                    cjs = Path(sys_pnpm).resolve().parent / "node_modules" / "pnpm" / "bin" / "pnpm.cjs"
                    if cjs.is_file():
                        log(f"using npm-global pnpm: {cjs}")
                        _PNPM_BIN = str(cjs)
                        return _PNPM_BIN
                _PNPM_BIN = sys_pnpm
                log(f"using system pnpm: {_PNPM_BIN}")
            else:
                _PNPM_BIN = "pnpm"
                log("no pnpm found (bundled or system), pnpm commands will fail")
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


def _backend_supports_no_open() -> bool:
    """目标后端版本是否支持 `--no-open` 旗标。

    判定: 读 web-app bundle 的源码 startup.ts (版本切换 = git checkout 到
    目标提交, 源码随目标版本固定)。认证 (browser-auth, 2026-08-25) 引入于
    --no-open (2026-08-14) 之后, 需要 token 的版本必然支持该旗标; 更老的
    版本两者皆无, 传旗标会让 commander 报 unknown option 退出, 必须跳过。"""
    p = SOURCE / "packages" / "bundle" / "web-app" / "src" / "startup.ts"
    try:
        return "--no-open" in p.read_text(encoding="utf-8")
    except OSError:
        return False


def _start_cmd() -> list[str]:
    """启动后端的命令: node apps/cli/lib/bin.js web (node 用解析后的路径)。

    支持 --no-open 的版本 (含全部认证版本) 传该旗标: 桌面壳自己弹 WebView2
    窗口, 后端不必再弹系统默认浏览器; printUrl 仍固定为 true, 'dsh web:
    <带 token 的 URL>' 照常打印到 stdout (落盘 backend-*.log), 启动器据此
    解析启动令牌完成浏览器认证。--port 引入更早于 --no-open, 同一版本门内
    一并传: 让 DSH_PORT 真正决定后端监听端口 (此前后端固定 3080, 换了端口
    的启动器永远等不到就绪)。"""
    cmd = [_node_bin(), "apps/cli/lib/bin.js", "web"]
    if _backend_supports_no_open():
        cmd.append("--no-open")
        cmd.extend(["--port", str(PORT)])
    return cmd


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


def _read_update_state() -> tuple[str, int, str]:
    """读取升级通知持久化状态 (last-update-seen.txt, 三行):
    第 1 行 = 上次拉取记录的最新 origin/master commit (基准 B);
    第 2 行 = pending (1=有未查看的新 commit, 标题栏蓝字+红点; 0=已查看/无新更新);
    第 3 行 = 已读基准 seen_hash (用户最近一次打开"切换版本"界面时 origin/master 的
              commit; 晚于它的 release tag 才算"从未出现过"的新版本)。

    语义: 亮/灭由 pending 驱动 (pending 由已读基准 seen_hash 与检测到的新 release 决定),
    落盘后关闭程序再启动仍保留。旧版仅两行 -> 第 3 行缺失时以基准 B 作为已读基准
    (把 B 之前的 release 视为已看过, 避免升级后历史版本全部标红点)。"""
    try:
        if SEEN_MARKER.exists():
            lines = SEEN_MARKER.read_text(encoding="utf-8").splitlines()
            base = lines[0].strip() if lines else ""
            pending = 1 if (len(lines) > 1 and lines[1].strip() == "1") else 0
            seen = lines[2].strip() if len(lines) > 2 else ""
            if not seen:
                seen = base  # 旧版兼容: 无第3行时以基准 B 作为已读基准
            return base, pending, seen
    except OSError:
        pass
    return "", 0, ""


def _save_update_state(base: str, pending: int, seen_hash: str = "") -> None:
    """持久化升级通知状态 (基准 B + pending + 已读基准), 供启动恢复。"""
    try:
        SEEN_MARKER.write_text(
            f"{(base or '').strip()}\n{1 if pending else 0}\n"
            f"{(seen_hash or '').strip()}\n", encoding="utf-8")
    except OSError as ex:
        log(f"update state write failed: {ex}")

def _new_release_commits(info, seen_hash: str) -> list[dict]:
    """从检测结果 info 中过滤出真正"从未看过"的新 release tag 提交。

    入参 info["commits"] 是 "当前 HEAD 之后" 的新 release tag (check_for_update 产出),
    但"已看过与否"应相对已读基准 seen_hash 判定, 而不是相对当前 HEAD:
      - 用户 HEAD 停留在旧版本时, info["commits"] 会包含大量历史 release;
      - 已读基准 seen_hash (用户上次打开版本界面时 origin/master) 之后的 release 才算
        "从未出现过", 之前的 (seen_hash 的祖先) 都视为已看过。
    用 `git merge-base --is-ancestor <release-commit> <seen_hash>` 判断: rc==0 表示该
    release commit 已被 seen_hash 包含 (已读), 跳过; 否则算新版本。
    seen_hash 为空 (从未打开过版本界面 / 仓库不可用) 时全部算新。
    返回过滤后的 release dict 列表 (供 _handle_check_result 计数、_populate_list 标红点)。"""
    commits = (info.get("commits") or []) if info else []
    seen = (seen_hash or "").strip()
    out: list[dict] = []
    for c in commits:
        h = (c.get("hash") or "").strip()
        if not h:
            continue
        if seen:
            r = _git(["merge-base", "--is-ancestor", h, seen], timeout=30)
            if r[0] == 0:
                continue  # 该 release 已被已读基准包含 -> 看过, 不算新
        out.append(c)
    return out


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
    """拉取官方 master 到 origin/master (默认走 origin remote, SSH 失败
    (未配 key / 认证失败 / 网络) 时回退 HTTPS URL 拉取), 并跟随 release
    tags (dsh-v*): 版本列表 / 更新检测直接读本地 refs/tags, 需要 fetch
    同步官方新 tag。"""
    r = _git(["fetch", "origin", "master", "--tags"], timeout=180)
    if r[0] == 0:
        return r
    log("fetch via origin failed, falling back to https url: " + r[2].strip()[:200])
    return _git(["fetch", "--tags", REPO_URL_HTTPS,
                 "master:refs/remotes/origin/master"], timeout=180)


def check_for_update() -> dict | None:
    """检测官方仓库相对当前 HEAD 的**新 release tags (dsh-v*)**。

    只有 fetch 后本地出现 HEAD 之后的新 release tag 才算"有新版本":
    普通 PR 合并 / 未打 tag 的 release 分支合并不算 (available=False 仍表示
    检测成功, 只是无新 release)。网络/仓库异常返回 None (调用方保持现状)。"""
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
    # 统计本地 HEAD 之后的新 release tags (dsh-v*): fetch 已跟随官方 tag,
    # tag 指向的 commit 出现在 HEAD..origin/master 里即为"新版本"。普通 PR
    # 合并 / 未打 tag 的 release 分支合并不算 (与 GitHub Releases 对应)。
    ahead_r = _git(["rev-list", "HEAD..origin/master"])
    if ahead_r[0] != 0:
        log("update check: cannot list commits")
        return None
    ahead = set(ahead_r[1].split())
    tags_r = _git(["tag", "-l", _TAG_PREFIX], timeout=30)
    new_tags: list[tuple[str, str, str]] = []  # (tag, commit, date)
    if tags_r[0] == 0 and tags_r[1]:
        for t in tags_r[1].splitlines():
            t = t.strip()
            if not t:
                continue
            c_r = _git(["rev-parse", t + "^{commit}"], timeout=30)
            if c_r[0] != 0:
                continue
            commit = c_r[1].strip()
            if commit not in ahead:
                continue
            d_r = _git(["log", "-1", "--format=%ad", _GIT_DATE_FMT, commit],
                       timeout=30)
            new_tags.append((t, commit,
                             d_r[1].strip() if d_r[0] == 0 else ""))
    new_tags.sort(key=lambda x: x[2], reverse=True)  # 新->旧
    count = len(new_tags)
    latest_r = _git(["rev-parse", "origin/master"])
    head = head_r[1].strip()
    latest = latest_r[1].strip() if latest_r[0] == 0 else ""
    info = {"available": count > 0, "count": count, "demo": False,
            "head": head, "head_short": head[:7],
            "latest": latest, "latest_short": latest[:7],
            "commits": []}
    if count > 0:
        for tag, commit, date in new_tags:
            info["commits"].append(
                {"hash": commit, "short": commit[:7], "date": date,
                 "subject": tag})
        log(f"update check: {count} new release tag(s) past local head")
    else:
        log("update check: up to date (no new release tag)")
    return info


def perform_update(target_ref: str, progress=None, demo: bool = False) -> tuple[bool, str]:
    """把仓库强制切换到 target_ref (本地已存在的 commit/ref)。

    可见 cmd 流程 (每步独立弹窗, 完成自动关闭, 失败 pause 供查看):
      1. git cmd: 校验目标本地存在 -> checkout -f 强制切换 -> 打印新 HEAD
         (显示"切换完成"后窗口自动关闭);
      2. 环境更新: lockfile 与已安装依赖不一致时弹出 pnpm install cmd
         ("环境更新完成"后自动关闭); 无环境更新则跳过;
      3. 返回后由调用方 (_start_rebuild_after_switch) 弹出构建 cmd。
    切换后处于 detached HEAD。切换仅针对本地已有提交 (fetch 由后台更新
    检测线程 / 对话框"获取最新仓库"按钮负责)。
    返回 (ok, message)。progress(msg) 可选回调 (后台线程调用, 调用方负责封送)。"""
    _ACTIVE["cancel"] = False   # 复位可能残留的取消标记, 避免切换版本的 git/install 被误取消
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

    ref12 = target_ref[:12]
    gb = _git_bin()
    git_exec = f'"{gb}"' if (os.path.isabs(gb) or " " in gb) else gb
    src = '"' + str(SOURCE) + '"'

    # ---- 1) 执行 git 切换: 校验 + 切换 + 打印新 HEAD, 输出进日志区 ----
    _say(f"切换版本到 {ref12} …")
    git_body = (
        "chcp 65001 >nul & "
        "echo. & echo ============================================ & "
        f"echo  正在切换版本: {ref12} ... & "
        "echo ============================================ & "
        f"{git_exec} -C {src} rev-parse --verify --quiet {target_ref} && "
        f"{git_exec} -C {src} checkout -q -f {target_ref} && "
        "echo. & echo 切换完成: & "
        f"{git_exec} -C {src} log -1 --format=\"   %h %s\" & "
        "echo. & echo ------------- & exit 0"
    )
    if not _show_console_step("切换版本 (git checkout)", git_body, timeout=300):
        return False, ("切换版本失败: target=" + ref12
                       + "\n\n强制切换会丢弃工作区未提交的改动。"
                       + "\n请查看日志区中的错误信息。")

    new_r = _git(["rev-parse", "HEAD"])
    new_head = new_r[1].strip() if new_r[0] == 0 else "?"
    # 记录当前提交 (与 DSH_Desktop/last-commit.txt 的既有约定一致)
    try:
        (BUILD_DIR / "last-commit.txt").write_text(new_head + "\n", encoding="utf-8")
    except OSError as ex:
        log(f"update: last-commit write failed: {ex}")
    # 使构建指纹失效: 调用方随后立即重新构建; 若构建失败, 需在控制面板
    # 点"前后端构建"手动重试 (启动不再自动构建)。
    try:
        if MARKER.exists():
            MARKER.unlink()
            log("update: build fingerprint invalidated (rebuild follows)")
    except OSError as ex:
        log(f"update: fingerprint invalidate failed: {ex}")

    # ---- 2) 环境更新: lockfile 与已装依赖不一致才弹可见 pnpm install cmd ----
    msg = f"已切换到 {new_head[:12]}（强制切换，工作区改动已丢弃）。"
    if _deps_need_update():
        _say("安装依赖 (pnpm install) …")
        # 关键: pnpm install 前先停掉旧后端进程, 释放 node_modules 里的
        # 文件锁 (Windows 上运行中的后端会锁住 .js/.node, 导致删除/重建
        # 报 EPERM/EBUSY 失败)。构建完成后 _restart_backend_blocking 会
        # 重新启动新后端的产物。
        _stop_backend()
        store = '"' + str(DATA_DIR / "pnpm-store") + '"'
        install_body = (
            "chcp 65001 >nul & set CI=true & "
            "echo. & echo ============================================ & "
            "echo  正在更新环境依赖 (pnpm install) ... & "
            "echo ============================================ & "
            + _pnpm_cmd("install --config.confirmModulesPurge=false --store-dir " + store)
            + " && echo. & echo 环境更新完成 & "
            "echo ------------- & exit 0"
        )
        if not _show_console_step("环境更新 (pnpm install)", install_body, timeout=1800):
            log("update: pnpm install failed (see log panel)")
            msg += "依赖安装未完成 (pnpm install 失败)，重新构建可能失败。"
        else:
            msg += "依赖已更新。"
    else:
        log("update: dependency lockfile unchanged, skip pnpm install")
        msg += "依赖无更新，无需重新安装。"
    _say("完成")
    return True, msg + "正在重新构建后端…"


def _wait_backend_ready(timeout: float | None = None) -> bool:
    """轮询 http://127.0.0.1:PORT 直到就绪 (默认 WAIT_TIMEOUT 秒)。

    期间若用户点了"取消"或"终止"(都会置位 _ACTIVE["cancel"]), 立即返回
    False —— 否则启动线程会一直卡在轮询里, 表现为后端"很难终止"、启动中
    状态迟迟不结束。成功返回 True。"""
    deadline = time.time() + (timeout if timeout is not None else WAIT_TIMEOUT)
    while time.time() < deadline:
        if _ACTIVE["cancel"]:
            return False
        if http_ready():
            return True
        time.sleep(0.5)
    return False


def _stop_backend() -> None:
    """停掉当前后端进程树并等待其占用的端口释放 (不重新启动)。

    切换版本时, 后端 node 进程正加载着 node_modules 里的 .js/.node
    文件; 在 Windows 上这些文件被进程占住, pnpm install / 构建清理
    想删除/替换时会撞文件锁 (EPERM/EBUSY) 而失败。因此必须在
    pnpm install 之前先把后端停掉、释放文件锁, 再装依赖, 之后
    _restart_backend_blocking 再启动新后端的产物。

    注意: 只停不启。调用方若无后续启动逻辑 (如切换失败提前返回),
    需自行决定是否把端口/后端交给谁。"""
    global _JOB_HANDLE
    proc = _ACTIVE.get("proc")
    if proc is not None and proc.poll() is None:
        log(f"stop backend: killing backend pid={proc.pid}")
        kill_tree(proc.pid)
        _ACTIVE["proc"] = None
    # 等端口释放 (kill_tree 异步, 轮询); 用户已请求取消/终止则立即退出, 不再等
    for _ in range(40):
        if _ACTIVE["cancel"]:
            break
        if not port_open("127.0.0.1", PORT):
            break
        time.sleep(0.25)
    if port_open("127.0.0.1", PORT):
        log("stop backend: port still occupied (not ours?)")


def _restart_backend_blocking(timeout: float | None = None) -> bool:
    """杀旧后端进程树 -> 启动新后端 (纳入 Job) -> 等待就绪。

    用于切换版本重新构建后加载新产物。返回是否就绪; 端口被非 DSH
    进程占用时只等待就绪 (不杀)。"""
    global _JOB_HANDLE
    _ACTIVE["cancel"] = False   # 复位可能残留的取消标记, 避免本次重启被误判为取消
    _stop_backend()
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
    需在控制面板点"前后端构建"手动重试, 当前仍用旧后端)。"""
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
                _finish_ui("重新构建后端失败。代码已切换，请在控制面板点\"前后端构建\"手动重试。")
                return
            fp = get_workspace_fingerprint()
            if fp:
                record_fingerprint(fp)
                log("rebuild after switch: fingerprint recorded")
            # 控制面板模式: 切换版本后只重新构建 + 刷新版本显示, 不自动
            # 启动后端/webview (用户点 DSH 启动时才进入界面)。
            _ui(lambda: _refresh_panel_after_switch(titlebar))
            log("rebuild after switch: done (panel mode, backend not auto-started)")
        except Exception as ex:
            log(f"rebuild after switch failed: {ex}")
            _finish_ui(f"切换后重建流程出错: {ex}")

    threading.Thread(target=_work, daemon=True).start()


def _refresh_panel_after_switch(titlebar) -> None:
    """版本切换+重建完成后刷新控制面板的版本显示与按钮状态。"""
    try:
        panel = getattr(titlebar, "_panel", None)
        if panel is not None:
            panel._version = _current_version_info()
            panel._render_version()
            panel.refresh_buttons()
            _log_ui_ts("版本切换完成: 已更新当前版本显示。")
    except Exception as ex:
        log(f"refresh panel after switch failed: {ex}")


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

# 官方 release tag 前缀: 发布版本都会打 dsh-v* tag (如 dsh-v0.1.2-alpha.1),
# 与 GitHub Releases 页面一一对应; 合并了 release 分支但未发布的提交不打 tag。
_TAG_PREFIX = "dsh-v*"


def _current_version_info() -> dict:
    """解析当前 HEAD 的版本信息 (控制面板左上角大 label 用)。

    返回: {"tag": str|None, "commit": full hash, "short": 7位短哈希}。
    tag = 当前 HEAD 直接指向的 dsh-v* tag (--points-at HEAD), 没有则
    回退到最近的祖先 release tag (git describe --tags --abbrev=0)。
    仓库不可用 (未克隆/git 失败) 时 tag=None、commit="?"。"""
    head_r = _git(["rev-parse", "HEAD"])
    if head_r[0] != 0:
        return {"tag": None, "commit": "?", "short": "?"}
    head = head_r[1].strip()
    tag = None
    r = _git(["tag", "-l", _TAG_PREFIX, "--points-at", "HEAD"], timeout=30)
    if r[0] == 0 and r[1].strip():
        tag = r[1].splitlines()[0].strip()
    if tag is None:
        d = _git(["describe", "--tags", "--abbrev=0"], timeout=30)
        if d[0] == 0 and d[1].strip():
            cand = d[1].strip()
            if cand.startswith("dsh-v"):
                tag = cand
    return {"tag": tag, "commit": head, "short": head[:7]}


def _list_local_commits(limit: int = 100) -> tuple[list[dict], str]:
    """读本地仓库的正式 release tags (dsh-v*) 列表 (新->旧) 与当前 HEAD。

    直接读 refs/tags/dsh-v* (fetch 已跟随官方 tag): 每个 tag 即一个已发布
    版本, subject 列显示 tag 名 (如 dsh-v0.1.2-alpha.1), hash/date 取 tag
    指向的 commit。按 commit 时间新->旧排序后截取上限。
    返回 (commits, head_hash)。"""
    rows: list[dict] = []
    tag_r = _git(["tag", "-l", _TAG_PREFIX], timeout=30)
    if tag_r[0] == 0 and tag_r[1]:
        for tag in tag_r[1].splitlines():
            tag = tag.strip()
            if not tag:
                continue
            c_r = _git(["rev-parse", tag + "^{commit}"], timeout=30)
            if c_r[0] != 0:
                continue
            commit = c_r[1].strip()
            d_r = _git(["log", "-1", "--format=%ad", _GIT_DATE_FMT, commit],
                       timeout=30)
            rows.append({"hash": commit, "short": commit[:7],
                         "date": d_r[1].strip() if d_r[0] == 0 else "",
                         "subject": tag})
    # 按 commit 时间新->旧排序 (日期字符串 "YYYY-MM-DD HH:MM:SS" 可直接排序)
    rows.sort(key=lambda r: r["date"], reverse=True)
    commits = rows[:limit]
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
    # 布局: 880x520 内容 + 自绘标题栏, 时间列显示到秒, 列表拉宽不拥挤
    form.ClientSize = Size(int(880 * s), int(520 * s))
    try:
        # 头部文字/按钮保持标准 9.5pt (用户确认此大小正常);
        # commit 列表字体的放大见下方 lv.Font (单独乘 s)
        form.Font = Font("Microsoft YaHei UI", 9.5)
    except Exception:
        pass
    # 自绘标题栏 (主题色背景 + 图标 + 标题 + 关闭按钮, 可拖动)
    tb = _install_dialog_chrome(form, "升级 DSH Desktop", dark, s,
                                lambda: form.Close())
    form.ClientSize = Size(int(880 * s), int(520 * s) + tb)

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
    W = int(836 * s)
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
    try:
        # commit 列表字体: 8.5pt * 缩放 (比 9.5pt 小一号, 列表内容更宽松;
        # 头部文字/按钮仍是标准 9.5pt, 见 form.Font)
        lv.Font = Font("Microsoft YaHei UI", 8.5 * s)
    except Exception:
        pass
    lv.View = _View.Details
    lv.FullRowSelect = True
    lv.MultiSelect = False
    lv.HideSelection = False
    lv.HeaderStyle = _CHS(0)            # None (隐藏表头, 更像 git log 文本)
    # 列宽总和预留竖向滚动条, 避免出现横向滚动条
    lv.Columns.Add("sel", int(34 * s))
    lv.Columns.Add("hash", int(88 * s))
    lv.Columns.Add("date", int(180 * s))
    lv.Columns.Add("subject", int(W - 368 * s))
    lv.Columns.Add("new", int(44 * s))
    lv.BackColor = Color.FromArgb(30, 30, 33) if dark else Color.White
    lv.ForeColor = Color.FromArgb(229, 231, 235) if dark else Color.FromArgb(31, 41, 55)
    cur_fg = Color.FromArgb(166, 171, 179) if dark else Color.Gray
    cur_bg = Color.FromArgb(70, 71, 76) if dark else Color.FromArgb(226, 228, 231)
    # 单选列字体: ●/○ 是同一字体的配套几何符号 (外径一致), Segoe UI Symbol
    # 渲染更清晰且圈更大, 避免默认字体下未选中圈偏小、与选中圈对不上。
    # 随列表字体同步缩小一号 (11pt * scale, 与 8.5pt 列表文字协调)。
    try:
        from System.Drawing import Font as _Font
        _radio_font = _Font("Segoe UI Symbol", 11.0 * s)
    except Exception:
        _radio_font = None
    form.Controls.Add(lv)

    # ---------- 新版本标记: 用默认渲染支持的 NEW 列 (橙底白字), 不接管 OwnerDraw ----------
    # 之前用 OwnerDraw 自绘导致 hover 时文字消失 / 当前版本灰底失效 (系统默认渲染被替换)。
    # 这里改回系统默认渲染: 新增"new"列, 新版本行该列显示橙底白字 NEW, 无任何 hover 副作用;
    # 当前版本行用 item.BackColor/ForeColor (cur_bg/cur_fg) 灰底灰字, 由系统默认绘制正确渲染。
    _new_bg = Color.FromArgb(249, 115, 22)   # 橙色 (orange-500)

    rows = []  # {"item": ListViewItem, "commit": dict, "current": bool, "new": bool}
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
            # 用于标红的"未读新版本": demo 用初始 info, 非 demo 实时读 titlebar._update_info
            cur_info = info
            if not demo:
                cur_info = getattr(titlebar, "_update_info", None) or {}
            seen_hash = getattr(titlebar, "_update_seen_hash", "") or ""
            unseen = {c.get("hash") for c in _new_release_commits(cur_info, seen_hash)}
            for c in commits:
                is_cur = bool(head) and c.get("hash") == head
                is_new = c.get("hash") in unseen
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
                # 新版本: 最右侧 NEW 列 (橙底白字, 默认渲染; 当前版本行/普通行留空)
                new_cell = item.SubItems.Add("NEW" if (is_new and not is_cur) else "")
                if is_new and not is_cur:
                    new_cell.ForeColor = Color.White
                    new_cell.BackColor = _new_bg
                if is_cur:
                    item.ForeColor = cur_fg
                    item.BackColor = cur_bg     # 当前版本整行灰背景
                lv.Items.Add(item)
                rows.append({"item": item, "commit": c, "current": is_cur, "new": is_new})
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
            # 前 4 列实际宽度 = sel(34*s) + hash(88*s) + date(180*s) + new(44*s)。
            # 必须用缩放后的值: 原来硬编码 302 (未乘 s), 缩放 >1 时 subject
            # 列被设得过宽, 总和超出列表宽度 -> 出现横向滚动条。
            used = int(34 * s) + int(88 * s) + int(180 * s) + int(44 * s)
            lv.Columns[3].Width = max(80, lv.ClientSize.Width - used - scroll)
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

    from System.Windows.Forms import FlatStyle, ControlStyles
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
    _right = int(880 * s) - int(22 * s)
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
        b.SetStyle(ControlStyles.Selectable, False)   # 鼠标点击也无法获焦, 不画焦点白框
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
    btn_ok.SetStyle(ControlStyles.Selectable, False)   # 鼠标点击也无法获焦, 不画焦点白框
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

    def _capture_style(btn):
        try:
            return (btn.BackColor, btn.ForeColor, btn.FlatAppearance.MouseOverBackColor)
        except Exception:
            return None

    _ok_style = _capture_style(btn_ok)
    _fetch_style = _capture_style(btn_fetch)

    def _target_ref() -> str:
        if lv.SelectedIndices.Count > 0:
            return rows[lv.SelectedIndices[0]]["commit"]["hash"]
        return "origin/master"

    def _set_busy(busy: bool) -> None:
        # 忙碌期间 (拉取仓库/切换版本): 切换版本 + 获取最新仓库按钮都禁用,
        # 避免重复操作; 取消按钮始终可用 (用户仍可关闭对话框)。
        # 禁用时统一灰底灰字且无 hover (与主窗口按钮一致的"不可用"样式)。
        btn_ok.Enabled = not busy
        btn_fetch.Enabled = not busy
        btn_cancel.Enabled = True
        _style_winforms_button(btn_ok, not busy, _ok_style, dark)
        _style_winforms_button(btn_fetch, not busy, _fetch_style, dark)
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
                # 手动拉取: 只更新基准 B (红点蓝字不需要, 用户已在查看界面)
                updater = getattr(titlebar, "_handle_manual_fetch", None)
                if updater is not None:
                    updater(info2)
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

                def _request_cookie(e) -> str | None:
                    """取 WebView2 本次请求自带的 Cookie 头。

                    认证流程里 WebView2 先访问带 token 的 URL (放行) → 后端
                    303 + Set-Cookie → 重定向到 / 的请求已带签名 cookie;
                    这里原样转发, 后端才能返回真正的 index.html。"""
                    try:
                        hdrs = e.Request.Headers
                        for name in ("Cookie", "cookie"):
                            try:
                                if hdrs.Contains(name):
                                    val = str(hdrs.GetHeader(name)).strip()
                                    if val:
                                        return val
                            except Exception:
                                continue
                    except Exception:
                        pass
                    return None

                def _on_wrr(s, e) -> None:
                    try:
                        uri = str(e.Request.Uri)
                        if uri.rstrip("/") != URL:
                            return  # 只注入根文档, 其他 Document 请求放行
                        # 用 http.client 直接读 (不走系统代理, 快); 必须带上
                        # WebView2 请求的 Cookie: 裸请求无 cookie 会被新版后端
                        # (browser-auth) 401, 再把 401 文本合成 200 返回 →
                        # 页面恒显示认证提示。
                        headers = {"Accept": "text/html"}
                        cookie = _request_cookie(e)
                        if cookie is not None:
                            headers["Cookie"] = cookie
                        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
                        conn.request("GET", "/", headers=headers)
                        resp = conn.getresponse()
                        if resp.status != 200:
                            # 未认证/后端异常: 放行, 让 WebView2 自行请求,
                            # 如实呈现后端的真实响应 (不伪装 200)。
                            conn.close()
                            return
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
          - 恢复无边框窗的 WS_MINIMIZEBOX/WS_SYSMENU, 让任务栏图标单击能最小化
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

        def _restore_taskbar_minimize_capable(self) -> None:
            """恢复无边框窗的任务栏最小化能力。

            pywebview 的 frameless 把 FormBorderStyle 设为 None, 这会清掉
            WS_MINIMIZEBOX (0x20000) 与 WS_SYSMENU (0x80000)。Windows 默认
            "单击任务栏图标: 窗口未最小化 -> 最小化 / 已最小化 -> 还原" 依赖
            这两个样式; 缺失时单击任务栏图标只会把窗口激活到前台, 不会最小化。
            这里在窗口可见前 (Load) 补回, 恢复系统默认的任务栏单击行为。
            只改样式位, 不引入系统标题栏/边框 (无边框视觉保持不变)。"""
            try:
                hwnd = self.Handle.ToInt32()
                user32 = ctypes.windll.user32
                user32.GetWindowLongW.restype = wintypes.LONG
                user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
                user32.SetWindowLongW.restype = wintypes.LONG
                user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
                GWL_STYLE = -16
                WS_MINIMIZEBOX = 0x00020000
                WS_SYSMENU = 0x00080000
                style = int(user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE))
                if not (style & (WS_MINIMIZEBOX | WS_SYSMENU)):
                    user32.SetWindowLongW(
                        wintypes.HWND(hwnd), GWL_STYLE,
                        style | WS_MINIMIZEBOX | WS_SYSMENU)
                    log("taskbar minimize styles restored (WS_MINIMIZEBOX|WS_SYSMENU)")
            except Exception as ex:
                log(f"taskbar minimize style failed: {ex}")

        def _on_dsh_load(self, sender, e):
            # 恢复无边框窗的任务栏最小化能力: FormBorderStyle.None 会清掉
            # WS_MINIMIZEBOX/WS_SYSMENU, 导致窗口未最小化时单击任务栏图标
            # 只会激活而不会最小化。Load 在窗口可见前触发, 此时改样式即可。
            self._restore_taskbar_minimize_capable()
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

    # 0.6) 首次运行: release 包不带后端仓库, 首次启动用内嵌 git 拉取官方仓库。
    #      依赖安装 / 构建 / 后端启动一律不收在启动流程里, 交给用户通过
    #      控制面板的"运行环境检测"(装/更新依赖) +"前后端构建"(生成编译产物)
    #      手动触发 (需求: 打开时先不打开后端和 webview; 启动只保证仓库可拉取)。
    if not _repo_valid():
        if _git_worktree_ok():
            # 有有效 .git: 断点续传 (进度条从 0 走是本次传输进度, 不是重新下载)
            _splash_set_progress(5, "首次运行：检测到已下载内容，正在续传…")
        else:
            _splash_set_progress(5, "首次运行：正在从官方仓库拉取代码（需联网）…")
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

    # 1) 创建 Job (KILL_ON_JOB_CLOSE): 本进程退出 -> 后端必死 (内核级, 含强杀)。
    #    不再自动构建/自动启动后端: 窗体打开显示控制面板, 由用户手动触发
    #    (需求: 打开时先不打开后端和 webview; 依赖安装 + 构建 + 启动均由
    #     控制面板按钮手动触发, 启动只负责拉取缺失的官方仓库)。
    global _JOB_HANDLE
    _JOB_HANDLE = _create_kill_job()

    # 5) WebView2 窗口 (frameless + 原生自绘标题栏)
    # 控制面板模式下 WebView2 先加载空白页 (被控制面板盖住); 用户点
    # "DSH 启动"后 ControlPanel 才启动后端并 load_url(带 token 的 URL)
    # 覆盖内容区。认证 token 解析逻辑移入 ControlPanel._on_start_dsh。
    try:
        import webview
    except ImportError as e:
        _close_splash()
        log(f"webview import failed: {e}; run 00_env.bat (creates DSH_Desktop/.venv with pywebview)")
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
    # 控制面板模式: WebView2 初始加载空白页 (被控制面板盖住), 点 DSH 启动后
    # 由 ControlPanel 启动后端并 load_url(带 token 的 URL) 覆盖内容区。
    init_web_url = "about:blank"
    window = webview.create_window(
        "DSH Desktop",
        init_web_url,
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
        # UI 线程: 原生标题栏 + 控制面板 + 边缘缩放 + 系统托盘
        global _MAIN_FORM
        try:
            bar = TitleBar(window)
            bar.install()
            api.bind(bar)
            window._titlebar_ref = bar  # 保活, 防 GC 导致事件失效
            log("custom titlebar installed")
        except Exception as e:
            log(f"titlebar install failed: {e}")
            bar = None
        # 控制面板 (原生主页): 盖住 webview, 初始显示 (不加载后端页面)。
        # 点 DSH 启动后 hide() 让 webview 覆盖内容区; 终止后 show() 显露。
        panel = None
        if bar is not None:
            try:
                panel = ControlPanel(window, bar)
                panel.install()
                panel.show()
                bar._panel = panel       # 标题栏控制按钮 -> panel 动作
                bar.refresh_control_buttons()
                window._panel_ref = panel  # 保活
                log("control panel installed (panel mode)")
            except Exception as ex:
                log(f"control panel install failed: {ex}")
        # 记录 form 引用 (托盘"显示窗口/退出"用)
        try:
            _MAIN_FORM = window.native
        except Exception as ex:
            log(f"main form ref failed: {ex}")
        # 拦截窗口关闭 (X 按钮/Alt+F4): 非退出模式 -> 隐藏到托盘
        try:
            form = window.native

            def _on_form_closing(sender, args) -> None:
                # 托盘"退出"(_ALLOW_CLOSE) 或 设置"关闭=结束进程" -> 真正退出;
                # 否则 (关闭=隐藏到托盘) 拦截关闭, 隐藏窗口, 后端继续跑
                if not _ALLOW_CLOSE and get_close_behavior() != "exit":
                    args.Cancel = True
                    _hide_main_window()

            form.FormClosing += _on_form_closing
            log("form closing interception installed (close -> tray)")
        except Exception as ex:
            log(f"form closing interception failed: {ex}")
        # 系统托盘 (右键: 显示窗口 / 退出); 字体随 DPI 缩放 (bar._scale)
        try:
            if bar is not None:
                _setup_tray(window.native, bar._scale)
        except Exception as ex:
            log(f"tray setup failed: {ex}")
        # 后台定期检测官方仓库更新: 结果驱动控制面板"有更新"badge
        # (标题栏"检查更新"按钮已移除, 版本切换入口移到控制面板)
        if bar is not None:
            try:
                bar.start_update_checker()
            except Exception as ex:
                log(f"update checker start failed: {ex}")

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
    if backend_running():
        try:
            proc = _ACTIVE.get("proc")
            if proc is not None and proc.poll() is None:
                log("launcher exit, killing backend tree")
                kill_tree(proc.pid)
        except Exception as ex:
            log(f"launcher exit backend kill failed: {ex}")
    if _JOB_HANDLE is not None:
        _kernel32.CloseHandle(_JOB_HANDLE)
        log("kill-on-close job handle closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
