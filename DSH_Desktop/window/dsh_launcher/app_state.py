"""全局应用状态: 主窗口/托盘/退出、单实例、控制面板忙碌状态。

注意: 本模块中被 global 重绑定的变量 (_MAIN_WINDOW / _JOB_HANDLE 等)
在其他模块中必须用 app_state.X 访问, 不能 from-import (会拿到旧值)。"""

import ctypes
from ctypes import wintypes
import os
import sys

from .paths import WINDOW_DIR
from .logs import log
from .jobobject import _kernel32


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
