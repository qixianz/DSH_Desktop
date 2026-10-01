"""子进程: 捕获输出运行、隐藏窗口运行、结束进程树, 端口/HTTP 探测, 启动取消状态。"""

import http.client
import os
import socket
import subprocess
import threading
import time

from .paths import PORT, SOURCE
from .logs import log, _log_ui_ts


def _run_captured(cmd, cwd=None, env=None, timeout=None,
                  emit_lines: bool = True, prefix: str = "",
                  status_interval: float | None = None,
                  status_message: str = "") -> tuple[int, list[str]]:
    """在仓库 (SOURCE) 内运行命令, 逐行捕获 stdout/stderr 追加到日志区。

    全程静默 (CREATE_NO_WINDOW + SW_HIDE), 不弹任何独立控制台窗口,
    输出实时回填到控制面板右侧日志区。cmd 为 list (Popen list 模式) 或字符串。
    返回 (returncode, lines)。支持取消 (_ACTIVE["cancel"]) 与超时杀进程树。"""
    # 延迟导入: tools 依赖 proc, 顶层导入会循环
    from .tools import _node_env
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
    last_activity = time.monotonic()

    def _reader(p=p):
        nonlocal last_activity
        try:
            for line in p.stdout:
                last_activity = time.monotonic()
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
                if status_interval and status_message and time.monotonic() - last_activity >= status_interval:
                    _log_ui_ts(status_message)
                    last_activity = time.monotonic()
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
def _no_window_startup() -> tuple[int, object | None]:
    """subprocess 无窗口启动参数 (Windows)。"""
    if os.name != "nt":
        return 0, None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return subprocess.CREATE_NO_WINDOW, si

# 启动流程取消状态: 用户点击 splash 关闭按钮时置位, 终止进行中的
# clone/install/build 子进程并让 main() 尽快退出。
_ACTIVE = {"proc": None, "cancel": False}
