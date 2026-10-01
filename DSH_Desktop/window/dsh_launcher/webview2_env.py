"""WebView2 用户数据目录 (按实例隔离) 与环境预热/旧目录清理。"""

import os
import time
from pathlib import Path

from .paths import WEBVIEW2_DATA_BASE
from .logs import log
from .proc import hidden_run


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
