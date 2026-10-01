"""启动期诊断采样: RedirectionGuard 位 + 真实鼠标光标 + 窗口消息循环状态。

用途: 定位"主界面鼠标悬停显示转圈光标, 点一下恢复正常"这类问题。

为什么需要落盘诊断: 该现象依赖真实的鼠标输入路径 (WinForms 通过
WM_SETCURSOR 响应系统反馈光标 IDC_APPSTARTING), 用程序化方式移动光标
或构造消息都无法复现, 因此只能在用户现场采集原始量再判定。本模块把判定
所需的全部量按时间线记录到日志, 不做任何行为改动。

每条采样记录:
  RT          - 本进程 RedirectionGuard EnforceRedirectionTrust 位
                (1 = 拒绝遍历非提权用户创建的 junction, pnpm 依赖树不可用)
  cursor      - 系统当前真实光标 (APPSTARTING = 系统的"转圈"反馈光标)
  in_win      - 鼠标是否落在主窗口内 (光标读数只在窗口内才有意义)
  hung        - IsHungAppWindow: 窗口消息队列是否被阻塞
  fg          - 主窗口是否为前台窗口
  wf_cursor   - WinForms 侧 form.Cursor 当前值
  wait_cursor - Application.UseWaitCursor 当前值

用 grep '[curdiag]' 过滤日志即可拿到整条时间线。
"""

import ctypes
import threading
import time
from ctypes import wintypes

from .logs import log

# 采样是否已启动 (防重入; shown 事件重复触发时不重复刷日志)
_STARTED = {"flag": False}

# 采样节奏: 前 6 次每 0.5s (窗口刚显示, 用户视线正好在此), 之后 6 次每 2s。
# 覆盖"打开就在转圈"与"稍后才转圈"两种时序。
_SAMPLE_DELAYS = [0.5] * 6 + [2.0] * 6

# 标准光标 ID -> 名称。IDC_APPSTARTING 就是"箭头 + 转圈"的反馈光标, 是
# 本诊断最关心的读数。
_IDC_NAMES = {
    32512: "ARROW",
    32513: "IBEAM",
    32514: "WAIT(busy)",
    32515: "CROSS",
    32516: "UPARROW",
    32642: "SIZENWSE",
    32643: "SIZENESW",
    32644: "SIZEWE",
    32645: "SIZENS",
    32646: "SIZEALL",
    32648: "NO",
    32649: "HAND",
    32650: "APPSTARTING(feedback-busy)",
    32651: "HELP",
}

# HCURSOR -> 名称 (LoadCursor 取回标准光标句柄后建表, 用于反查 GetCursorInfo)
_CURSOR_NAMES: dict[int, str] = {}


class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", ctypes.c_void_p), ("ptScreenPos", wintypes.POINT)]


def _load_user32():
    """取 user32 并声明签名 —— 64 位下 HWND 必须显式声明, 否则被截断。"""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
    u.GetCursorInfo.restype = wintypes.BOOL
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.GetCursorPos.restype = wintypes.BOOL
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.GetForegroundWindow.restype = wintypes.HWND
    u.IsHungAppWindow.argtypes = [wintypes.HWND]
    u.IsHungAppWindow.restype = wintypes.BOOL
    u.LoadCursorW.restype = ctypes.c_void_p
    u.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return u


def _init_cursor_names(user32) -> None:
    if _CURSOR_NAMES:
        return
    for ident, name in _IDC_NAMES.items():
        try:
            h = user32.LoadCursorW(None, ctypes.c_void_p(ident))
            if h:
                _CURSOR_NAMES[int(h)] = name
        except Exception:
            pass


def _cursor_name(user32) -> str:
    info = _CURSORINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if not user32.GetCursorInfo(ctypes.byref(info)):
            return "unavailable"
        if not (info.flags & 0x1):  # CURSOR_SHOWING
            return "hidden"
        return _CURSOR_NAMES.get(int(info.hCursor or 0), "other(%s)" % info.hCursor)
    except Exception as ex:
        return "error(%s)" % ex


def _cursor_in_window(user32, hwnd) -> bool:
    try:
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return False
        return (rect.left <= pt.x < rect.right) and (rect.top <= pt.y < rect.bottom)
    except Exception:
        return False


def _winforms_cursor_state(form) -> tuple[str, str]:
    """读 WinForms 侧光标状态; 读不到返回 ('n/a', 'n/a')。

    刻意用属性直接读取 (只读操作), 不用 form.Invoke —— 若 UI 线程正好阻塞,
    Invoke 会把采样线程一起挂住, 反而丢失最需要的那几帧。"""
    cursor = "n/a"
    use_wait = "n/a"
    try:
        cursor = str(getattr(form, "Cursor", None))
    except Exception as ex:
        cursor = "error(%s)" % ex
    try:
        from System.Windows.Forms import Application
        use_wait = str(bool(Application.UseWaitCursor))
    except Exception as ex:
        use_wait = "error(%s)" % ex
    return cursor, use_wait


def _sample_once(user32, form, hwnd, rt_bit: int) -> str:
    try:
        hung = bool(user32.IsHungAppWindow(wintypes.HWND(hwnd)))
    except Exception:
        hung = False
    try:
        fg = int(user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:
        fg = False
    cursor = _cursor_name(user32)
    in_win = _cursor_in_window(user32, hwnd)
    wf_cursor, use_wait = _winforms_cursor_state(form)
    return ("[curdiag] RT=%d cursor=%-28s in_window=%-5s hung=%-5s fg=%-5s "
            "wf_cursor=%s use_wait_cursor=%s"
            % (rt_bit, cursor, in_win, hung, fg, wf_cursor, use_wait))


def _sampler(window) -> None:
    try:
        from .build import _redirection_guard_enforced
        rt_bit = 1 if _redirection_guard_enforced() else 0
    except Exception:
        rt_bit = -1
    try:
        form = window.native
        hwnd = form.Handle.ToInt32()
    except Exception as ex:
        log(f"[curdiag] setup failed: {ex}")
        return

    user32 = _load_user32()
    _init_cursor_names(user32)
    log("[curdiag] sampling started (hover the main window if it misbehaves)")
    for delay in _SAMPLE_DELAYS:
        time.sleep(delay)
        try:
            log(_sample_once(user32, form, hwnd, rt_bit))
        except Exception as ex:
            log(f"[curdiag] sample failed: {ex}")
    log("[curdiag] sampling finished")


def log_startup_diagnostics(window) -> None:
    """窗口显示后启动后台诊断采样 (daemon, 不阻塞 UI, 失败静默)。

    shown 事件理论上只触发一次, 这里仍做防重入: 重复采样只会刷日志,
    没有诊断价值。"""
    if _STARTED.get("flag"):
        return
    _STARTED["flag"] = True
    try:
        threading.Thread(target=_sampler, args=(window,), daemon=True,
                         name="curdiag").start()
    except Exception as ex:
        log(f"[curdiag] thread start failed: {ex}")
