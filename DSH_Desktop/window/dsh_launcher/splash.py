"""启动加载窗 (splash): 进度条、取消、致命错误提示。"""

import ctypes
from ctypes import wintypes
import threading
import time

from .paths import WINDOW_DIR
from .logs import log
from .proc import _ACTIVE, _kill_proc_tree
from .theme import read_theme_tokens, resolve_initial_dark


# ==================== 启动加载窗 (splash) ====================
# 覆盖"构建后端 / 启动后端 / 等待就绪"阶段: 中间 deepseek娘.png,
# 下方蓝色 marquee 进度条。主题色与主窗口同一套方案 (CSS token + 偏好)。
_SPLASH = {"form": None, "bar": None, "bar_state": None, "lbl": None}


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
